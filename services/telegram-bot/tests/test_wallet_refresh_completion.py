"""Server-issued cookies remain verifiable when navigation discards a response body."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error
from test_wallet_sessions import key

from vpars_bot.wallet import REGIONS, WalletError
from vpars_bot.wallet_sessions import verify_session


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("subject", ["123", "456"])
async def test_unavailable_body_still_requires_matching_access_cookie(monkeypatch, region, subject):
    origin = "https://" + REGIONS[region][0]
    request = SimpleNamespace(method="POST", url=origin + "/web/api/auth/refresh")
    response = SimpleNamespace(url=request.url, request=request, status=200,
                               body=AsyncMock(side_effect=Error("Body no longer retained")))
    handlers = {}

    class Page:
        async def route(self, *args):
            pass

        def on(self, name, handler):
            handlers[name] = handler

        async def goto(self, *args, **kwargs):
            handlers["response"](response)
            # A later response must not overwrite the response being verified.
            handlers["response"](SimpleNamespace(url=request.url,
                request=SimpleNamespace(method="POST"), status=400))

    context = SimpleNamespace(add_cookies=AsyncMock(), close=AsyncMock(),
        new_page=AsyncMock(return_value=Page()), cookies=AsyncMock(return_value=[
            {"name": "access_token_web", "value": key(purpose="access", subject=subject)},
            {"name": "refresh_token_web", "value": key(subject=subject)},
        ]))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())

    @asynccontextmanager
    async def driver():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    monkeypatch.setattr("playwright.async_api.async_playwright", driver)
    persist = AsyncMock()
    if subject == "123":
        assert (await verify_session(region, key(), persist))[0] == "123"
    else:
        with pytest.raises(WalletError, match="RejectedKey"):
            await verify_session(region, key(), persist)
    persist.assert_awaited_once()
    response.body.assert_not_awaited()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
