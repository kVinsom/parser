"""Small, sequential public-page diagnostic; emits sizes, never cookies or bodies.

Run inside the Byparr image with /opt/vinted on PYTHONPATH. It does not enqueue
deliveries or change saved searches. Browser sizes exclude failed/in-flight work
and transport overhead. With the cache, cold route.fetch downloads are not
included in browser sizes; use a passive capture for deployment-wide totals.
"""
# Runtime-only modules live in the pinned container's /app and /opt/vinted.
# ruff: noqa: E402

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from urllib.parse import urlsplit

sys.path[:0] = ["/opt/vinted", "/app"]

from byparr_runtime import install_catalog_readiness
from src import endpoints, utils
from src.models import LinkRequest
from vinted import CatalogResult, classify
from vinted_regions import get_region


async def main():
    install_catalog_readiness(endpoints)
    if os.environ.get("PROFILE_SKIP_ANALYTICS") == "1":
        from traffic_policy import install_traffic_policy
        install_traffic_policy(endpoints)
    cache = None
    if os.environ.get("PROFILE_STATIC_CACHE") == "1":
        from static_cache import StaticAssetCache, install_static_cache
        cache = StaticAssetCache()
        install_static_cache(endpoints, cache)
    cases = [("DE", "Nike"), ("FR", "Adidas"), ("IT", "Stone Island"), ("UK", "Amiri")]
    selected = os.environ.get("PROFILE_REGIONS", "DE,FR,IT,UK").split(",")
    cases = [(code, brand) for code, brand in cases if code in selected]
    if not cases:
        raise ValueError("PROFILE_REGIONS must include a supported region")
    case_count = len(cases)
    rounds = int(os.environ.get("PROFILE_ROUNDS", "2" if cache else "1"))
    if not 1 <= rounds <= 2:
        raise ValueError("PROFILE_ROUNDS must be 1 or 2")
    cases *= rounds  # Later passes use fresh contexts and the same asset cache.
    paused = set()
    for index, (code, brand) in enumerate(cases):
        if code in paused:
            continue
        region = get_region(code)
        url = region.catalog_url(brand, 1, True)
        async for dep in utils.get_browser(None, None, None):
            rows, tasks = [], set()

            async def measured(request, rows=rows):
                try:
                    if cache and request in cache.served_requests:
                        return
                    response = await request.response()
                    sizes = await request.sizes()
                    parsed = urlsplit(request.url)
                    headers = await response.all_headers()
                    rows.append({
                        "type": request.resource_type, "host": parsed.hostname,
                        "path": parsed.path if (request.resource_type in ("script", "stylesheet")
                                                 or parsed.hostname == "cdn.cookielaw.org") else "[omitted]",
                        "status": response.status, "bytes": sum(sizes.values()),
                        "cache_control": headers.get("cache-control", ""),
                        "mime": headers.get("content-type", ""),
                        "encoding": headers.get("content-encoding", ""),
                        "vary": headers.get("vary", ""),
                        "sets_cookie": "set-cookie" in headers,
                    })
                except Exception:
                    pass

            def finished(request, measured=measured, tasks=tasks):
                task = asyncio.create_task(measured(request))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            dep.page.on("requestfinished", finished)
            start = time.monotonic()
            try:
                result = await endpoints.read_item(LinkRequest(
                    url=url, maxTimeout=20000, blockMedia=True, returnOnlyCookies=False), dep)
                elapsed = time.monotonic() - start
                checked = classify(CatalogResult(code, url), region,
                                   result.solution.url, result.solution.status,
                                   result.solution.response)
                if checked.outcome in ("blocked", "rate_limited"):
                    paused.add(code)
                if tasks:
                    await asyncio.gather(*list(tasks))
                grouped = defaultdict(int)
                for row in rows:
                    grouped[row["type"]] += row["bytes"]
                print(json.dumps({"region": code, "brand": brand,
                                  "elapsed_seconds": elapsed, "outcome": checked.outcome,
                                  "items": checked.item_count, "by_type": dict(grouped),
                                  "pass": index // case_count + 1,
                                  "cache_hits": cache.hits if cache else 0,
                                  "cache_bytes": cache.bytes if cache else 0,
                                  "cache_stats": dict(cache.stats) if cache else {},
                                  "resources": (sorted(rows, key=lambda r: r["bytes"], reverse=True)[:12]
                                                if os.environ.get("PROFILE_VERBOSE") == "1" else [])}), flush=True)
            except Exception as exc:
                print(json.dumps({"region": code, "error": type(exc).__name__}), flush=True)
            finally:
                await dep.context.close()
        await asyncio.sleep(4)


if __name__ == "__main__":
    asyncio.run(main())
