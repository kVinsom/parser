"""Real browser regressions. Run ONLY in a disposable Docker --network none container.

No proxy, production environment, database, Telegram or actual Vinted requests.
All page responses are fulfilled locally by Playwright routing.
"""
import asyncio
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from browser_pool import SharedBrowser
from catalog_document import DocumentGate
from invisible_playwright.async_api import InvisiblePlaywright
from vinted_regions import get_region


def browser_processes():
    found = {"drivers": [], "displays": [], "browsers": []}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            state = (entry / "stat").read_text().split(") ", 1)[1].split()[0]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if state == "Z":
            continue
        if "run-driver" in command and "/node " in command:
            found["drivers"].append(int(entry.name))
        if command.split(" ", 1)[0].rsplit("/", 1)[-1] == "Xvfb":
            found["displays"].append(int(entry.name))
        if "-juggler-pipe" in command:
            found["browsers"].append(int(entry.name))
    return found


def catalog():
    state = {"items": [{"id": 101}], "pagination": {"current_page": 1, "per_page": 96},
             "uiState": "SUCCESS", "error": None, "replicaWarning": False}
    flight = json.dumps([1, "1:" + json.dumps(state) + "\n"])
    return ('<html><title>Catalog | Vinted</title><body>'
            '<div data-testid="product-item-id-101">'
            '<a data-testid="product-item-id-101--overlay-link" href="/items/101-shirt">Shirt</a>'
            '<p data-testid="product-item-id-101--price-text">10 €</p>'
            '<img data-testid="product-item-id-101--image--img" src="https://assets.example.test/photo.png">'
            '</div><a data-testid="catalog-pagination--next-page">Next</a>'
            '<script>self.__next_f=[]</script><script>self.__next_f.push(' + flight + ')</script>'
            '<script src="https://assets.example.test/application.js"></script></body></html>')


