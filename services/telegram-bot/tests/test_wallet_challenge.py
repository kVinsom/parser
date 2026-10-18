"""Observed challenge frames stop checkout; no challenge interaction or payment."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from test_wallet_browser import browser_page as browser_page
from test_wallet_browser import fixture_checkout

from vpars_bot.wallet import REGIONS, WalletError
from vpars_bot.wallet_browser import BrowserCheckout


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("owner,brand", [(101, "stone-island"), (202, "amiri")])
async def test_challenge_after_listing_buy_is_actionable_without_payment(browser_page, monkeypatch, region, owner, brand):
    origin = "https://" + REGIONS[region][0]
    async def listing(route):
        await route.fulfill(content_type="text/html", body='''
          <button id="buy" onclick="document.querySelector('iframe').style.display='block'">Buy</button>
          <iframe style="display:none" src="https://geo.captcha-delivery.com/captcha/?synthetic=1"></iframe>
          <button id="pay" onclick="window.payments=1">Pay</button>''')
    await browser_page.route(origin + "/items/*", listing)
    # Exercise the actual timeout branch without spending 15 seconds per fixture.
    monkeypatch.setattr(browser_page, "wait_for_url", AsyncMock(side_effect=BrowserTimeout("test timeout")))
    account = SimpleNamespace(user_id=owner, region=region)
    adapter = BrowserCheckout(browser_page, account, {
        "region": region, "selectors": {"listing_buy": {"css": "#buy"}},
    })
    with pytest.raises(WalletError, match="^CheckoutChallenge$"):
        await adapter.checkout(SimpleNamespace(url=origin + f"/items/123-{brand}", item_id="123"))
    assert not adapter.submitted and adapter.raw is None
    assert await browser_page.evaluate("window.payments || 0") == 0


async def test_challenge_appearing_before_confirmation_blocks_payment(browser_page):
    adapter, order = await fixture_checkout(browser_page)
    quote = adapter.quote(adapter.raw)
    await browser_page.evaluate("""() => {
        const frame = document.createElement('iframe');
        frame.src = 'https://geo.captcha-delivery.com/captcha/?synthetic=1';
        document.body.append(frame);
    }""")
    with pytest.raises(WalletError, match="^CheckoutChallenge$"):
        await adapter.pay(order, quote)
    assert await browser_page.evaluate("window.payments || 0") == 0


@pytest.mark.parametrize("source,hidden", [
    ("https://geo.captcha-delivery.com/captcha/", True),
    ("https://geo.captcha-delivery.com.other.example/captcha/", False),
    ("https://geo.captcha-delivery.com/unrelated/", False),
])
async def test_hidden_or_unrelated_frame_is_not_a_challenge(browser_page, source, hidden):
    adapter, _ = await fixture_checkout(browser_page)
    await browser_page.evaluate("""({source, hidden}) => {
        const frame = document.createElement('iframe'); frame.src = source;
        if (hidden) frame.style.display='none'; document.body.append(frame);
    }""", {"source": source, "hidden": hidden})
    assert (await adapter.snapshot())["item_id"] == "123"


@pytest.mark.parametrize("region", list(REGIONS))
async def test_logged_out_header_stops_before_buy_in_each_region(browser_page, region):
    origin = "https://" + REGIONS[region][0]
    await browser_page.route(origin + "/items/*", lambda route: route.fulfill(
        content_type="text/html", body='''<a data-testid="header--login-button">Login</a>
        <button id="buy" onclick="window.buys=1">Buy</button>'''))
    adapter = BrowserCheckout(browser_page, SimpleNamespace(region=region), {
        "region": region, "selectors": {"listing_buy": {"css": "#buy"}},
    })
    with pytest.raises(WalletError, match="^CheckoutLoginRequired$"):
        await adapter.checkout(SimpleNamespace(url=origin + "/items/123-amiri", item_id="123"))
    assert await browser_page.evaluate("window.buys || 0") == 0
    assert not adapter.submitted and adapter.raw is None


async def test_logged_out_after_quote_cannot_submit_payment(browser_page):
    adapter, order = await fixture_checkout(browser_page)
    quote = adapter.quote(adapter.raw)
    await browser_page.evaluate('''() => {
      const login = document.createElement('a');
      login.dataset.testid = 'header--login-button'; login.textContent = 'Login';
      document.body.append(login);
    }''')
    with pytest.raises(WalletError, match="^CheckoutLoginRequired$"):
        await adapter.pay(order, quote)
    assert await browser_page.evaluate("window.payments || 0") == 0
