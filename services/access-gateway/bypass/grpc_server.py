"""Bounded gRPC adapter around the existing validated catalog reader."""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import grpc
from cancellable_http import RequestCancellation
from grpc_common import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, SERVICE_NAME
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from item_api import ItemApiClient, item_url
from vinted import fetch_catalog, validate_endpoint
from vinted_regions import REGIONS, get_region
from vinted_rpc import access_pb2 as pb
from vinted_rpc import access_pb2_grpc as rpc

OUTCOMES = {name.lower(): pb.CatalogOutcome.Value(name) for name in (
    "PAGE_LOADED", "BLOCKED", "RATE_LIMITED", "UNEXPECTED_REDIRECT",
    "UNEXPECTED_CONTENT", "UPSTREAM_ERROR", "SERVICE_ERROR", "TIMEOUT",
)}


class VintedAccess(rpc.VintedAccessServicer):
    def __init__(self, endpoint, max_inflight=1, fetch=fetch_catalog):
        self.endpoint = validate_endpoint(endpoint)
        if type(max_inflight) is not int or not 1 <= max_inflight <= 6:
            raise ValueError("max_inflight must be 1 to 6")
        self.slots = threading.BoundedSemaphore(max_inflight)
        self.detail_slots = threading.BoundedSemaphore(max(1, min(2, max_inflight // 2)))
        self.catalog_slots = threading.BoundedSemaphore(min(4, max_inflight))
        self.fetch = fetch
        self.item_api = ItemApiClient() if fetch is fetch_catalog else None
        self.budget = None
        if fetch is fetch_catalog and os.environ.get("VINTED_PROXY_BUDGET_REQUIRED") == "1":
            from budget_guard import BudgetGuard
            self.budget = BudgetGuard(self.endpoint)

    def ListRegions(self, request, context):
        return pb.ListRegionsResponse(regions=[
            pb.Region(code=r.code, name=r.name, base_url=r.base_url)
            for r in REGIONS.values()
        ])

    def FetchCatalog(self, request, context):
        return self._fetch(request, context)

    def FetchItem(self, request, context):
        if not re.fullmatch(r"[1-9][0-9]{0,19}", request.item_id):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Numeric item_id is required")
        catalog = pb.FetchCatalogRequest(region=request.region, include_html=request.include_html)
        if request.HasField("timeout_seconds"):
            catalog.timeout_seconds = request.timeout_seconds
        return self._fetch(catalog, context, item_id=request.item_id, force_html=request.force_html)

    def _fetch(self, request, context, item_id=None, force_html=False):
        operation_started = time.monotonic()
        try:
            region = get_region(request.region)
            page = request.page if request.HasField("page") else 1
            timeout = request.timeout_seconds if request.HasField("timeout_seconds") else 60
            if page < 1:
                raise ValueError("page must be positive")
            if not 1 <= timeout <= 300:
                raise ValueError("timeout_seconds must be 1 to 300")
            if len(request.search_text.encode("utf-8")) > 500:
                raise ValueError("search_text must be at most 500 UTF-8 bytes")
        except ValueError:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          "Use region UK/GB, FR, IT or DE; page >= 1; "
                          "timeout_seconds 1..300; search_text <= 500 UTF-8 bytes")

        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "Request is no longer active")
        if self.budget is not None:
            from proxy_budget import BudgetPause
            try:
                self.budget.check("details" if item_id is not None else "catalog")
            except BudgetPause as exc:
                context.set_trailing_metadata((("retry-after", str(exc.retry_after)),
                                               ("source-deferred-code", exc.code)))
                context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Source request paused by proxy budget")
        remaining = context.time_remaining()
        if remaining is not None:
            # The HTTP adapter reserves ten extra seconds for the Byparr response.
            budget = math.floor(remaining - 10)
            if budget < 1:
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED,
                              "Deadline must allow at least 11 seconds for Byparr")
            timeout = min(timeout, budget)
        detail_acquired = item_id is not None
        lane = self.detail_slots if detail_acquired else self.catalog_slots
        if not lane.acquire(blocking=False):
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Request lane capacity is occupied")
        if not self.slots.acquire(blocking=False):
            lane.release()
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Browser capacity is occupied")
        try:
            if not context.is_active():
                context.abort(grpc.StatusCode.CANCELLED, "Request is no longer active")
            if item_id is not None and not force_html and self.item_api is not None and timeout >= 5:
                started = time.monotonic()
                payload = self.item_api.fetch(region.code, item_id, timeout=min(3, timeout - 1))
                elapsed = time.monotonic() - started
                logging.info("Gateway item phase region=%s representation=api elapsed_ms=%.1f accepted=%s",
                             region.code, elapsed * 1000, payload is not None)
                if not context.is_active():
                    context.abort(grpc.StatusCode.CANCELLED, "Request is no longer active")
                if payload:
                    url = item_url(region.code, item_id)
                    return pb.FetchCatalogResponse(
                        region=region.code, url=url, final_url=url, outcome=pb.PAGE_LOADED,
                        upstream_status=200, reason="Verified item API response",
                        item_json_utf8=payload if request.include_html else b"",
                        response_bytes=len(payload), elapsed_seconds=elapsed,
                    )
                timeout = max(1, timeout - math.ceil(elapsed))
            try:
                options = {}
                if request.newest_first:
                    options["newest_first"] = True
                if item_id is not None:
                    options["item_id"] = item_id
                if self.fetch is fetch_catalog:
                    cancellation = RequestCancellation()
                    if not context.add_callback(cancellation.cancel):
                        cancellation.cancel()
                    options["cancellation"] = cancellation
                result = self.fetch(region, endpoint=self.endpoint, timeout=timeout,
                                    search_text=request.search_text, page=page,
                                    block_media=not request.load_media, measure_timing=request.measure_timing,
                                    **options)
                logging.info("Gateway source phase region=%s kind=%s outcome=%s total_ms=%.1f browser_http_ms=%.1f",
                             region.code, "details" if item_id is not None else "catalog", result.outcome,
                             (time.monotonic() - operation_started) * 1000, result.timings_ms.get("byparr_http_ms", 0))
            except Exception:
                # Never expose exception text, backend addresses or proxy credentials.
                logging.error("Catalog adapter raised an unexpected exception")
                context.abort(grpc.StatusCode.INTERNAL, "Catalog adapter failed")
            if not context.is_active():
                context.abort(grpc.StatusCode.CANCELLED, "Request is no longer active")
            from proxy_budget import CODES
            if result.reason in CODES:
                context.set_trailing_metadata((("retry-after", str(result.retry_after or 30)),
                                               ("source-deferred-code", result.reason)))
                context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Source request paused by proxy budget")
            if result.outcome not in OUTCOMES:
                context.abort(grpc.StatusCode.INTERNAL, "Unknown catalog result")
            if result.retry_after:
                context.set_trailing_metadata((("retry-after", str(result.retry_after)),))
            response = pb.FetchCatalogResponse(
                region=result.region, url=result.url, outcome=OUTCOMES[result.outcome],
                reason=result.reason, title=result.title, final_url=result.final_url,
                byparr_version=result.byparr_version, browser_mode=result.browser_mode,
                block_media=result.block_media, item_count=result.item_count,
                item_link_count=result.item_link_count, item_samples=result.item_samples,
                response_bytes=result.response_bytes, elapsed_seconds=result.elapsed_seconds,
                timings_ms=result.timings_ms,
                browser_lifecycle=result.browser_lifecycle,
            )
            if result.solution_status is not None:
                response.upstream_status = result.solution_status
            if result.byparr_http_status is not None:
                response.byparr_http_status = result.byparr_http_status
            if request.include_html and result.outcome == "page_loaded":
                response.html_utf8 = result.body.encode("utf-8")
            if response.ByteSize() > MAX_RESPONSE_BYTES:
                context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Catalog response exceeds 32 MiB")
            return response
        finally:
            # HTTP shutdown wakes synchronous work; release only once it has exited.
            self.slots.release()
            lane.release()


