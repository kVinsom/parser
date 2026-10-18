"""Bounded read-only comparison of initial HTML and settled catalog content.

Run via stdin in Byparr. Emits counts/paths, never cookies or response bodies.
Raw captures remain local for comparison with the production parser.
"""
# ruff: noqa: E402
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

sys.path[:0] = ["/opt/vinted", "/tmp", "/app"]
from byparr_runtime import REQUEST_CATALOG_TRANSPORT, install_catalog_readiness
from src import endpoints, utils
from src.models import LinkRequest
from static_cache import StaticAssetCache, install_static_cache
from traffic_policy import install_traffic_policy
from vinted import CatalogResult, catalog_context, classify, is_region_url
from vinted_regions import get_region


async def main():
    install_catalog_readiness(endpoints, document_catalog=os.environ.get("PROBE_DOCUMENT") == "1",
                              compact_catalog=os.environ.get("PROBE_COMPACT") == "1")
    install_traffic_policy(endpoints)
    install_static_cache(endpoints, StaticAssetCache(disk_dir="/var/cache/vinted-static"))
    cases = [("DE", "Nike"), ("FR", "Adidas"), ("IT", "Stone Island"), ("UK", "Amiri")]
    if os.environ.get("PROBE_COMPACT") == "1":
        cases *= 2
    for code, brand in cases:
        if code not in os.environ.get("PROBE_REGIONS", "DE,FR,IT,UK").split(","):
            continue
        region = get_region(code)
        suffix = os.environ.get("PROBE_SUFFIX", "")
        url = region.catalog_url(brand, 1, True) + suffix
        tag = code + ("-small" if suffix else "")
        async for dep in utils.get_browser(None, None, None):
            responses = []
            transport = {}
            token = REQUEST_CATALOG_TRANSPORT.set(transport)
            started = time.monotonic()
            dep.page.on("response", responses.append)
            try:
                async with asyncio.timeout(45):
                    result = await endpoints.read_item(LinkRequest(
                        url=url, maxTimeout=20000, blockMedia=True, returnOnlyCookies=False), dep)
                    checked = classify(CatalogResult(code, url), region,
                                       result.solution.url, result.solution.status, result.solution.response)
                    print(json.dumps({"region": code, "mode": tag, "outcome": checked.outcome,
                                      "transport": transport.get("mode", "rendered"),
                                      "seconds": time.monotonic() - started}), flush=True)
                    if checked.outcome != "page_loaded":
                        continue
                    main = next(r for r in responses if r.request.resource_type == "document"
                                and urlsplit(r.url).path == "/catalog")
                    raw = await main.text()
                    rendered = result.solution.response
                    Path(f"/tmp/catalog-{tag}-raw.html").write_text(raw)
                    Path(f"/tmp/catalog-{tag}-dom.html").write_text(rendered)
                    def ids(body):
                        return set(re.findall(r'data-testid="product-item-id-([0-9]+)"', body))
                    headers = await main.all_headers()
                    print(json.dumps({"region": code, "raw_bytes": len(raw.encode()),
                        "dom_bytes": len(rendered.encode()), "raw_cards": len(ids(raw)),
                        "dom_cards": len(ids(rendered)), "same_cards": ids(raw) == ids(rendered),
                        "encoding": headers.get("content-encoding"),
                        "length": headers.get("content-length"),
                        "public_data_requests": sorted(set(urlsplit(r.url).path for r in responses
                            if r.request.resource_type in ("xhr", "fetch")
                            and urlsplit(r.url).hostname in region.hosts
                            and urlsplit(r.url).path.startswith("/api/")))}), flush=True)
                    if os.environ.get("PROBE_VARIANTS") in ("1", "rsc", "partial"):
                        for variant, target, headers in (
                            ("per24", url + "&per_page=24", {}),
                            ("rsc", url, {"RSC": "1"}),
                        ):
                            if os.environ.get("PROBE_VARIANTS") in ("rsc", "partial") and variant != "rsc":
                                continue
                            if os.environ.get("PROBE_VARIANTS") == "partial":
                                full = Path(f"/tmp/catalog-{code}-rsc.txt").read_text()
                                root = next(json.loads(line[2:]) for line in full.splitlines() if line.startswith("0:"))
                                def router_tree(node):
                                    segment = "__PAGE__" if isinstance(node[0], str) and node[0].startswith("__PAGE__") else node[0]
                                    return [segment, {k: router_tree(v) for k, v in node[1].items()}]
                                tree = router_tree(root["f"][0][0])
                                headers["Next-Router-State-Tree"] = quote(json.dumps(tree, separators=(",", ":")), safe="")
                            await asyncio.sleep(4)
                            # Diagnostic requests must not be intercepted by the
                            # candidate page's accepted document-only resource gate.
                            reply = await dep.context.request.get(target, headers=headers,
                                                                   max_redirects=0, timeout=8000)
                            if reply.status == 307 and variant == "rsc":
                                location = urljoin(target, reply.headers.get("location", ""))
                                keys = set(parse_qs(urlsplit(location).query))
                                if (is_region_url(location, region) and urlsplit(location).path == "/catalog"
                                        and catalog_context(location) == catalog_context(url)
                                        and keys <= {"search_text", "page", "order", "_rsc"}):
                                    await reply.dispose()
                                    await asyncio.sleep(4)
                                    reply = await dep.context.request.get(location, headers=headers,
                                                                         max_redirects=0, timeout=8000)
                            body = await reply.text()
                            data = {"status": reply.status, "mime": reply.headers.get("content-type"),
                                    "retry_after": reply.headers.get("retry-after")}
                            await reply.dispose()
                            data.update(region=code, variant=variant, bytes=len(body.encode()),
                                        cards=len(ids(body)))
                            if os.environ.get("PROBE_VARIANTS") == "partial":
                                variant = data["variant"] = "rsc-partial"
                            print(json.dumps(data), flush=True)
                            if data["status"] != 200:
                                break
                            Path(f"/tmp/catalog-{code}-{variant}.txt").write_text(body)
            except Exception as exc:
                print(json.dumps({"region": code, "error": type(exc).__name__,
                    "message": re.sub(r'https?://\S+', '[url]', str(exc).splitlines()[0])[:180]}), flush=True)
            finally:
                REQUEST_CATALOG_TRANSPORT.reset(token)
                await dep.context.close()
        await asyncio.sleep(4)


if __name__ == "__main__":
    asyncio.run(main())
