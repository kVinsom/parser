"""Exercise native Firefox routing using local fake proxies, with no Internet.

Run inside the Byparr image with Docker --network none. Neither fake proxy
opens an upstream connection. No live credentials or source requests are used.
"""

import asyncio
import io
import ipaddress
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from browser_network import (
    BACKGROUND_DOWNLOAD_HOSTS,
    COMPONENT_PREFS,
    install_browser_network_policy,
    protected_factory,
)


async def verify_protocol(protocol):
    from invisible_playwright.async_api import InvisiblePlaywright
    from playwright.async_api import Error

    recorded = []
    handlers = set()

    async def proxy(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            async with asyncio.timeout(3):
                if protocol == "http":
                    header = await reader.readuntil(b"\r\n\r\n")
                    method, target, _ = (
                        header.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                    )
                    if method == "CONNECT":
                        recorded.append(target.rsplit(":", 1)[0].lower())
                    writer.write(
                        b"HTTP/1.1 502 Test proxy has no upstream\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                else:
                    version, count = await reader.readexactly(2)
                    assert version == 5
                    await reader.readexactly(count)
                    writer.write(b"\x05\x02")
                    await writer.drain()
                    version, length = await reader.readexactly(2)
                    assert version == 1
                    assert (await reader.readexactly(length)).decode() == "test-user"
                    length = (await reader.readexactly(1))[0]
                    assert (
                        await reader.readexactly(length)
                    ).decode() == "test-password"
                    writer.write(b"\x01\x00")
                    await writer.drain()
                    version, command, _, address_type = await reader.readexactly(4)
                    assert version == 5 and command == 1
                    if address_type == 3:
                        length = (await reader.readexactly(1))[0]
                        host = (await reader.readexactly(length)).decode("ascii")
                    else:
                        host = str(
                            ipaddress.ip_address(
                                await reader.readexactly(4 if address_type == 1 else 16)
                            )
                        )
                    await reader.readexactly(2)
                    recorded.append(host.lower())
                    writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    executable = next(Path("/cache/invisible-playwright").glob("*/firefox"))
    options = {
        "headless": True,
        "humanize": False,
        "locale": "en-GB",
        "timezone": "UTC",
        "binary_path": str(executable),
        "proxy": {
            "server": f"{protocol}://127.0.0.1:{port}",
            "username": "test-user",
            "password": "test-password",
        },
    }
    prefs = protected_factory(InvisiblePlaywright)(**options)._build_prefs()
    assert all(prefs[key] == value for key, value in COMPONENT_PREFS.items())
    utils = SimpleNamespace(InvisiblePlaywright=InvisiblePlaywright)
    policy = install_browser_network_policy(utils)
    manager = utils.InvisiblePlaywright(**options)
    denied = [
        *BACKGROUND_DOWNLOAD_HOSTS,
        "firefox.settings.services.mozilla.com",
        "incoming.telemetry.mozilla.org",
        "www.google-analytics.com",
        "outside.example",
        "ocsp.digicert.com",
        "firefox.settings.services.mozilla.com.evil",
        "metrics.vinted.lt",
        "images1.vinted.net",
        "images2.vinted.net",
        "marketplace-web-assets.vinted.com",
    ]
    try:
        # Explicit UTC now needs no IP echo, including in the real launcher.
        async with manager as browser:
            context = await browser.new_context()
            page = await context.new_page()
            # Byparr's old page routes can continue_(), skipping context routes.
            # Exercise the production page guard ahead of that route as well.
            from traffic_policy import install_traffic_policy

            async def original_setup(request, dep):
                async def continue_original(route):
                    await route.continue_()

                await dep.page.route("**/*", continue_original)

            endpoints = SimpleNamespace(setup_routes=original_setup)
            install_traffic_policy(endpoints, skip_analytics=False)
            from direct_assets import DirectAssetFetcher
            from static_cache import StaticAssetCache, install_static_cache

            cache = StaticAssetCache(direct_fetcher=DirectAssetFetcher())
            install_static_cache(endpoints, cache)
            await endpoints.setup_routes(None, SimpleNamespace(page=page))
            for host in denied:
                try:
                    await page.goto(
                        f"https://{host}/local-network-policy-check", timeout=2500
                    )
                except Error:
                    pass
                assert host not in recorded, (
                    f"Blocked download reached the {protocol} proxy"
                )
            for host, brand in [
                ("www.vinted.fr", "Nike"),
                ("www.vinted.it", "Gucci"),
                ("www.vinted.de", "Nike"),
                ("www.vinted.co.uk", "Gucci"),
            ]:
                try:
                    await page.goto(
                        f"https://{host}/catalog?search_text={brand}", timeout=2500
                    )
                except Error:
                    pass
                assert host in recorded, (
                    f"Normal catalog routing was lost for {host}"
                )
            await page.goto("about:blank")
            await page.evaluate("""async () => {
                await Promise.all(['images1.vinted.net', 'images2.vinted.net'].map(host =>
                    fetch('https://' + host + '/photo.webp', {mode: 'no-cors'})
                        .catch(() => null)));
            }""")
            assert not {"images1.vinted.net", "images2.vinted.net"}.intersection(recorded)
            asset_url = "https://marketplace-web-assets.vinted.com/_next/static/chunks/local.js"

            class AssetReply(io.BytesIO):
                status = 200

                def __init__(self, body):
                    super().__init__(body)
                    self.headers = {"cache-control": "public, immutable, max-age=3600",
                                    "content-type": "application/javascript"}

                def geturl(self):
                    return asset_url

            with patch("direct_assets.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = lambda *args, **kwargs: AssetReply(b"window.assetLoads=(window.assetLoads||0)+1;")
                for _ in range(2):
                    await asyncio.wait_for(page.evaluate("""url => new Promise((resolve, reject) => {
                        const script = document.createElement('script');
                        script.src = url;
                        script.onload = resolve;
                        script.onerror = () => reject(new Error('asset not loaded'));
                        document.head.appendChild(script);
                    })""", asset_url), timeout=5)
                assert opener.call_count == 1
                assert opener.call_args.args[0].proxies == {}
                assert await page.evaluate("window.assetLoads") == 2
            assert cache.hits == 1
            assert "marketplace-web-assets.vinted.com" not in recorded
            await context.close()
        direct_services = await verify_direct_services(policy, options)
        assert not set(denied).intersection(recorded)
        return {
            "protocol": protocol,
            "blocked_hosts_reached_proxy": 0,
            "denied_host_cases": len(denied),
            "catalog_hosts_reached_local_proxy": 4,
            "service_hosts_reached_direct_listener": direct_services,
            "service_hosts_reached_proxy": len(set(direct_services).intersection(recorded)),
            "photo_fetch_cases": 2,
            "photo_hosts_reached_proxy": 0,
            "direct_asset_executions": 2,
            "direct_asset_downloads": 1,
            "asset_hosts_reached_proxy": 0,
            "external_connections": 0,
        }
    finally:
        policy.close()
        policy.restore_geo()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def verify_direct_services(policy, options):
    """Exercise native hostname bypass without the page guard, only on loopback.

    An ordinary production context blocks these navigations. This raw test
    context exercises the proxy choice also used by Firefox's internal channels.
    """
    from invisible_playwright.async_api import InvisiblePlaywright

    seen = []
    tasks = set()

    async def receive(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            async with asyncio.timeout(3):
                header = await reader.readuntil(b"\r\n\r\n")
                host = next(line for line in header.decode().split("\r\n")
                            if line.lower().startswith("host:"))
                seen.append(host.split(":", 2)[1].strip())
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                             b"Content-Length: 6\r\nConnection: close\r\n\r\ndirect")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    hosts = ["firefox.settings.services.mozilla.com", "ocsp.digicert.com"]
    token, proxy = policy.register(options["proxy"])
    try:
        async with protected_factory(InvisiblePlaywright)(
            **{**options, "proxy": proxy,
               "extra_prefs": {"network.dns.localDomains": ",".join(hosts)}}
        ) as browser:
            context = await browser.new_context()
            page = await context.new_page()
            for host in hosts:
                response = await page.goto(f"http://{host}:{port}/service", timeout=5000)
                assert response.status == 200
                assert await page.locator("body").inner_text() == "direct"
            await context.close()
        assert set(hosts).issubset(seen)
        return hosts
    finally:
        policy.unregister(token)
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    os.environ["INVPW_TRUE_HEADLESS"] = "1"
    for protocol in ("http", "socks5"):
        print(json.dumps(await verify_protocol(protocol)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
