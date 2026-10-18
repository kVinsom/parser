"""One bounded compact-catalog experiment; never changes jobs or sends messages.

Run inside Byparr via stdin. Stops on a refused catalog or JSON request; no
identity rotation or retry. Bodies stay in /tmp for local parser comparison.
"""
# Imports follow the mounted runtime path setup when this file is piped via stdin.
# ruff: noqa: E402
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path[:0] = ["/opt/vinted", "/app"]
from byparr_runtime import install_catalog_readiness
from src import endpoints, utils
from src.models import LinkRequest
from static_cache import StaticAssetCache, install_static_cache
from traffic_policy import install_traffic_policy
from vinted import CatalogResult, classify
from vinted_regions import get_region


async def main():
    region = get_region(os.environ.get("PROBE_REGION", "FR"))
    brand = os.environ.get("PROBE_BRAND", "Adidas")
    install_catalog_readiness(endpoints)
    install_traffic_policy(endpoints)
    cache = StaticAssetCache()
    install_static_cache(endpoints, cache)
    url = region.catalog_url(brand, 1, True)
    async for dep in utils.get_browser(None, None, None):
        async with asyncio.timeout(30):
            result = await endpoints.read_item(LinkRequest(
                url=url, maxTimeout=20000, blockMedia=True, returnOnlyCookies=False), dep)
            checked = classify(CatalogResult(region.code, url), region,
                               result.solution.url, result.solution.status, result.solution.response)
            print(json.dumps({"region": region.code, "catalog": checked.outcome}), flush=True)
            if checked.outcome != "page_loaded":
                return
            references = set()
            for entry in cache.entries.values():
                script = entry.body.decode("utf-8", errors="replace")
                for match in re.finditer(r'["\x27`]([^"\x27`\s]{0,70}(?:catalog/items|catalog_items)[^"\x27`\s]{0,70})["\x27`]', script):
                    references.add(match[1])
            print(json.dumps({"public_script_catalog_paths": sorted(references)}), flush=True)
            # Same anonymous browser context as the catalog, one ordinary JSON call.
            target = "/api/v2/catalog/items?" + urlencode({
                "search_text": brand, "page": 1, "per_page": 96, "order": "newest_first"})
            data = await dep.page.evaluate("""async target => {
                const r = await fetch(target, {headers:{Accept:'application/json'},
                    signal:AbortSignal.timeout(8000), redirect:'error'});
                return {status:r.status, mime:r.headers.get('content-type'),
                    retry_after:r.headers.get('retry-after'), body:await r.text()};
            }""", target)
            body = data.pop("body")
            data["body_bytes"] = len(body.encode())
            if data["status"] == 200 and "json" in (data["mime"] or ""):
                payload = json.loads(body)
                data["keys"] = list(payload) if isinstance(payload, dict) else []
                data["items"] = len(payload.get("items", [])) if isinstance(payload, dict) else 0
                Path(f"/tmp/compact-{region.code}.json").write_text(body)
                Path(f"/tmp/compact-{region.code}.html").write_text(result.solution.response)
            print(json.dumps(data), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
