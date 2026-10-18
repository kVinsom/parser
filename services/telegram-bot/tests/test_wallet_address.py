"""Saved-address behavior on intercepted pages; no real addresses or purchases."""

import pytest
from test_wallet_browser import browser_page as browser_page
from test_wallet_browser import fixture_checkout
from test_wallet_handoff_browser import browser_fixture

from vpars_bot.domain import UserError
from vpars_bot.wallet import REGIONS, WalletError
from vpars_bot.wallet_address import ADDRESS_DIALOG, EMPTY_ADDRESS, address_requirement
from vpars_bot.wallet_browser import FIELDS, SNAPSHOT

ADDRESS_UI = """
<div data-testid="empty-shipping-address-details" role="button"
 onclick="window.opens=(window.opens||0)+1;
 document.querySelector('[data-testid=shipping-address-dialog]').hidden=false">
 Add your address</div>
<div data-testid="shipping-address-dialog" role="dialog" hidden>
 <input aria-label="Address" value="TEST FIXTURE">
 <button id="save" onclick="window.saves=(window.saves||0)+1;
 document.querySelector('[data-testid=empty-shipping-address-details]').hidden=true;
 this.parentElement.hidden=true">Save address</button>
</div>
"""


async def address_checkout(page, region="DE", uid=101):
    browser = await browser_fixture(page, region, uid)
    await page.evaluate("history.pushState({}, '', '/checkout')")
    await page.locator("body").evaluate("(e, html) => e.insertAdjacentHTML('beforeend', html)",
                                        ADDRESS_UI)
    return browser


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("uid,brand", [(101, "stone-island"), (202, "ralph-lauren")])
async def test_vinted_selected_address_survives_repeat_prepare(browser_page, region, uid, brand):
    browser = await browser_fixture(browser_page, region, uid)
    browser.order.url = browser.origin + "/items/123-" + brand
    await browser_page.evaluate("history.pushState({}, '', '/checkout')")
    await browser_page.locator("body").evaluate("""e => e.insertAdjacentHTML('beforeend',
      '<select id="delivery"><option>other</option><option selected>saved</option></select>')""")
    await browser.prepare()
    await browser.prepare(resume=True)
    assert await browser.state() == "checkout"
    assert await browser_page.locator("#delivery").input_value() == "saved"
    assert await browser_page.evaluate("[window.buys||0, window.pays||0]") == [0, 0]


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("uid", [101, 202])
async def test_missing_address_opens_only_on_resume_and_only_user_saves(browser_page, region, uid):
    browser = await address_checkout(browser_page, region, uid)
    await browser.prepare()
    assert await browser.state() == "address_required"
    assert await browser_page.evaluate("window.opens||0") == 0
    await browser.prepare(resume=True)
    await browser.prepare(resume=True)
    assert await browser.state() == "address_editing"
    assert await browser_page.evaluate("[window.opens, window.saves||0]") == [1, 0]
    assert await browser_page.get_by_label("Address").input_value() == "TEST FIXTURE"
    # Simulated explicit human Save in the local fixture, never on live Vinted.
    await browser_page.locator("#save").click()
    await browser.prepare(resume=True)
    assert await browser.state() == "checkout"
    assert await browser_page.evaluate("[window.opens, window.saves, window.buys||0, window.pays||0]") == [1, 1, 0, 0]


async def test_wrong_account_cannot_open_address_form(browser_page):
    browser = await address_checkout(browser_page)
    browser.identity = "202"
    with pytest.raises(UserError):
        await browser.prepare(resume=True)
    assert await browser_page.evaluate("window.opens||0") == 0


@pytest.mark.parametrize("blocker,state", [
    ('<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>', "challenge"),
    ('<button data-testid="header--login-button">Log in</button>', "login"),
])
async def test_address_does_not_override_challenge_or_login(browser_page, blocker, state):
    browser = await address_checkout(browser_page)
    await browser_page.locator("body").evaluate("(e, html) => e.insertAdjacentHTML('beforeend', html)", blocker)
    await browser.prepare(resume=True)
    assert await browser.state() == state
    assert await browser_page.evaluate("window.opens||0") == 0


@pytest.mark.parametrize("url", ["https://example.test/checkout", "https://www.vinted.de/settings/shipping"])
async def test_address_controls_outside_checkout_are_ignored(browser_page, url):
    browser = await browser_fixture(browser_page, "DE", 101)
    await browser_page.goto(url)
    await browser_page.set_content(ADDRESS_UI)
    await browser.prepare(resume=True)
    assert await address_requirement(browser_page, browser.origin) is None
    assert await browser_page.evaluate("window.opens||0") == 0


@pytest.mark.parametrize("change", [
    "e => e.after(e.cloneNode(true))",
    "e => e.removeAttribute('role')",
])
async def test_ambiguous_or_changed_address_control_is_not_clicked(browser_page, change):
    browser = await address_checkout(browser_page)
    await browser_page.locator(EMPTY_ADDRESS).evaluate(change)
    await browser.prepare(resume=True)
    assert await browser_page.evaluate("window.opens||0") == 0


@pytest.mark.parametrize("selector", [EMPTY_ADDRESS, ADDRESS_DIALOG])
async def test_missing_or_unsaved_address_blocks_quote_and_payment(browser_page, selector):
    adapter, order = await fixture_checkout(browser_page)
    quote = adapter.quote(adapter.raw)
    await browser_page.locator("body").evaluate("(e, html) => e.insertAdjacentHTML('beforeend', html)", ADDRESS_UI)
    if selector == ADDRESS_DIALOG:
        await browser_page.locator(ADDRESS_DIALOG).evaluate("e => e.hidden = false")
    with pytest.raises(WalletError, match="CheckoutAddressRequired"):
        await adapter.snapshot()
    with pytest.raises(WalletError, match="CheckoutAddressRequired"):
        await adapter.pay(order, quote)
    assert await browser_page.evaluate("window.payments||0") == 0


async def test_atomic_snapshot_blocks_address_form_appearing_before_click(browser_page):
    adapter, _ = await fixture_checkout(browser_page)
    await browser_page.locator("body").evaluate("(e, html) => e.insertAdjacentHTML('beforeend', html)", ADDRESS_UI)
    with pytest.raises(Exception, match="checkout_address_required"):
        await browser_page.evaluate(SNAPSHOT, {
            "selectors": adapter.contract["selectors"], "fields": sorted(FIELDS),
            "origin": adapter.origin, "expected": adapter.raw,
        })
    assert await browser_page.evaluate("window.payments||0") == 0


async def test_hidden_address_controls_do_not_invalidate_saved_delivery(browser_page):
    adapter, _ = await fixture_checkout(browser_page)
    await browser_page.locator("body").evaluate("(e, html) => e.insertAdjacentHTML('beforeend', html)", ADDRESS_UI)
    await browser_page.locator(EMPTY_ADDRESS).evaluate("e => e.style.visibility = 'hidden'")
    assert await adapter.snapshot() == adapter.raw
    assert await browser_page.evaluate("window.payments||0") == 0


async def test_missing_warning_is_not_proof_of_a_valid_delivery(browser_page):
    adapter, order = await fixture_checkout(browser_page)
    await browser_page.locator("#delivery_key").evaluate("e => e.textContent = ''")
    quote = adapter.quote(await adapter.snapshot())
    with pytest.raises(WalletError, match="ManualAction"):
        await adapter.pay(order, quote)
    assert await browser_page.evaluate("window.payments||0") == 0
