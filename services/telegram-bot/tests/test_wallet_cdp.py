"""Dedicated CDP sessions and cookie continuity; all browser traffic is synthetic."""

import json
import socket
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_wallet_browser import browser_page as browser_page
from test_wallet_sessions import key

from vpars_bot.wallet import REGIONS, WalletError
from vpars_bot.wallet_browser import cdp_endpoint, wallet_context, wallet_page
from vpars_bot.wallet_sessions import merge_session_cookies


@pytest.mark.parametrize("user_id,region", [(101, "DE"), (101, "FR"), (202, "DE"), (202, "FR")])
async def test_saved_login_profile_reopens_headless_without_cdp(tmp_path, monkeypatch, user_id, region):
    from playwright.async_api import async_playwright

    import vpars_bot.wallet_browser as module

    monkeypatch.setattr(module, "ROOT", tmp_path)
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    profile = tmp_path / ".wallet-profiles" / f"cdp-chrome-{user_id}-{region}"
    binding = tmp_path / ".wallet-profiles" / ".cdp-bindings" / str(port)
    binding.mkdir(parents=True)
    (binding / "owner.json").write_text(json.dumps({"user_id": user_id, "region": region}))
    config = {"browser_channel": "chrome", "browser_cdp_url": f"http://127.0.0.1:{port}"}
    origin = "https://" + REGIONS[region][0]
    async with async_playwright() as driver:
        context = await driver.chromium.launch_persistent_context(str(profile), channel="chrome", headless=True)
        original_languages = await context.pages[0].evaluate("navigator.languages")
        await context.add_cookies([{"name": "test_saved_session", "value": str(user_id), "url": origin,
                                    "expires": time.time() + 3600}])
        await context.close()
        async with wallet_context(driver, user_id, region, config, background=True,
                                  locale="en-GB", service_workers="block") as restored:
            assert (await restored.cookies(origin))[0]["value"] == str(user_id)
            assert "HeadlessChrome" in await restored.pages[0].evaluate("navigator.userAgent")
            assert await restored.pages[0].evaluate("navigator.languages") == original_languages
        with pytest.raises(WalletError, match="BrowserAlreadyBound"):
            async with wallet_context(driver, user_id + 1, region, config, background=True):
                pytest.fail("Cannot reuse another person's session")


async def test_background_refuses_open_manual_browser_without_closing_it(tmp_path, monkeypatch):
    import asyncio

    import vpars_bot.wallet_browser as module

    monkeypatch.setattr(module, "ROOT", tmp_path)
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        binding = tmp_path / ".wallet-profiles" / ".cdp-bindings" / str(port)
        binding.mkdir(parents=True)
        (binding / "owner.json").write_text(json.dumps({"user_id": 101, "region": "DE"}))
        driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock()))
        config = {"browser_channel": "chrome", "browser_cdp_url": f"http://127.0.0.1:{port}"}
        with pytest.raises(WalletError, match="ProfileBusy"):
            async with wallet_context(driver, 101, "DE", config, background=True):
                pytest.fail("Manual browser owns the profile")
        driver.chromium.launch_persistent_context.assert_not_awaited()
        assert server.is_serving()


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:9222", "http://localhost:9222", "http://0.0.0.0:9222",
    "http://remote.test:9222", "http://127.0.0.1:80", "http://127.0.0.1:99999",
    "http://127.0.0.1:9222/json", "http://user@127.0.0.1:9222",
    "http://127.0.0.1:9222?token=secret", "ws://127.0.0.1:9222/devtools/browser/id",
    "http://127.0.0.1:9222\n", "", [], True,
])
def test_only_explicit_local_cdp_endpoints(url):
    with pytest.raises(WalletError, match="BrowserConnectionInvalid"):
        cdp_endpoint({"browser_cdp_url": url})


def test_normalize_endpoint():
    assert cdp_endpoint({}) is None
    assert cdp_endpoint({"browser_cdp_url": "http://127.0.0.1:09222/"}) == "http://127.0.0.1:9222"


