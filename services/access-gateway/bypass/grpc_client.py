"""Reusable gRPC client and one-shot integration check for Vinted catalogs."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import grpc
from artifacts import save_html
from grpc_common import (
    DEFAULT_TARGET,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    SERVICE_NAME,
)
from grpc_health.v1 import health_pb2, health_pb2_grpc
from vinted_regions import select_regions
from vinted_rpc import access_pb2 as pb
from vinted_rpc import access_pb2_grpc as rpc

ROOT = Path(__file__).resolve().parent


class VintedClient:
    def __init__(self, target=DEFAULT_TARGET):
        self.channel = grpc.insecure_channel(
            target, compression=grpc.Compression.Gzip,
            options=[("grpc.max_receive_message_length", MAX_RESPONSE_BYTES),
                     ("grpc.max_send_message_length", MAX_REQUEST_BYTES),
                     ("grpc.enable_retries", 0)],
        )
        self.stub = rpc.VintedAccessStub(self.channel)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.channel.close()

    def health(self, timeout=3):
        return health_pb2_grpc.HealthStub(self.channel).Check(
            health_pb2.HealthCheckRequest(service=SERVICE_NAME), timeout=timeout,
        )

    def list_regions(self, timeout=3):
        return self.stub.ListRegions(pb.ListRegionsRequest(), timeout=timeout)

    def fetch_catalog(self, region, *, search_text="", page=1, timeout=60,
                      include_html=True, load_media=False, measure_timing=False):
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("timeout must be 1 to 300 seconds")
        return self.stub.FetchCatalog(pb.FetchCatalogRequest(
            region=region, search_text=search_text, page=page, timeout_seconds=timeout,
            include_html=include_html, load_media=load_media,
            measure_timing=measure_timing,
        ), timeout=timeout + 15)


def response_summary(response):
    return {
        "region": response.region, "url": response.url,
        "outcome": pb.CatalogOutcome.Name(response.outcome).lower(),
        "reason": response.reason, "title": response.title, "final_url": response.final_url,
        "upstream_status": response.upstream_status if response.HasField("upstream_status") else None,
        "byparr_http_status": response.byparr_http_status if response.HasField("byparr_http_status") else None,
        "byparr_version": response.byparr_version, "browser_mode": response.browser_mode,
        "browser_lifecycle": response.browser_lifecycle,
        "block_media": response.block_media, "item_count": response.item_count,
        "item_link_count": response.item_link_count, "item_samples": list(response.item_samples),
        "response_bytes": response.response_bytes, "html_bytes_received": len(response.html_utf8),
        "elapsed_seconds": response.elapsed_seconds,
        "timings_ms": dict(response.timings_ms),
    }


def write_report(folder, report):
    report["successful"] = sum(row["outcome"] == "page_loaded" for row in report["results"])
    report["all_accessible"] = report["successful"] == len(report["requested_regions"])
    temporary = folder / "report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(folder / "report.json")
    lines = ["# Перевірка доступу через gRPC", "",
             f"Успішні каталоги: {report['successful']}/{len(report['requested_regions'])}.", "",
             "| Регіон | Результат | Товарів | Повний час gRPC, с | Отримано HTML, байтів |",
             "|---|---|---:|---:|---:|"]
    for row in report["results"]:
        lines.append(f"| {row['region']} | {row['outcome']} | {row.get('item_count', 0)} | "
                     f"{row['rpc_elapsed_seconds']} | {row.get('html_bytes_received', 0)} |")
    lines.extend(["", "Одноразовий тест доступу через gRPC; не перевірка роботи 24/7.", ""])
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Call the Vinted gRPC service")
    parser.add_argument("--target", default=os.environ.get("VINTED_GRPC_TARGET", DEFAULT_TARGET))
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--health", action="store_true")
    actions.add_argument("--list-regions", action="store_true")
    parser.add_argument("--regions", "--region", nargs="+", default=["FR"])
    parser.add_argument("--search-text", default="")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--save-html", action="store_true")
    parser.add_argument("--load-media", action="store_true")
    args = parser.parse_args(argv)
    try:
        regions = select_regions(args.regions)
        if args.page < 1 or not 1 <= args.timeout <= 300 or len(args.search_text.encode("utf-8")) > 500:
            raise ValueError("Use page >= 1, timeout 1..300 and search text <= 500 UTF-8 bytes")
    except ValueError as error:
        parser.error(str(error))
    with VintedClient(args.target) as client:
        if args.health or args.list_regions:
            try:
                if args.health:
                    status = client.health().status
                    print(health_pb2.HealthCheckResponse.ServingStatus.Name(status))
                    return 0 if status == health_pb2.HealthCheckResponse.SERVING else 2
                print(json.dumps([{"code": r.code, "name": r.name, "base_url": r.base_url}
                                  for r in client.list_regions().regions], ensure_ascii=True, indent=2))
                return 0
            except grpc.RpcError as error:
                print("gRPC error: " + error.code().name)
                return 2
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        folder = ROOT / ".artifacts" / ("grpc-" + stamp + "-" + uuid.uuid4().hex[:8])
        folder.mkdir(parents=True)
        report = {"transport": "grpc", "started_at": datetime.now(timezone.utc).isoformat(),
                  "requested_regions": [r.code for r in regions], "results": [], "interrupted": False,
                  "search_text": args.search_text, "page": args.page,
                  "include_html": args.save_html, "block_media": not args.load_media}
        write_report(folder, report)
        print(f"REPORT {folder / 'report.md'}", flush=True)
        try:
            for region in regions:
                started = time.monotonic()
                try:
                    response = client.fetch_catalog(region.code, search_text=args.search_text,
                                                    page=args.page, timeout=args.timeout,
                                                    include_html=args.save_html, load_media=args.load_media)
                    rpc_elapsed = time.monotonic() - started
                    row = response_summary(response)
                    if args.save_html and response.html_utf8:
                        path = folder / (region.code + ".html.gz")
                        save_html(path, response.html_utf8.decode("utf-8"))
                        row["html_path"] = str(path)
                except grpc.RpcError as error:
                    rpc_elapsed = time.monotonic() - started
                    row = {"region": region.code, "outcome": "rpc_error", "grpc_code": error.code().name}
                row["rpc_elapsed_seconds"] = round(rpc_elapsed, 6)
                report["results"].append(row)
                write_report(folder, report)
                print(f"{region.code}: {row['outcome']}; items={row.get('item_count', 0)}; "
                      f"{row['rpc_elapsed_seconds']}s", flush=True)
                if row["outcome"] in ("service_error", "rpc_error"):
                    break
        except KeyboardInterrupt:
            report["interrupted"] = True
            write_report(folder, report)
            return 130
        return 0 if report["all_accessible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
