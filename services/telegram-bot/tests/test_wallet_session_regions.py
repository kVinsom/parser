"""Undocumented JWT country hints never choose a storefront or enable payment."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_wallet_regions import seed
from test_wallet_sessions import key

from vpars_bot.db import WalletAccount, WalletOrder, WalletSession, utcnow
from vpars_bot.session_secrets import SessionCipher
from vpars_bot.wallet import REGIONS, WalletError
from vpars_bot.wallet_sessions import SessionService, SessionWorker, validate_key, verify_session


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("hint", ["US", "FR", "GB", None])
def test_unverified_country_hint_does_not_reject_selected_storefront(region, hint):
    result = validate_key(key(hint), region)
    assert result["cc"] == hint  # Never forge/replace token claims.


@pytest.mark.parametrize("region", ["US", "PL", "", None])
def test_unsupported_selected_storefront_still_rejected(region):
    with pytest.raises(WalletError, match="UnsupportedCountry"):
        validate_key(key("DE"), region)


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("response_kind", ["accepted", "rejected", "challenge", "other_origin", "other_subject"])
async def test_country_hint_requires_selected_origin_to_accept_same_identity(monkeypatch, region, response_kind):
    origin = "https://" + REGIONS[region][0]
    seen_origins = []

    class Page:
        def __init__(self):
            self.handlers = {}

        async def route(self, *args):
            pass

        def on(self, event, handler):
            self.handlers[event] = handler

        async def goto(self, url, **kwargs):
            assert url == origin + "/"
            seen_origins.append(url)
            request = SimpleNamespace(method="POST")
            self.handlers["response"](SimpleNamespace(
                url=("https://www.vinted.com" if response_kind == "other_origin" else origin)
                    + "/web/api/auth/refresh",
                status={"rejected": 400, "challenge": 403}.get(response_kind, 200),
                request=request, body=AsyncMock(return_value=b"{}"),
            ))

    context = SimpleNamespace(
        add_cookies=AsyncMock(), close=AsyncMock(), new_page=AsyncMock(return_value=Page()),
        cookies=AsyncMock(return_value=[{
            "name": "access_token_web", "value": key("US", purpose="access",
                subject="999" if response_kind == "other_subject" else "123"),
        }, {"name": "refresh_token_web", "value": key("US")}]),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())

    @asynccontextmanager
    async def driver():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    monkeypatch.setattr("playwright.async_api.async_playwright", driver)
    wait_for = asyncio.wait_for
    async def bounded_wait(awaitable, timeout):
        return await wait_for(awaitable, timeout=.03)
    monkeypatch.setattr("vpars_bot.wallet_sessions.asyncio.wait_for", bounded_wait)
    persist = AsyncMock()
    token = key("US")
    errors = {"rejected": "RejectedKey", "challenge": "SessionChallenge",
              "other_origin": "SessionUnavailable", "other_subject": "RejectedKey"}
    if response_kind in errors:
        with pytest.raises(WalletError, match=errors[response_kind]):
            await verify_session(region, token, persist)
    else:
        identity, expiry = await verify_session(region, token, persist)
        assert identity == "123" and expiry > utcnow()
    assert seen_origins == [origin + "/"]
    assert context.add_cookies.await_args.args[0][0]["domain"] == "." + REGIONS[region][0]
    assert context.add_cookies.await_args.args[0][0]["path"] == "/"
    assert context.add_cookies.await_args.args[0][0]["value"] == token
    if response_kind in {"accepted", "other_subject"}:
        persist.assert_awaited_once()
        context.cookies.assert_awaited_once_with(origin)
    else:
        persist.assert_not_awaited()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()


async def test_two_users_four_regions_with_us_hint_wait_for_checkout(db, registry):
    sessions = db[1]
    await seed(sessions, registry)
    cipher = SessionCipher(Fernet.generate_key())
    service = SessionService(sessions, cipher)
    for uid in (101, 202):
        for region in REGIONS:
            await service.submit(uid, key("US", subject=str(uid)), f"{uid}-{region}",
                                 region=region, auto_enable=True)
    async def verified(region, token, persist):
        from vpars_bot.wallet_sessions import claims

        await persist([])
        return claims(token)["sub"], utcnow() + timedelta(hours=1)
    installer = AsyncMock()
    checkout = AsyncMock(side_effect=WalletError("CheckoutUnavailable"))
    worker = SessionWorker(sessions, verified, cipher, installer, checkout)
    for _ in range(8):
        assert await worker.once()
    assert not await worker.once()
    async with sessions() as session:
        rows = list(await session.scalars(select(WalletSession)))
        assert len(rows) == 8
        assert all(row.state == "connected" and row.error == "CheckoutUnavailable" for row in rows)
        assert all(not row.enabled for row in await session.scalars(select(WalletAccount)))
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
        assert {(row.user_id, row.region) for row in rows} == {
            (uid, region) for uid in (101, 202) for region in REGIONS
        }