@pytest.mark.parametrize("other", [(202, "DE"), (101, "FR")])
async def test_endpoint_binding_prevents_cross_user_or_country_reuse(tmp_path, monkeypatch, other):
    import vpars_bot.wallet_browser as module

    monkeypatch.setattr(module, "ROOT", tmp_path)
    context = SimpleNamespace(close=AsyncMock())
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    connect = AsyncMock(return_value=browser)
    driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    config = {"browser_cdp_url": "http://127.0.0.1:9222"}
    async with wallet_context(driver, 101, "DE", config) as result:
        assert result is context
    with pytest.raises(WalletError, match="BrowserAlreadyBound"):
        async with wallet_context(driver, *other, config):
            pytest.fail("Another account must never obtain this browser")
    assert connect.await_count == 1
    context.close.assert_not_awaited()
    browser.close.assert_not_awaited()


async def test_failed_connect_does_not_bind_or_launch_fallback(tmp_path, monkeypatch):
    import vpars_bot.wallet_browser as module

    monkeypatch.setattr(module, "ROOT", tmp_path)
    driver = SimpleNamespace(chromium=SimpleNamespace(
        connect_over_cdp=AsyncMock(side_effect=RuntimeError("secret upstream detail")),
        launch_persistent_context=AsyncMock(),
    ))
    with pytest.raises(WalletError, match="^BrowserUnavailable$"):
        async with wallet_context(driver, 101, "DE", {"browser_cdp_url": "http://127.0.0.1:9222"}):
            pass
    assert not list(tmp_path.rglob("owner.json"))
    driver.chromium.launch_persistent_context.assert_not_awaited()


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("owner", ["101", "202"])
async def test_refresh_keeps_browser_state_and_replaces_only_auth(browser_page, region, owner):
    host = REGIONS[region][0]
    origin = "https://" + host
    context = browser_page.context
    def cookie(name, value):
        return {"name": name, "value": value, "domain": "." + host, "path": "/"}
    await context.add_cookies([
        cookie("access_token_web", key(subject=owner)),
        cookie("refresh_token_web", "old"), cookie("datadome", "solved-state"),
        cookie("anon_id", "existing-device"), cookie("consent", "accepted"),
        {"name": "refresh_token_web", "value": "other-region", "url": "https://unrelated.test"},
    ])
    await merge_session_cookies(context, region, [
        cookie("access_token_web", key(subject=owner)), cookie("refresh_token_web", "new"),
        cookie("anon_id", "temporary-device"), cookie("datadome", "temporary-state"),
        cookie("new_site_cookie", "new-value"),
    ])
    values = {c["name"]: c["value"] for c in await context.cookies(origin)}
    assert values["refresh_token_web"] == "new"
    assert values["datadome"] == "solved-state"
    assert values["anon_id"] == "existing-device"
    assert values["consent"] == "accepted"
    assert values["new_site_cookie"] == "new-value"
    assert (await context.cookies("https://unrelated.test"))[0]["value"] == "other-region"


async def test_refresh_cannot_replace_different_account(browser_page):
    context = browser_page.context
    await context.add_cookies([{"name": "access_token_web", "value": key(subject="101"),
                               "url": "https://www.vinted.de"}])
    before = await context.cookies()
    with pytest.raises(WalletError, match="BrowserAccountMismatch"):
        await merge_session_cookies(context, "DE", [
            {"name": "access_token_web", "value": key(subject="202"),
             "domain": ".www.vinted.de", "path": "/"},
        ])
    assert await context.cookies() == before


async def test_refresh_rejects_cross_region_cookie_without_mutation(browser_page):
    before = await browser_page.context.cookies()
    with pytest.raises(WalletError, match="SessionResponseInvalid"):
        await merge_session_cookies(browser_page.context, "DE", [
            {"name": "access_token_web", "value": key(subject="101"),
             "domain": ".www.vinted.fr", "path": "/"},
        ])
    assert await browser_page.context.cookies() == before


