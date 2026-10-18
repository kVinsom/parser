"""Observed delivery-phone UI on local intercepted pages; no real contact data."""

import pytest
from test_wallet_browser import browser_page as browser_page
from test_wallet_browser import fixture_checkout

from vpars_bot.wallet import ERRORS, REGIONS, WalletError
from vpars_bot.wallet_browser import FIELDS, SNAPSHOT
from vpars_bot.wallet_phone import phone_requirement

PROMPTS = [
    '<div data-testid="shipping-contact-details"><span data-testid="shipping-contact-Plus-icon">+</span></div>',
    '<div data-testid="shipping-contact-modal" role="dialog"><input value="+49"></div>',
]


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("uid,brand", [(101, "stone-island"), (202, "ralph-lauren")])
@pytest.mark.parametrize("prompt", PROMPTS)
async def test_missing_or_unsaved_phone_blocks_quote_and_pay(browser_page, region, uid, brand, prompt):
    origin = "https://" + REGIONS[region][0]
    adapter, order = await fixture_checkout(browser_page)
    html = await browser_page.content()
    await browser_page.goto(origin + "/checkout")
    await browser_page.set_content(html)
    adapter.origin = origin
    adapter.account.user_id = uid
    order.url = origin + "/items/123-" + brand
    quote = adapter.quote(adapter.raw)
    await browser_page.locator("body").evaluate("(e, html) => e.insertAdjacentHTML('beforeend', html)", prompt)
    with pytest.raises(WalletError, match="CheckoutPhoneRequired"):
        await adapter.snapshot()
    with pytest.raises(WalletError, match="CheckoutPhoneRequired"):
        await adapter.pay(order, quote)
    assert await browser_page.evaluate("window.payments || 0") == 0
    assert "телефон доставки" in ERRORS["CheckoutPhoneRequired"]


@pytest.mark.parametrize("prompt", PROMPTS)
async def test_atomic_payment_check_blocks_phone_prompt_appearing_after_preflight(browser_page, prompt):
    adapter, _ = await fixture_checkout(browser_page)
    await browser_page.locator("body").evaluate("(e, html) => e.insertAdjacentHTML('beforeend', html)", prompt)
    with pytest.raises(Exception, match="checkout_phone_required"):
        await browser_page.evaluate(SNAPSHOT, {
            "selectors": adapter.contract["selectors"], "fields": sorted(FIELDS),
            "origin": adapter.origin, "expected": adapter.raw,
        })
    assert await browser_page.evaluate("window.payments || 0") == 0


@pytest.mark.parametrize("style", ["display:none", "visibility:hidden", "visibility:collapse"])
async def test_hidden_phone_prompt_does_not_invalidate_verified_checkout(browser_page, style):
    adapter, _ = await fixture_checkout(browser_page)
    await browser_page.locator("body").evaluate("(e, html) => e.insertAdjacentHTML('beforeend', html)",
                                              f'<section style="{style}">{"".join(PROMPTS)}</section>')
    assert await adapter.snapshot() == adapter.raw


@pytest.mark.parametrize("url", ["https://example.test/checkout", "https://www.vinted.de/settings/account"])
async def test_unrelated_phone_page_is_not_delivery_setup(browser_page, url):
    await browser_page.goto(url)
    await browser_page.set_content("".join(PROMPTS))
    assert await phone_requirement(browser_page, "https://www.vinted.de") is None


async def test_unrelated_plus_icon_is_not_a_missing_delivery_phone(browser_page):
    adapter, _ = await fixture_checkout(browser_page)
    await browser_page.locator("body").evaluate('''e=>e.insertAdjacentHTML('beforeend',
        '<span data-testid="shipping-contact-Plus-icon">Unrelated icon</span>')''')
    assert await adapter.snapshot() == adapter.raw
