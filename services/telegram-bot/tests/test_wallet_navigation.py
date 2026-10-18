"""Real Chromium/HTTP tests; no route mocks that could hide cache or guard behavior."""

from collections import Counter
from contextlib import asynccontextmanager, suppress

import pytest
from aiohttp import web
from playwright.async_api import Error, async_playwright

from vpars_bot.wallet import WalletError
from vpars_bot.wallet_navigation import background_navigation_guard, navigation_guard


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


async def test_guard_retains_http_cache_blocks_external_navigation_but_allows_child_frames():
    requests = Counter()

    async def foreign(request):
        requests["foreign:" + request.path] += 1
        return web.Response(text="Frame loaded", content_type="text/html")

    async with server(foreign) as other:
        async def local(request):
            requests[request.path] += 1
            if request.path == "/cached.js":
                return web.Response(text="window.ready = true;", content_type="text/javascript",
                                    headers={"Cache-Control": "public, max-age=3600"})
            if request.path == "/redirect":
                raise web.HTTPFound(other + "/escaped")
            return web.Response(text=f'<script src="/cached.js"></script><iframe src="{other}/frame"></iframe>',
                                content_type="text/html")

        async with server(local) as origin, async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()
                async with navigation_guard(page, origin):
                    for path in ("/first", "/second"):
                        await page.goto(origin + path)
                        assert await page.evaluate("window.ready")
                    assert requests["/cached.js"] == 1
                    assert requests["foreign:/frame"] >= 1
                    for url in (other + "/escaped", origin + "/redirect"):
                        with pytest.raises(Error):
                            await page.goto(url)
                    assert requests["foreign:/escaped"] == 0
            finally:
                await browser.close()


async def test_background_guard_preserves_cache_post_bodies_and_child_frames(monkeypatch):
    requests = Counter()
    bodies = []

    async def foreign(request):
        requests["foreign:" + request.path] += 1
        return web.Response(text="Frame loaded", content_type="text/html")

    async with server(foreign) as other:
        async def local(request):
            requests[request.path] += 1
            if request.path == "/cached.js":
                return web.Response(text="window.ready=true;", content_type="text/javascript",
                                    headers={"Cache-Control": "public, max-age=3600"})
            if request.path == "/checkout/build":
                bodies.append(await request.json())
                return web.json_response({"total": "13.50", "currency": "EUR"})
            return web.Response(text=f'<script src="/cached.js"></script><iframe src="{other}/frame"></iframe>',
                                content_type="text/html")

        async with server(local) as origin, async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()

                async def no_interception(*args, **kwargs):
                    pytest.fail("Background guard must not install CDP Fetch or Playwright routing")

                monkeypatch.setattr(page.context, "new_cdp_session", no_interception)
                monkeypatch.setattr(page, "route", no_interception)
                async with background_navigation_guard(page, origin):
                    for path in ("/first", "/second"):
                        await page.goto(origin + path)
                        assert await page.evaluate("window.ready")
                    result = await page.evaluate('''async () => (await fetch('/checkout/build', {
                        method:'POST', headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({item:'123', delivery:'saved'})})).json()''')
                    assert result == {"total": "13.50", "currency": "EUR"}
                    assert bodies == [{"item": "123", "delivery": "saved"}]
                    assert requests["/cached.js"] == 1
                    assert requests["foreign:/frame"] >= 1
            finally:
                await browser.close()


@pytest.mark.parametrize("redirect", [False, True])
async def test_background_guard_closes_foreign_main_page_and_fails_checkout(redirect):
    async def foreign(request):
        return web.Response(text="External page", content_type="text/html")

    async with server(foreign) as other:
        async def local(request):
            if request.path == "/redirect":
                raise web.HTTPFound(other + "/external")
            return web.Response(text="Checkout", content_type="text/html")

        async with server(local) as origin, async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()
                with pytest.raises(WalletError, match="ManualAction"):
                    async with background_navigation_guard(page, origin):
                        await page.goto(origin + "/checkout")
                        async with page.expect_event("close"):
                            with suppress(Error):
                                await page.goto(origin + "/redirect" if redirect else other + "/external")
                assert page.is_closed()
            finally:
                await browser.close()
