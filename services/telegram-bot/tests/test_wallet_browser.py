"""Browser contract tests on intercepted HTML only; never contact or pay Vinted."""

from dataclasses import asdict
from html import escape
from types import SimpleNamespace

import pytest

from vpars_bot.wallet import WalletError
from vpars_bot.wallet_browser import FIELDS, SELECTORS, SNAPSHOT, BrowserCheckout


@pytest.fixture
async def browser_page():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Install Chromium for the optional browser tests")
            raise
        context = await browser.new_context(service_workers="block")
        # ALL requests are intercepted; even a fixture redirect cannot reach the network.
        await context.route("**/*", lambda route: route.fulfill(status=200, body="<html></html>"))
        page = await context.new_page()
        await page.goto("https://www.vinted.de/checkout")
        try:
            yield page
        finally:
            await browser.close()


async def fixture_checkout(page):
    raw = {
        "item_id": "123",
        "currency": "EUR",
        "item_price": "10.00 €",
        "shipping": "2.00 €",
        "protection": "1.50 €",
        "total": "13.50 €",
        "wallet_balance": "50.00 €",
        "wallet_charge": "13.50 €",
        "external_charge": "0.00 €",
        "payment_method": "Vinted Balance",
        "delivery_key": "pickup-1",
        "account_key": "account-1",
        "checkout_key": "checkout-1",
    }
    elements = "".join(f'<div id="{key}">{escape(value)}</div>' for key, value in raw.items())
    await page.set_content(
        '<main id="checkout_root">'
        + elements
        + '<button id="pay" onclick="window.payments = (window.payments || 0) + 1; '
        "document.querySelector('#success_root').style.display = 'block'\">Pay</button></main>"
        '<div id="success_root" style="display:none"><span id="order_id">order-456</span>'
        '<span id="order_item_id">123</span></div>'
    )
    contract = {
        "region": "DE",
        "expected_account": "account-1",
        "wallet_label": "Vinted Balance",
        "decimal_separator": ".",
        "selectors": {key: {"css": "#" + key} for key in SELECTORS},
    }
    account = SimpleNamespace(region="DE", currency="EUR", max_total=2000)
    order = SimpleNamespace(item_id="123", currency="EUR", listed_price=1000)
    adapter = BrowserCheckout(page, account, contract)
    adapter.raw = await adapter.snapshot()
    return adapter, order


async def test_atomic_verified_wallet_payment(browser_page):
    adapter, order = await fixture_checkout(browser_page)
    quote = adapter.quote(adapter.raw)
    assert set(asdict(quote)) == FIELDS
    quote.check(order, adapter.account)
    assert await adapter.pay(order, quote) == "order-456"
    assert await browser_page.evaluate("window.payments") == 1
    with pytest.raises(WalletError):
        await adapter.pay(order, quote)
    assert await browser_page.evaluate("window.payments") == 1


async def test_vinted_checkout_error_is_reported_before_any_payment(browser_page):
    adapter, order = await fixture_checkout(browser_page)
    quote = adapter.quote(adapter.raw)
    await browser_page.locator("body").evaluate('''body => {
        const dialog = document.createElement('div');
        dialog.dataset.testid = 'checkout-generic-error-modal';
        dialog.textContent = 'Checkout unavailable';
        body.append(dialog);
    }''')
    with pytest.raises(WalletError, match="CheckoutRejected"):
        await adapter.snapshot()
    assert not await browser_page.evaluate("window.payments || 0")
    # Also cover a modal appearing after Python preflight but before the atomic click.
    with pytest.raises(Exception, match="checkout_rejected"):
        await browser_page.evaluate(SNAPSHOT, {
            "selectors": adapter.contract["selectors"], "fields": sorted(FIELDS),
            "origin": adapter.origin, "expected": adapter.raw,
        })
    assert not await browser_page.evaluate("window.payments || 0")
    with pytest.raises(WalletError, match="CheckoutRejected"):
        await adapter.pay(order, quote)
    assert not await browser_page.evaluate("window.payments || 0")


@pytest.mark.parametrize(
    "field,value",
    [
        ("external_charge", "13.50 €"),
        ("total", "99.00 €"),
        ("item_id", "999"),
        ("account_key", "other"),
        ("delivery_key", "other"),
        ("payment_method", "Card"),
    ],
)
async def test_last_millisecond_page_change_blocks_click(browser_page, field, value):
    adapter, order = await fixture_checkout(browser_page)
    quote = adapter.quote(adapter.raw)
    await browser_page.locator("#" + field).evaluate("(e, text) => e.textContent = text", value)
    with pytest.raises(Exception, match="changed_checkout"):
        await adapter.pay(order, quote)
    assert await browser_page.evaluate("window.payments || 0") == 0


async def test_ambiguous_price_and_disabled_pay_are_rejected(browser_page):
    adapter, order = await fixture_checkout(browser_page)
    await browser_page.locator("#total").evaluate("e => e.after(e.cloneNode(true))")
    with pytest.raises(Exception, match="ambiguous_checkout"):
        await adapter.snapshot()
    adapter, order = await fixture_checkout(browser_page)
    quote = adapter.quote(adapter.raw)
    await browser_page.locator("#pay").evaluate("e => e.disabled = true")
    with pytest.raises(Exception, match="payment_unavailable"):
        await adapter.pay(order, quote)
    assert await browser_page.evaluate("window.payments || 0") == 0


async def test_wrong_success_item_is_not_a_completed_purchase(browser_page):
    import asyncio

    adapter, order = await fixture_checkout(browser_page)
    await browser_page.locator("#order_item_id").evaluate("e => e.textContent = '999'")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await adapter.pay(order, adapter.quote(adapter.raw))
    assert await browser_page.evaluate("window.payments || 0") == 1