def create_server(endpoint="http://127.0.0.1:8191", listen="127.0.0.1:50051",
                  max_inflight=1, fetch=fetch_catalog):
    servicer = VintedAccess(endpoint, max_inflight, fetch)
    executor = ThreadPoolExecutor(max_workers=max_inflight + 4, thread_name_prefix="vinted-grpc")
    server = grpc.server(
        executor, compression=grpc.Compression.Gzip,
        maximum_concurrent_rpcs=max_inflight + 16,
        options=[("grpc.max_receive_message_length", MAX_REQUEST_BYTES),
                 ("grpc.max_send_message_length", MAX_RESPONSE_BYTES),
                 ("grpc.so_reuseport", 0)],
    )
    rpc.add_VintedAccessServicer_to_server(servicer, server)
    probe = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(probe, server)
    probe.set("", health_pb2.HealthCheckResponse.SERVING)
    probe.set(SERVICE_NAME, health_pb2.HealthCheckResponse.SERVING)
    try:
        port = server.add_insecure_port(listen)
        if not port:
            raise ValueError("Cannot bind gRPC listener")
    except Exception:
        executor.shutdown(wait=False)
        raise
    return server, probe, executor, port


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Vinted gRPC catalog access")
    parser.add_argument("--listen", default=os.environ.get("VINTED_GRPC_LISTEN", "127.0.0.1:50051"))
    parser.add_argument("--byparr-url", default=os.environ.get("BYPARR_URL", "http://127.0.0.1:8191"))
    parser.add_argument("--max-inflight", type=int, default=os.environ.get("VINTED_GRPC_MAX_INFLIGHT", "1"))
    args = parser.parse_args(argv)
    try:
        server, probe, executor, _ = create_server(args.byparr_url, args.listen, args.max_inflight)
    except (ValueError, RuntimeError):
        parser.error("Cannot start gRPC: check listener, Byparr base URL and max-inflight (1..6)")
    stopping = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopping.set())
    server.start()
    print(f"Vinted gRPC listening on {args.listen}; browser capacity={args.max_inflight}", flush=True)
    try:
        stopping.wait()
    finally:
        probe.enter_graceful_shutdown()
        server.stop(grace=10).wait()
        executor.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
