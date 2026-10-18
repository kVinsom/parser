"""Bounded public catalog transport investigation; never logs cookies or bodies.

Run through worker/tools/diagnose_traffic.py while normal source work is held.
Endpoint and headers come from Vinted's public catalog client JavaScript.
"""
# ruff: noqa: E402
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path[:0] = ["/opt/vinted", "/tmp", "/app"]
from byparr_runtime import (
    REQUEST_CATALOG_TRANSPORT,
    BrowserSession,
    install_catalog_readiness,
)
from catalog_document import catalog_states
from src import endpoints, utils
from src.models import LinkRequest
from vinted import CatalogResult, classify
from vinted_regions import get_region


async def main():
    install_catalog_readiness(endpoints, document_catalog=True, compact_catalog=False,
                              api_catalog=os.environ.get("PROBE_CANDIDATE") == "1")
    for code, brand, locale in [("DE", "Nike", "de-DE"), ("FR", "Adidas", "fr-FR"),
                                ("IT", "Stone Island", "it-IT"), ("UK", "Amiri", "en-GB")]:
        if code not in os.environ.get("PROBE_REGIONS", "DE,FR,IT,UK").split(","):
            continue
        region = get_region(code)
        url = region.catalog_url(brand, 1, True)
        async for dep in utils.get_browser(None, None, None):
            dep = BrowserSession(dep.page, dep.context)
            try:
                params = {"search_text": brand, "order": "newest_first", "page": 1, "per_page": 96}
                source_ids = set()
                if os.environ.get("PROBE_COLD") != "1":
                    result = await endpoints.read_item(LinkRequest(
                        url=url, maxTimeout=20000, blockMedia=True, returnOnlyCookies=False), dep)
                    checked = classify(CatalogResult(code, url), region,
                                       result.solution.url, result.solution.status, result.solution.response)
                    print(json.dumps({"region": code, "phase": "document", "outcome": checked.outcome}), flush=True)
                    if checked.outcome != "page_loaded":
                        continue
                    body = result.solution.response
                    Path(f"/tmp/catalog-{code}-api-reference.html").write_text(body)
                    states = list(catalog_states(body))
                    if len(states) != 1:
                        raise ValueError("Ambiguous reference")
                    state = states[0]
                    source_ids = {str(item["id"]) for item in state["items"]}
                    if state["pagination"].get("time") is not None:
                        params["time"] = state["pagination"]["time"]
                    if os.environ.get("PROBE_CANDIDATE") == "1":
                        await asyncio.sleep(4)
                        await dep.page.close()
                        dep.page = await dep.context.new_page()
                        transport = {}
                        token = REQUEST_CATALOG_TRANSPORT.set(transport)
                        try:
                            result = await endpoints.read_item(LinkRequest(
                                url=url, maxTimeout=20000, blockMedia=True, returnOnlyCookies=False), dep)
                            checked = classify(CatalogResult(code, url), region,
                                result.solution.url, result.solution.status, result.solution.response)
                            print(json.dumps({"region": code, "phase": "candidate", "outcome": checked.outcome,
                                              "transport": transport.get("mode")}), flush=True)
                            if checked.outcome != "page_loaded":
                                continue
                            Path(f"/tmp/catalog-{code}-api-candidate.html").write_text(result.solution.response)
                        finally:
                            REQUEST_CATALOG_TRANSPORT.reset(token)
                for encoding in os.environ.get("PROBE_ENCODINGS", "gzip;br").split(";"):
                    await asyncio.sleep(4)
                    target = "https://api." + region.domain + "/svc-catalogue/items?" + urlencode(params)
                    start = time.monotonic()
                    reply = await dep.context.request.get(target, headers={
                        "Accept": "application/json", "Accept-Encoding": encoding,
                        "Platform": "web", "X-Next-App": "marketplace-web", "Locale": locale,
                        "Origin": region.base_url, "Referer": url}, max_redirects=0, timeout=15000)
                    try:
                        raw = await reply.body()
                        row = {"region": code, "phase": "api", "cold": os.environ.get("PROBE_COLD") == "1",
                               "status": reply.status, "mime": reply.headers.get("content-type"),
                               "requested_encoding": encoding, "encoding": reply.headers.get("content-encoding"),
                               "length": reply.headers.get("content-length"), "decoded_bytes": len(raw),
                               "etag": bool(reply.headers.get("etag")), "cache_control": reply.headers.get("cache-control"),
                               "retry_after": reply.headers.get("retry-after"), "seconds": time.monotonic() - start}
                        if reply.status == 200 and "application/json" in reply.headers.get("content-type", ""):
                            data = json.loads(raw)
                            items = data.get("items", [])
                            ids = {str(item["id"]) for item in items}
                            row.update(keys=list(data), items=len(items), unique_ids=len(ids),
                                       common_with_document=len(ids & source_ids), same_ids=ids == source_ids,
                                       pagination=data.get("pagination"), code=data.get("code"),
                                       has_warning=bool(data.get("replica_warning")))
                            Path(f"/tmp/catalog-{code}-api.json").write_bytes(raw)
                        print(json.dumps(row), flush=True)
                        if reply.status != 200:
                            break
                    finally:
                        await reply.dispose()
            except Exception as exc:
                print(json.dumps({"region": code, "error": type(exc).__name__}), flush=True)
            finally:
                await dep.context.close()
        await asyncio.sleep(4)


if __name__ == "__main__":
    asyncio.run(main())
