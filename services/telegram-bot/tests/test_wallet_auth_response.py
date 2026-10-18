"""Refresh response compatibility and real Chromium cookie rotation (synthetic keys only)."""

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_wallet_regions import seed
from test_wallet_sessions import key

from vpars_bot.db import Notification, WalletAccount, WalletOrder, WalletSession, utcnow
from vpars_bot.session_secrets import SessionCipher
from vpars_bot.wallet import REGIONS, WalletError
from vpars_bot.wallet_sessions import (
    SESSION_ERRORS,
    SessionService,
    SessionWorker,
    validate_key,
    verify_session,
)


def token(**changes):
    parts = key().split(".")
    data = json.loads(base64.urlsafe_b64decode(parts[1] + "==="))
    data.update(changes)
    parts[1] = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return ".".join(parts)


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("issuer", ["vinted-iam-service", "vinted-iam-oauth"])
def test_supported_refresh_issuers_still_require_remote_verification(region, issuer):
    assert validate_key(token(iss=issuer), region)["sub"] == "123"


@pytest.mark.parametrize("expiry", [float("nan"), float("inf"), 10**30])
def test_invalid_expiry_is_an_input_error(expiry):
    with pytest.raises(WalletError, match="InvalidKey"):
        validate_key(token(exp=expiry), "DE")


@pytest.mark.parametrize("response_token,error", [
    (token(iss="vinted-iam-oauth", purpose="access"), None),
    (token(iss="vinted-iam-service", purpose="access"), None),
    (token(iss="vinted-iam-oauth", purpose="access", sub=None), "SessionNotAuthenticated"),
    (token(purpose="access", sub="456"), "RejectedKey"),
    (token(purpose="access", iss="unexpected"), "SessionResponseInvalid"),
    (token(purpose="refresh"), "SessionResponseInvalid"),
    (token(purpose="access", exp=float("nan")), "SessionResponseInvalid"),
    ("not-a-jwt", "SessionResponseInvalid"),
    (None, "SessionResponseInvalid"),
], ids=["oauth", "legacy", "guest", "other-account", "unknown-issuer", "wrong-purpose",
        "invalid-expiry", "malformed", "missing"])