async def main():
    assert set(os.listdir("/sys/class/net")) == {"lo"}, "Docker --network none is required"
    assert not any(os.environ.get(key) for key in
                   ("PROXY_SERVER", "PROXY_USERNAME", "PROXY_PASSWORD")), "Do not pass production secrets"
    assert not any(browser_processes().values()), browser_processes()
    managers = []

    class Manager:
        def __init__(self):
            self.delegate = InvisiblePlaywright(headless=True, proxy=None, locale="fr-FR",
                                               timezone="Europe/Paris", humanize=True)
            self.closing, self.finish = asyncio.Event(), asyncio.Event()
            self.finish.set()
            managers.append(self)

        async def __aenter__(self):
            return await self.delegate.__aenter__()

        async def __aexit__(self, *args):
            self.closing.set()
            await self.finish.wait()
            return await self.delegate.__aexit__(*args)

    pool = SharedBrowser(Manager, max_uses=50)
    results = {}
    try:
        # A blocked driver teardown used to block the pool and be abandoned by
        # request cancellation, leaving the real Xvfb/Playwright processes behind.
        async with pool.page(key="FR") as session:
            await session.page.route("**/*", lambda route: route.abort())
            await session.page.goto("about:blank")
        assert len(browser_processes()["drivers"]) == 1, browser_processes()
        assert len(browser_processes()["displays"]) == 1, browser_processes()
        managers[0].finish.clear()
        pool.max_uses = 1

        async def rotate():
            async with pool.page(key="DE"):
                pass

        rotation = asyncio.create_task(rotate())
        await asyncio.wait_for(managers[0].closing.wait(), 10)
        rotation.cancel()
        try:
            await rotation
            raise AssertionError("Cancellation was lost")
        except asyncio.CancelledError:
            pass
        assert not pool.current.closed
        managers[0].finish.set()
        await pool.close()
        await asyncio.sleep(0.2)
        assert not any(browser_processes().values()), browser_processes()
        results["cancelled_rotation_leaked_processes"] = 0
    finally:
        for manager in managers:
            manager.finish.set()
        await pool.close()

    pool = SharedBrowser(Manager, max_uses=50)
    try:
        for code, brand, hold in (("FR", "Nike", 2), ("DE", "Adidas", 8),
                                  ("IT", "Stone Island", 8), ("UK", "Amiri", 8)):
            region = get_region(code)
            url = region.catalog_url(brand, 1, True)
            body = catalog()
            scripts = []
            async with pool.page(key=code) as session:
                async def fixture(route, _request, *, body=body, scripts=scripts):
                    if route.request.resource_type == "document":
                        await route.fulfill(status=200, content_type="text/html", body=body)
                    elif route.request.url.endswith("application.js"):
                        scripts.append(True)
                        await route.fulfill(status=200, content_type="application/javascript",
                            body="window.offlineHydrated=true; const end=performance.now()+200; while(performance.now()<end){}")
                    else:
                        await route.abort()

                await session.page.route("**/*", fixture)
                gate = DocumentGate(hold_seconds=hold, remaining=lambda: 10000)
                await gate.attach(session.page)
                response = await session.page.goto(url, wait_until="commit", timeout=10000)

                async def slow_body(response=response):
                    await asyncio.sleep(2.2)
                    return await response.body()

                raw = await gate.read(SimpleNamespace(status=response.status, url=response.url,
                    header_value=response.header_value, body=slow_body), url, region)
                gate.release(accepted=raw is not None)
                await session.page.wait_for_load_state("load", timeout=5000)
                if hold == 2:
                    assert raw is None and scripts == [True], (code, raw, scripts)
                else:
                    assert raw == body and scripts == [], (code, raw, scripts)
                results[code] = {"hold_seconds": hold, "full_application_loads": len(scripts),
                                 "complete_document": raw is not None}
                assert len(browser_processes()["drivers"]) == 1, browser_processes()
        assert pool.active == 0
    finally:
        await pool.close()
    await asyncio.sleep(0.2)
    results["remaining_processes"] = browser_processes()
    assert not any(results["remaining_processes"].values()), results
    # Exercise the real pinned FastAPI entry point, including its lazy router.
    # Repeated cold health probes must not even create the browser manager.
    with tempfile.TemporaryFile(mode="w+") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "/opt/vinted/byparr_runtime.py", stdout=log, stderr=log,
            env={**os.environ, "BROWSER_LOCALE": "fr-FR"})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def health():
                with opener.open("http://127.0.0.1:8191/health", timeout=0.5) as response:
                    return json.load(response)

            startup_at = asyncio.get_running_loop().time()
            for attempt in range(150):
                try:
                    payload = await asyncio.to_thread(health)
                    break
                except OSError:
                    if process.returncode is not None or attempt == 149:
                        log.seek(0)
                        raise AssertionError(log.read()) from None
                    await asyncio.sleep(0.2)
            results["startup_health_wait_ms"] = round(
                (asyncio.get_running_loop().time() - startup_at) * 1000, 1)
            for _ in range(4):
                payload = await asyncio.to_thread(health)
                assert payload["userAgent"] == "", payload
                assert not any(browser_processes().values()), browser_processes()
            results["cold_health_browser_launches"] = 0
            source_started, source_closed = asyncio.Event(), asyncio.Event()

            async def stalled_source(reader, writer):
                try:
                    await reader.readuntil(b"\r\n\r\n")
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                                 b"Content-Length: 1000000\r\n\r\n<html><title>Offline fixture</title>")
                    await writer.drain()
                    source_started.set()
                    await reader.read()
                finally:
                    source_closed.set()
                    writer.close()
                    await writer.wait_closed()

            source = await asyncio.start_server(stalled_source, "127.0.0.1", 0)
            source_port = source.sockets[0].getsockname()[1]
            _, client = await asyncio.open_connection("127.0.0.1", 8191)
            try:
                payload = json.dumps({"cmd": "request.get", "maxTimeout": 60000,
                    "url": f"http://127.0.0.1:{source_port}/stall",
                    "blockMedia": True, "returnOnlyCookies": False}).encode()
                client.write(b"POST /v1 HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
                await client.drain()
                await asyncio.wait_for(source_started.wait(), 45)
                cancelled_at = asyncio.get_running_loop().time()
                client.close()
                await client.wait_closed()
                await asyncio.wait_for(source_closed.wait(), 10)
                results["disconnect_to_browser_source_close_ms"] = round(
                    (asyncio.get_running_loop().time() - cancelled_at) * 1000, 1)
            finally:
                client.close()
                await client.wait_closed()
                source.close()
                await source.wait_closed()
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 20)
    results["remaining_processes"] = browser_processes()
    assert not any(results["remaining_processes"].values()), results
    print(json.dumps(results, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
