"""Bounded local parser load and optional read-only Vinted smoke. Never sends Telegram."""

import argparse
import asyncio
import json
import sys
import time
import tracemalloc
from pathlib import Path
from statistics import median
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vpars_parser.errors import ParserError  # noqa: E402
from vpars_parser.gateway import GrpcGatewayClient  # noqa: E402
from vpars_parser.models import ParseRequest, Region  # noqa: E402
from vpars_parser.ports import GatewayPage  # noqa: E402
from vpars_parser.service import ParserService  # noqa: E402
from vpars_parser.settings import Settings  # noqa: E402


class SimulatedGateway:
    def __init__(self):
        self.html = (ROOT / "tests/fixtures/FR.html").read_bytes()

    async def list_regions(self, request_id):
        return [Region(code="FR", name="France", base_url="https://www.vinted.fr")]

    async def fetch_catalog(self, query):
        await asyncio.sleep(0.08)
        url = "https://www.vinted.fr/catalog?" + urlencode(
            {
                "order": "newest_first",
                "page": query.page,
            }
        )
        return GatewayPage(
            region="FR",
            url=url,
            final_url=url,
            outcome="PAGE_LOADED",
            html=self.html,
            upstream_status=200,
        )

    async def close(self):
        pass


async def sample(service, region="FR", page=1):
    started = time.perf_counter()
    try:
        result = await service.parse(
            ParseRequest(region_id=region, page=page, newest_first=True, timeout_seconds=30)
        )
        row = {
            "status": result.completeness,
            "ids": len(result.observed_item_ids),
            "pagination": result.pagination.has_more,
            "cards_skipped": result.cards_skipped,
        }
    except ParserError as exc:
        row = {"status": exc.detail.code, "retry_after": exc.detail.retry_after}
    row.update(region=region, page=page, ms=(time.perf_counter() - started) * 1000)
    return row


async def run(output, live, live_only=False):
    report = {
        "scope": "Parser with simulated 80ms gateway; excludes browser, worker SQL and Telegram",
        "duration_per_rate_seconds": 12,
        "loads": [],
    }
    for rate in (() if live_only else (60, 120, 300)):
        service = ParserService(SimulatedGateway(), Settings())
        tracemalloc.start()
        start, cpu_start = time.perf_counter(), time.process_time()
        tasks, peak_active = [], 0
        count = rate // 5  # Twelve seconds at the requested rate.
        for index in range(count):
            await asyncio.sleep(max(0, start + index * 60 / rate - time.perf_counter()))
            tasks.append(asyncio.create_task(sample(service)))
            peak_active = max(peak_active, sum(not task.done() for task in tasks))
        samples = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        latencies = sorted(row["ms"] for row in samples)
        report["loads"].append(
            {
                "target_rpm": rate,
                "requests": count,
                "success": sum(r["status"] == "complete" for r in samples),
                "errors": [r for r in samples if r["status"] != "complete"],
                "elapsed_seconds": round(elapsed, 3),
                "p50_ms": round(median(latencies), 2),
                "p95_ms": round(latencies[min(count - 1, int(count * 0.95))], 2),
                "peak_active": peak_active,
                "unfinished": sum(not task.done() for task in tasks),
                "cpu_percent_one_core": round((time.process_time() - cpu_start) / elapsed * 100, 2),
                "python_peak_heap_mib": round(peak / 1024**2, 2),
                "catalog_requests": count,
                "detail_requests": 0,
                "retries": 0,
                "telegram_requests": 0,
            }
        )
        await service.close()
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if live or live_only:
        # Up to two pages per region; first failure stops the region, no immediate retry.
        service = ParserService(GrpcGatewayClient(Settings.from_env()), Settings())
        report["live"] = []
        stopped = set()
        previous = 0
        try:
            for page in (1, 2):
                for region in ("UK", "FR", "IT", "DE"):
                    if region in stopped:
                        continue
                    await asyncio.sleep(max(0, previous + 4 - time.perf_counter()))
                    previous = time.perf_counter()
                    row = await sample(service, region, page)
                    report["live"].append(row)
                    if row["status"] != "complete":
                        stopped.add(region)
                    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        finally:
            await service.close()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--live-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.output, args.live, args.live_only))