async def test_response_failure_never_blames_key_syntax(monkeypatch, caplog, response_token, error):
    class Page:
        def __init__(self):
            self.handlers = {}

        async def route(self, *args):
            pass

        def on(self, event, handler):
            self.handlers[event] = handler

        async def goto(self, *args, **kwargs):
            request = SimpleNamespace(method="POST")
            self.handlers["response"](SimpleNamespace(
                url="https://www.vinted.de/web/api/auth/refresh", status=200,
                request=request, body=AsyncMock(return_value=b"{}"),
                finished=AsyncMock(),
            ))

    cookies = [{"name": "refresh_token_web", "value": token(iss="vinted-iam-oauth")}]
    if response_token is not None:
        cookies.append({"name": "access_token_web", "value": response_token})
    context = SimpleNamespace(add_cookies=AsyncMock(), close=AsyncMock(),
        new_page=AsyncMock(return_value=Page()), cookies=AsyncMock(return_value=cookies))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())

    @asynccontextmanager
    async def driver():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    monkeypatch.setattr("playwright.async_api.async_playwright", driver)
    persist = AsyncMock()
    caplog.set_level("INFO", logger="vpars_bot.wallet_sessions")
    if error:
        with pytest.raises(WalletError, match=error):
            await verify_session("DE", key(), persist)
    else:
        identity, expiry = await verify_session("DE", key(), persist)
        assert identity == "123" and expiry > utcnow()
    persist.assert_awaited_once()
    assert key() not in caplog.text
    assert not response_token or response_token not in caplog.text
    assert "region=DE" in caplog.text
    assert "outcome=" + (error or "verified") in caplog.text
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("subject", ["123", "456"])
@pytest.mark.parametrize("navigate", [False, True])
async def test_real_browser_keeps_only_rotated_key_for_selected_region(monkeypatch, region, subject, navigate):
    from playwright.async_api import Response, async_playwright

    body = AsyncMock(side_effect=AssertionError("Refresh must not read the response body"))
    monkeypatch.setattr(Response, "body", body)

    origin = "https://" + REGIONS[region][0]
    refresh = token(sub=subject)
    rotated = token(sub=subject, iss="vinted-iam-oauth")
    access = token(sub=subject, iss="vinted-iam-oauth", purpose="access")
    host = REGIONS[region][0]
    device_cookie = f"synthetic-device-{region}-{subject}"
    calls = []

    async def serve(route):
        assert route.request.url.startswith(origin + "/")  # No live traffic.
        if route.request.url == origin + "/":
            navigation = ".then(() => { location.href = '/signed-in'; })" if navigate else ""
            await route.fulfill(content_type="text/html", body=(
                "<script>fetch('/web/api/auth/refresh', {method: 'POST'})" + navigation + ";</script>"
            ))
        elif route.request.url == origin + "/signed-in":
            await route.fulfill(content_type="text/html", body="<p>Signed in</p>")
        elif route.request.url == origin + "/web/api/auth/refresh":
            calls.append(await route.request.all_headers())
            await route.fulfill(status=200, content_type="application/json", body="{}", headers={
                "set-cookie": (
                    f"refresh_token_web={rotated}; Domain=.{host}; Path=/; HttpOnly; Secure; SameSite=Lax\n"
                    f"access_token_web={access}; Domain=.{host.removeprefix('www.')}; Path=/; HttpOnly; Secure; SameSite=Lax\n"
                    f"anon_id={device_cookie}; Domain=.{host}; Path=/; Secure; SameSite=Lax"
                ),
            })
        else:
            await route.abort()

    async with async_playwright() as real_driver:
        browser = await real_driver.chromium.launch(headless=True, chromium_sandbox=True)
        new_context = browser.new_context

        async def isolated_context(**kwargs):
            context = await new_context(**kwargs)
            await context.add_cookies([{
                "name": "unrelated-session", "value": "never-export-third-party-state",
                "domain": "unrelated.example", "path": "/", "secure": True,
            }])
            new_page = context.new_page

            async def isolated_page():
                page = await new_page()
                page_route = page.route

                async def intercepted(pattern, guard):
                    async def guarded(route):
                        await guard(SimpleNamespace(request=route.request, abort=route.abort,
                                                    continue_=lambda: serve(route)))
                    await page_route(pattern, guarded)

                monkeypatch.setattr(page, "route", intercepted)
                return page

            monkeypatch.setattr(context, "new_page", isolated_page)
            return context

        monkeypatch.setattr(browser, "new_context", isolated_context)

        @asynccontextmanager
        async def driver():
            yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

        monkeypatch.setattr("playwright.async_api.async_playwright", driver)
        persist = AsyncMock()
        identity, expiry = await verify_session(region, refresh, persist)
        assert identity == subject and expiry > utcnow()
        assert len(calls) == 1
        assert calls[0]["cookie"].count("refresh_token_web=") == 1
        cookies = persist.await_args.args[0]
        saved = [cookie for cookie in cookies if cookie["name"] == "refresh_token_web"]
        assert len(saved) == 1 and saved[0]["value"] == rotated
        assert all(cookie["value"] != refresh for cookie in cookies)
        assert saved[0]["domain"] == "." + host
        assert [c["value"] for c in cookies if c["name"] == "anon_id"] == [device_cookie]
        assert not any(c["name"] == "unrelated-session" for c in cookies)
        body.assert_not_awaited()


@pytest.mark.parametrize("error", ["SessionNotAuthenticated", "SessionResponseInvalid"])
async def test_response_errors_remain_disabled_and_are_reported_for_every_owner_region(db, registry, error):
    sessions = db[1]
    await seed(sessions, registry)
    cipher = SessionCipher(Fernet.generate_key())
    service = SessionService(sessions, cipher)
    for uid in (101, 202):
        for region in REGIONS:
            await service.submit(uid, token(iss="vinted-iam-oauth", sub=str(uid)),
                                 f"{uid}-{region}", region=region, auto_enable=True)

    async def rejected(region, value, persist):
        await persist([{"name": "access_token_web", "value": "synthetic-response"}])
        raise WalletError(error)

    installer, checkout = AsyncMock(), AsyncMock()
    worker = SessionWorker(sessions, rejected, cipher, installer, checkout)
    for _ in range(8):
        assert await worker.once()
    assert not await worker.once()  # Failed refresh keys must never be replayed.
    installer.assert_not_awaited()
    checkout.assert_not_awaited()
    async with sessions() as session:
        rows = list(await session.scalars(select(WalletSession)))
        assert len(rows) == 8
        assert all(row.state == "failed" and row.error == error and row.secret is None for row in rows)
        assert all(not account.enabled for account in await session.scalars(select(WalletAccount)))
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
        notices = list(await session.scalars(select(Notification)))
        assert len(notices) == 8
        assert all(SESSION_ERRORS[error] in row.payload["text"] for row in notices)
        assert all(SESSION_ERRORS["InvalidKey"] not in row.payload["text"] for row in notices)
