"""Real loopback gRPC transport tests with a controlled, offline catalog adapter."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import grpc
import grpc_client
from grpc_client import VintedClient
from grpc_health.v1 import health_pb2
from grpc_server import OUTCOMES, create_server
from vinted import CatalogResult
from vinted_rpc import access_pb2 as pb


def catalog(region, **kwargs):
    body = '<html><title>Vinted</title><a href="/items/123-coat">Пальто</a></html>'
    return CatalogResult(region.code, region.catalog_url(kwargs.get("search_text", ""), kwargs.get("page", 1)),
                         outcome="page_loaded", reason="Catalog with items", body=body,
                         item_count=1, item_link_count=1, response_bytes=len(body.encode("utf-8")),
                         solution_status=200, byparr_http_status=200, browser_mode="ready-content",
                         block_media=kwargs.get("block_media", True))


class GrpcTests(unittest.TestCase):
    def setUp(self):
        self.fetch = Mock(side_effect=catalog)
        self.server, self.health, self.executor, port = create_server(
            listen="127.0.0.1:0", fetch=self.fetch)
        self.server.start()
        self.client = VintedClient(f"127.0.0.1:{port}")
        self.target = f"127.0.0.1:{port}"
        grpc.channel_ready_future(self.client.channel).result(timeout=5)

    def tearDown(self):
        self.client.channel.close()
        self.server.stop(0).wait()
        self.executor.shutdown(wait=True)

    def assert_rpc_error(self, code, call):
        with self.assertRaises(grpc.RpcError) as caught:
            call()
        self.assertEqual(caught.exception.code(), code)
        return caught.exception

    def test_health_and_region_registry_do_not_call_browser(self):
        self.assertEqual(self.client.health().status, health_pb2.HealthCheckResponse.SERVING)
        self.assertEqual([r.code for r in self.client.list_regions().regions], ["UK", "FR", "IT", "DE"])
        self.health.enter_graceful_shutdown()
        self.assertEqual(self.client.health().status, health_pb2.HealthCheckResponse.NOT_SERVING)
        self.fetch.assert_not_called()

    def test_wire_defaults_and_uk_alias(self):
        result = self.client.stub.FetchCatalog(pb.FetchCatalogRequest(region=" gb "), timeout=90)
        self.assertEqual((result.region, result.outcome, result.html_utf8), ("UK", pb.PAGE_LOADED, b""))
        args = self.fetch.call_args
        self.assertEqual(args.args[0].code, "UK")
        self.assertEqual(args.kwargs, dict(endpoint="http://127.0.0.1:8191", timeout=60,
                                          search_text="", page=1, block_media=True, measure_timing=False))

    def test_budget_pause_from_browser_stays_typed_and_preserves_retry_after(self):
        for region in ("FR", "IT", "DE", "UK"):
            with self.subTest(region=region):
                self.fetch.side_effect = lambda selected, **kw: CatalogResult(
                    selected.code, selected.catalog_url(), reason="ProxyBudgetWait", retry_after=17)
                error = self.assert_rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED,
                    lambda: self.client.fetch_catalog(region))
                metadata = dict(error.trailing_metadata())
                self.assertEqual(metadata["source-deferred-code"], "ProxyBudgetWait")
                self.assertEqual(metadata["retry-after"], "17")

    def test_item_identity_and_newest_order_cross_transport(self):
        result = self.client.stub.FetchItem(pb.FetchItemRequest(region="FR", item_id="10001", include_html=True), timeout=90)
        self.assertEqual(result.outcome, pb.PAGE_LOADED)
        self.assertEqual(self.fetch.call_args.kwargs["item_id"], "10001")
        for item_id in ("", "../catalog", "https://example.com", "0", "123/456"):
            self.assert_rpc_error(grpc.StatusCode.INVALID_ARGUMENT, lambda item_id=item_id: self.client.stub.FetchItem(
                pb.FetchItemRequest(region="FR", item_id=item_id), timeout=90))
        self.client.stub.FetchCatalog(pb.FetchCatalogRequest(region="FR", newest_first=True), timeout=90)
        self.assertTrue(self.fetch.call_args.kwargs["newest_first"])

    def test_search_page_media_and_html_cross_the_transport(self):
        result = self.client.fetch_catalog("DE", search_text="пальто & nike", page=2, load_media=True)
        self.assertEqual(result.outcome, pb.PAGE_LOADED)
        self.assertIn("Пальто", result.html_utf8.decode("utf-8"))
        self.assertEqual(result.response_bytes, len(result.html_utf8))
        self.assertEqual(self.fetch.call_args.kwargs["search_text"], "пальто & nike")
        self.assertEqual(self.fetch.call_args.kwargs["page"], 2)
        self.assertFalse(self.fetch.call_args.kwargs["block_media"])
        self.assertTrue(result.HasField("upstream_status"))

    def test_large_html_over_default_four_mib_is_transferred_intact(self):
        body = "Пальто\n" * 500_000
        self.assertGreater(len(body.encode("utf-8")), 4 * 1024 * 1024)
        def large(region, **kwargs):
            result = catalog(region, **kwargs)
            result.body = body
            result.response_bytes = len(body.encode("utf-8"))
            return result
        self.fetch.side_effect = large
        response = self.client.fetch_catalog("DE")
        self.assertEqual(response.html_utf8.decode("utf-8"), body)

    def test_errors_remain_typed_and_do_not_expose_challenge_html(self):
        for outcome, enum_value in OUTCOMES.items():
            if outcome == "page_loaded":
                continue
            with self.subTest(outcome=outcome):
                self.fetch.side_effect = lambda region, outcome=outcome, **kw: CatalogResult(
                    region.code, region.catalog_url(), outcome=outcome, body="challenge-secret")
                result = self.client.fetch_catalog("FR")
                self.assertEqual(result.outcome, enum_value)
                self.assertEqual(result.html_utf8, b"")
                self.assertFalse(result.HasField("upstream_status"))
                self.assertNotIn("cookie", result.DESCRIPTOR.fields_by_name)

    def test_invalid_requests_are_rejected_before_browser_work(self):
        requests = [pb.FetchCatalogRequest(), pb.FetchCatalogRequest(region="PL"), pb.FetchCatalogRequest(region="US"),
                    pb.FetchCatalogRequest(region="https://127.0.0.1"),
                    pb.FetchCatalogRequest(region="FR", page=0),
                    pb.FetchCatalogRequest(region="FR", timeout_seconds=0),
                    pb.FetchCatalogRequest(region="FR", timeout_seconds=301),
                    pb.FetchCatalogRequest(region="FR", search_text="я" * 251)]
        for request in requests:
            with self.subTest(request=request):
                self.assert_rpc_error(grpc.StatusCode.INVALID_ARGUMENT,
                                      lambda request=request: self.client.stub.FetchCatalog(request, timeout=30))
        self.fetch.assert_not_called()

    def test_request_limit_blocks_oversized_messages(self):
        self.assert_rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED, lambda: self.client.stub.FetchCatalog(
            pb.FetchCatalogRequest(region="FR", search_text="x" * 100_000), timeout=30))
        self.fetch.assert_not_called()

    def test_deadline_reduces_browser_timeout(self):
        self.client.stub.FetchCatalog(pb.FetchCatalogRequest(region="FR", timeout_seconds=60), timeout=25)
        self.assertIn(self.fetch.call_args.kwargs["timeout"], (14, 15))

    def test_insufficient_deadline_starts_no_browser_work(self):
        self.assert_rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED, lambda: self.client.stub.FetchCatalog(
            pb.FetchCatalogRequest(region="FR"), timeout=3))
        self.fetch.assert_not_called()

    def test_busy_browser_rejects_second_fetch_but_health_remains_available(self):
        started, release = threading.Event(), threading.Event()
        def slow(region, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test synchronization timeout")
            return catalog(region, **kwargs)
        self.fetch.side_effect = slow
        first = self.client.stub.FetchCatalog.future(pb.FetchCatalogRequest(region="FR"), timeout=30)
        try:
            self.assertTrue(started.wait(3))
            self.assert_rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED, lambda: self.client.fetch_catalog("DE"))
            self.assertEqual(self.client.health().status, health_pb2.HealthCheckResponse.SERVING)
            self.assertEqual(len(self.client.list_regions().regions), 4)
        finally:
            release.set()
        self.assertEqual(first.result(timeout=5).outcome, pb.PAGE_LOADED)
        self.assertEqual(self.client.fetch_catalog("IT").outcome, pb.PAGE_LOADED)

    def test_cancellation_keeps_slot_until_backend_finishes(self):
        started, release = threading.Event(), threading.Event()
        def slow(region, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test synchronization timeout")
            return catalog(region, **kwargs)
        self.fetch.side_effect = slow
        first = self.client.stub.FetchCatalog.future(pb.FetchCatalogRequest(region="FR"), timeout=30)
        try:
            self.assertTrue(started.wait(3))
            self.assertTrue(first.cancel())
            self.assert_rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED, lambda: self.client.fetch_catalog("DE"))
            self.assertEqual(self.fetch.call_count, 1)
        finally:
            release.set()

    def test_adapter_exception_is_redacted_and_capacity_is_released(self):
        self.fetch.side_effect = RuntimeError("secret-proxy-password")
        error = self.assert_rpc_error(grpc.StatusCode.INTERNAL, lambda: self.client.fetch_catalog("FR"))
        self.assertNotIn("secret-proxy-password", error.details())
        self.fetch.side_effect = catalog
        self.assertEqual(self.client.fetch_catalog("FR").outcome, pb.PAGE_LOADED)

    def test_response_size_limit_is_explicit(self):
        with patch("grpc_server.MAX_RESPONSE_BYTES", 20):
            self.assert_rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED,
                                  lambda: self.client.fetch_catalog("FR"))
        self.assertEqual(self.client.fetch_catalog("FR").outcome, pb.PAGE_LOADED)

    def test_cli_preserves_partial_report_and_stops_on_service_failure(self):
        self.fetch.side_effect = lambda region, **kw: (
            catalog(region, **kw) if region.code == "UK" else
            CatalogResult(region.code, region.catalog_url(), outcome="service_error"))
        temporary_root = grpc_client.ROOT / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory, \
                patch.object(grpc_client, "ROOT", Path(directory)), contextlib.redirect_stdout(io.StringIO()):
            code = grpc_client.main(["--target", self.target, "--regions", "all", "--save-html"])
            report = json.loads(next(Path(directory).glob(".artifacts/*/report.json")).read_text(encoding="utf-8"))
            self.assertEqual(code, 2)
            self.assertEqual(len(report["results"]), 2)
            self.assertEqual(report["successful"], 1)
            self.assertFalse(report["all_accessible"])
            self.assertTrue(Path(report["results"][0]["html_path"]).is_file())


class RegionalCapacityTests(unittest.TestCase):
    def test_four_catalogs_and_two_details_have_bounded_independent_lanes(self):
        release, entered = threading.Event(), threading.Condition()
        calls = []

        def slow(region, **kwargs):
            with entered:
                calls.append(kwargs.get("item_id"))
                entered.notify_all()
            if not release.wait(5):
                raise RuntimeError("Test synchronization timeout")
            return catalog(region, **kwargs)

        server, _, executor, port = create_server(listen="127.0.0.1:0", max_inflight=6, fetch=slow)
        server.start()
        client = VintedClient(f"127.0.0.1:{port}")
        pending = []
        try:
            pending.extend(client.stub.FetchCatalog.future(pb.FetchCatalogRequest(region=r), timeout=30)
                           for r in ("UK", "FR", "IT", "DE"))
            with entered:
                self.assertTrue(entered.wait_for(lambda: len(calls) == 4, timeout=3))
            with self.assertRaises(grpc.RpcError) as error:
                client.fetch_catalog("FR")
            self.assertEqual(error.exception.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
            pending.extend(client.stub.FetchItem.future(pb.FetchItemRequest(region="FR", item_id=i), timeout=30)
                           for i in ("123", "124"))
            with entered:
                self.assertTrue(entered.wait_for(lambda: len(calls) == 6, timeout=3))
            self.assertEqual(calls.count(None), 4)
            self.assertEqual(client.health().status, health_pb2.HealthCheckResponse.SERVING)
        finally:
            release.set()
            for call in pending:
                call.result(timeout=5)
            client.channel.close()
            server.stop(0).wait()
            executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