async def test_first_login_accepts_guest_cookie_and_parent_domain(browser_page):
    context = browser_page.context
    await context.add_cookies([
        {"name": "access_token_web", "value": key(subject=None), "url": "https://www.vinted.de"},
        {"name": "refresh_token_web", "value": "old-variant", "url": "https://www.vinted.de"},
        {"name": "consent", "value": "saved", "domain": ".vinted.de", "path": "/"},
    ])
    await merge_session_cookies(context, "DE", [
        {"name": "access_token_web", "value": key(subject="101"), "domain": ".www.vinted.de", "path": "/"},
        {"name": "refresh_token_web", "value": "new", "domain": ".www.vinted.de", "path": "/"},
        {"name": "datadome", "value": "new-state", "domain": ".vinted.de", "path": "/"},
    ])
    cookies = await context.cookies("https://www.vinted.de")
    assert len([c for c in cookies if c["name"] == "access_token_web"]) == 1
    assert len([c for c in cookies if c["name"] == "refresh_token_web"]) == 1
    assert {c["name"]: c["value"] for c in cookies}["consent"] == "saved"


async def test_real_cdp_disconnect_preserves_session_tabs_and_manual_challenge(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright

    import vpars_bot.wallet_browser as module

    monkeypatch.setattr(module, "ROOT", tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = {"browser_cdp_url": f"http://127.0.0.1:{port}"}
    async with async_playwright() as owner_driver:
        context = await owner_driver.chromium.launch_persistent_context(
            str(tmp_path / "browser"), headless=True, chromium_sandbox=True,
            args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"],
        )
        try:
            await context.route("**/*", lambda route: route.fulfill(body="fixture"))
            original = context.pages[0]
            await original.goto("https://www.vinted.de/")
            await context.add_cookies([{"name": "datadome", "value": "existing", "url": original.url}])
            async with async_playwright() as driver:
                async with wallet_context(driver, 101, "DE", config) as attached:
                    async with wallet_page(attached, config) as page:
                        assert len(attached.pages) == 2
                        await page.goto("https://www.vinted.de/items/123-amiri")
                        await page.set_content('<button onclick="window.clicked=true">Continue</button>')
                        await page.get_by_role("button", name="Continue").click(timeout=5000)
                        assert await page.evaluate("window.clicked") is True
                    assert page.is_closed()
                    with pytest.raises(WalletError, match="CheckoutChallenge"):
                        async with wallet_page(attached, config, preserve_challenge=True) as challenge:
                            await challenge.goto("https://www.vinted.de/items/456-stone-island")
                            raise WalletError("CheckoutChallenge")
                    assert not challenge.is_closed()
            # The actual CDP transport was stopped, not merely a mocked close().
            assert not original.is_closed()
            assert len(context.pages) == 2
            assert (await context.cookies("https://www.vinted.de"))[0]["value"] == "existing"
            assert json.loads((tmp_path / ".wallet-profiles" / ".cdp-bindings" / str(port) / "owner.json").read_text()) == {
                "user_id": 101, "region": "DE",
            }
        finally:
            await context.close()


async def test_warm_pool_passes_challenge_to_browser_cleanup():
    from vpars_bot.wallet_pool import WarmBrowserPool

    seen = []
    @asynccontextmanager
    async def factory(account):
        try:
            yield SimpleNamespace(page=SimpleNamespace(is_closed=lambda: False))
        except WalletError as exc:
            seen.append(str(exc))
            raise
    account = SimpleNamespace(user_id=101, region="DE", currency="EUR", enabled=True, contract_hash="hash")
    async with WarmBrowserPool(None, factory=factory, loader=lambda *_: ({"region": "DE"}, "hash")) as pool:
        with pytest.raises(WalletError, match="CheckoutChallenge"):
            async with pool.adapter(account):
                raise WalletError("CheckoutChallenge")
        assert not pool.entries
    assert seen == ["CheckoutChallenge"]
