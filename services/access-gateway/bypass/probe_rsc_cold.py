"""Check the public compact response in fresh anonymous contexts, without bootstrap."""
# ruff: noqa: E402
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

sys.path[:0] = ["/opt/vinted", "/app"]
from src import utils
from vinted import catalog_context, is_region_url
from vinted_regions import get_region


def clean_tree(node):
    segment = "__PAGE__" if isinstance(node[0], str) and node[0].startswith("__PAGE__") else node[0]
    return [segment, {k: clean_tree(v) for k, v in node[1].items()}]


async def main():
    for code, brand in (("DE", "Nike"), ("FR", "Adidas")):
        full = Path(f"/tmp/catalog-{code}-rsc.txt").read_text()
        root = next(json.loads(line[2:]) for line in full.splitlines() if line.startswith("0:"))
        headers = {"RSC": "1", "Next-Router-State-Tree": quote(json.dumps(
            clean_tree(root["f"][0][0]), separators=(",", ":")), safe="")}
        region = get_region(code)
        url = region.catalog_url(brand, 1, True)
        async for dep in utils.get_browser(None, None, None):
            try:
                async with asyncio.timeout(20):
                    response = await dep.context.request.get(url, headers=headers, max_redirects=0, timeout=8000)
                    if response.status == 307:
                        location = urljoin(url, response.headers.get("location", ""))
                        if (is_region_url(location, region) and urlsplit(location).path == "/catalog"
                                and catalog_context(location) == catalog_context(url)
                                and set(parse_qs(urlsplit(location).query)) <= {"search_text", "page", "order", "_rsc"}):
                            await response.dispose()
                            await asyncio.sleep(4)
                            response = await dep.context.request.get(location, headers=headers, max_redirects=0, timeout=8000)
                    body = await response.body()
                    print(json.dumps({"region": code, "status": response.status,
                                      "mime": response.headers.get("content-type"), "bytes": len(body),
                                      "retry_after": response.headers.get("retry-after")}), flush=True)
                    if response.status == 200:
                        Path(f"/tmp/catalog-{code}-rsc-cold.txt").write_bytes(body)
                    await response.dispose()
            except Exception as exc:
                print(json.dumps({"region": code, "error": type(exc).__name__}), flush=True)
            finally:
                await dep.context.close()
        await asyncio.sleep(4)


if __name__ == "__main__":
    asyncio.run(main())
