"""Compare real Firefox readiness using local responses, with --network=none."""

import asyncio
import hashlib
import json
import os
import time
from types import SimpleNamespace

from browser_network import install_browser_network_policy
from byparr_runtime import BrowserSession, install_catalog_readiness, item_data_ready
from traffic_policy import install_traffic_policy


async def main():
    from invisible_playwright.async_api import InvisiblePlaywright
    from playwright.async_api import Error
    from playwright.async_api import TimeoutError as BrowserTimeout

    os.environ["INVPW_TRUE_HEADLESS"] = "1"
    utils = SimpleNamespace(InvisiblePlaywright=InvisiblePlaywright)
    policy = install_browser_network_policy(utils)
    results = []
    try:
        async with utils.InvisiblePlaywright(headless=True, humanize=True, locale="fr-FR", timezone="UTC") as browser:
            context = await browser.new_context()
            for market in ("fr", "it", "de", "co.uk"):
                for brand in ("Nike", "Gucci"):
                    page = await context.new_page()
                    url = f"https://www.vinted.{market}/items/123-shirt"
                    record = {"item_id": "123", "photos": [{"url": "https://images1.vinted.net/photo.webp"}],
                              "description": brand + " Good condition", "seller_id": "45",
                              "feedback_count": 10, "feedback_reputation": 0.9}
                    flight = "self.__next_f.push(" + json.dumps([1, "1:" + json.dumps(record) + "\n"]) + ")"
                    body = ('<html><head><title>' + brand + ' | Vinted</title>'
                            '<script>self.__next_f=[];</script><script async src="/latency-fixture.js"></script>'
                            '</head><body><img data-testid="item-photo-1">'
                            '<div data-testid="item-attributes-upload_date">Just now</div></body></html>')
                    script = ("const data=document.createElement('script');data.textContent=" + json.dumps(flight)
                              + ";document.body.appendChild(data);"
                              "setInterval(()=>fetch('/local-background').catch(()=>{}),75);")
                    local_requests = []

                    async def original_setup(request, dep, market=market, body=body,
                                             script=script, local_requests=local_requests):
                        async def fixture(route):
                            path = route.request.url.partition("vinted." + market)[2]
                            local_requests.append(path)
                            try:
                                if path == "/items/123-shirt":
                                    await route.fulfill(status=200, content_type="text/html", body=body)
                                elif path == "/latency-fixture.js":
                                    await asyncio.sleep(0.15)
                                    await route.fulfill(status=200, content_type="application/javascript", body=script)
                                elif path == "/local-background":
                                    await asyncio.sleep(0.2)
                                    await route.fulfill(status=200, content_type="application/json", body="{}")
                                else:
                                    raise AssertionError("Unexpected fixture request")
                            except Error:
                                pass  # Closing the completed page cancels local fixture work.
                        await dep.page.route("**/*", fixture)

                    async def networkidle(dep, timer):
                        try:
                            await dep.page.wait_for_load_state("networkidle", timeout=remaining(timer))
                        except BrowserTimeout:
                            pass  # Same tolerated post-load stall as production Byparr.

                    def remaining(timer):
                        return max(1, int((timer.deadline - time.monotonic()) * 1000))

                    async def no_challenge(page):
                        return False

                    endpoints = SimpleNamespace(setup_routes=original_setup,
                        _navigate_and_solve=None, remaining_ms=remaining,
                        challenge_present=no_challenge, _wait_for_networkidle=networkidle)
                    install_traffic_policy(endpoints)
                    install_catalog_readiness(endpoints, document_catalog=True)
                    dep = BrowserSession(page, context)
                    request = SimpleNamespace(url=url, block_media=True, return_only_cookies=False)
                    await endpoints.setup_routes(request, dep)
                    started = time.monotonic()
                    timer = SimpleNamespace(deadline=started + 1.8)
                    result = await asyncio.wait_for(endpoints._navigate_and_solve(dep, request, timer), timeout=3)
                    elapsed = time.monotonic() - started
                    assert result[0] is False and result[2].status == 200
                    assert item_data_ready(result[1], "123")
                    assert result[1].count('data-testid="item-photo-1"') == 1
                    assert page.url == url
                    assert local_requests.count("/items/123-shirt") == 1
                    results.append({"market": market, "brand": brand, "elapsed_ms": round(elapsed * 1000, 1),
                                    "proof_sha256": hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
                                    "external_connections": 0})
                    await page.close()
            await context.close()
        print(json.dumps(results), flush=True)
    finally:
        policy.close()
        policy.restore_geo()


if __name__ == "__main__":
    asyncio.run(main())
