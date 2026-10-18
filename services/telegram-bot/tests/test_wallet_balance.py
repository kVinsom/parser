"""Balance-first policy: one authorized Buy, one wallet debit, same-tab fallback."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select
from test_wallet import good_quote
from test_wallet_browser import browser_page as browser_page
from test_wallet_browser import fixture_checkout
from test_wallet_handoff import TOKEN, FakeBrowser, delivery_reference, init_data, setup

from vpars_bot.db import WalletAccount, WalletOrder, WalletSession
from vpars_bot.domain import UserError
from vpars_bot.wallet import REGIONS, WalletError
from vpars_bot.wallet_handoff_web import HandoffWindows, handoff_app, handoff_server
from vpars_bot.wallet_worker import WalletWorker


class BalanceSite(FakeBrowser):
    def __init__(self, sessions, quote):
        super().__init__()
        self.sessions, self.current_quote = sessions, quote
        self.payments = 0
        self.adapter_error = None
        self.payment_error = False
        self.raw = None
        self.before_quote = None

    async def balance_adapter(self, account):
        if self.adapter_error:
            raise WalletError(self.adapter_error)
        if self.before_quote:
            await self.before_quote(account)
        return self

    async def snapshot(self):
        return {"fixture":"same-checkout"}

    def quote(self, raw, *, require_wallet):
        assert not require_wallet
        return self.current_quote

    async def pay(self, order, quote):
        async with self.sessions() as session:
            durable = await session.get(WalletOrder, order.id)
            assert durable.state == "paying"
            assert durable.quote["total"] == quote.total
        self.payments += 1
        if self.payment_error:
            raise TimeoutError("payment response lost")
        return "fixture-order-123"


async def prepare(db, registry, uid=101, mid=2):
    service = await setup(db[1], registry)
    service.balance_first = True
    async with db[1].begin() as session:
        for account in await session.scalars(select(WalletAccount)):
            account.enabled, account.contract_hash = True, "a" * 64
    order = await service.request(uid, mid)
    region = list(REGIONS)[mid % 4]
    site = BalanceSite(db[1], replace(good_quote(), item_id=order.item_id,
                                      currency=REGIONS[region][1], account_key=str(uid)))

    @asynccontextmanager
    async def factory(order, account, identity):
        assert identity == str(uid)
        try:
            yield site
        finally:
            site.closed = True

    return service, order, site, HandoffWindows(service, factory=factory)


async def saved(db, order):
    async with db[1]() as session:
        return await session.get(WalletOrder, order.id)


async def test_balance_mode_without_https_never_starts_an_order_or_legacy_payment(db, registry):
    from sqlalchemy import func

    from vpars_bot.config import Settings
    from vpars_bot.wallet import WalletService

    await setup(db[1], registry)
    config = Settings(_env_file=None, wallet_checkout_mode="balance_first", wallet_handoff_url="")
    async with handoff_server(db[1], config) as service:
        assert service.balance_first
        with pytest.raises(UserError, match="зовнішнього доступу"):
            await service.request(101, 0)
        with pytest.raises(UserError, match="Оплату тепер"):
            await WalletService(db[1]).decide(101, "f" * 32, pay=True)
        assert await WalletWorker(db[1], None).claim() is None
    async with db[1]() as session:
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0


@pytest.mark.parametrize("uid", [101, 202])
@pytest.mark.parametrize("mid", range(8))
async def test_buy_pays_once_all_users_regions_brands(db, registry, uid, mid):
    service, order, site, windows = await prepare(db, registry, uid, mid)
    assert order.state == "balance_waiting"
    assert await WalletWorker(db[1], None).claim() is None
    with pytest.raises(UserError):
        await windows.attach(202 if uid == 101 else 101, order.id)
    assert site.payments == 0
    window = await windows.attach(uid, order.id)
    assert window.result == "succeeded" and site.payments == 1
    assert (await saved(db, order)).state == "succeeded"
    await windows.close(window)
    assert (await service.request(uid, mid)).id == order.id
    await service.recover()
    assert site.payments == 1
    with pytest.raises(UserError):
        await windows.attach(uid, order.id)


@pytest.mark.parametrize("balance", [0, 1349])
async def test_insufficient_balance_retains_same_browser_and_saved_delivery(db, registry, balance):
    service, order, site, windows = await prepare(db, registry)
    site.current_quote = replace(site.current_quote, wallet_balance=balance,
                                 wallet_charge=balance, external_charge=1350-balance,
                                 payment_method="external")
    window = await windows.attach(101, order.id)
    assert window.browser is site and window.result is None
    assert "бракує коштів" in window.note
    assert site.current_quote.delivery_key == "pickup-1"
    assert site.payments == 0 and not site.closed
    assert (await saved(db, order)).state == "human_active"
    await windows.detach(window)
    assert await windows.attach(101, order.id) is window
    assert site.payments == 0
    await windows.close(window)


async def test_exact_balance_is_enough_without_external_charge(db, registry):
    _, order, site, windows = await prepare(db, registry)
    site.current_quote = replace(site.current_quote, wallet_balance=1350)
    window = await windows.attach(101, order.id)
    assert site.payments == 1 and window.result == "succeeded"
    await windows.close(window)


@pytest.mark.parametrize("error", ["NotConnected", "WalletOnly", "ContractMismatch",
                                   "CheckoutAddressRequired", "CheckoutChallenge"])
async def test_unverified_balance_never_reported_as_insufficient(db, registry, error):
    _, order, site, windows = await prepare(db, registry)
    site.adapter_error = error
    window = await windows.attach(101, order.id)
    assert window.result is None and "бракує коштів" not in window.note
    assert site.payments == 0
    await windows.close(window)


@pytest.mark.parametrize("changes", [
    {"wallet_charge":1000, "external_charge":350}, {"payment_method":"external"},
])
async def test_sufficient_balance_never_uses_a_card(db, registry, changes):
    _, order, site, windows = await prepare(db, registry)
    site.current_quote = replace(site.current_quote, **changes)
    window = await windows.attach(101, order.id)
    assert "бот не використовував картку" in window.note
    assert (await saved(db, order)).error == "WalletOnly"
    assert site.payments == 0
    await windows.close(window)


@pytest.mark.parametrize("changes,error", [
    ({"shipping":20000, "total":21150, "wallet_balance":0}, "BudgetExceeded"),
    ({"item_price":1001, "total":1351}, "CheckoutChanged"),
])
async def test_full_limit_and_price_change_do_not_offer_second_payment(db, registry, changes, error):
    _, order, site, windows = await prepare(db, registry)
    site.current_quote = replace(site.current_quote, **changes)
    window = await windows.attach(101, order.id)
    current = await saved(db, order)
    assert window.result == "failed" and current.error == error
    assert site.payments == 0 and current.state == "failed"
    await windows.close(window)


async def test_payment_timeout_never_hands_off_or_retries(db, registry):
    service, order, site, windows = await prepare(db, registry)
    site.payment_error = True
    window = await windows.attach(101, order.id)
    assert window.result == "unknown" and window.note is None
    await windows.close(window)
    await service.recover()
    assert (await service.request(101, 2)).state == "unknown"
    assert site.payments == 1
    async with db[1]() as session:
        assert (await session.get(WalletAccount, (101, "DE"))).active_order == order.id


async def test_changed_account_binding_before_payment_blocks_debit(db, registry):
    _, order, site, windows = await prepare(db, registry)
    async def replace_identity(account):
        async with db[1].begin() as session:
            row = await session.get(WalletSession, (account.user_id, account.region))
            row.account_key = "changed-account"
    site.before_quote = replace_identity
    with pytest.raises(UserError, match="Підключення змінилося"):
        await windows.attach(101, order.id)
    assert site.payments == 0


@pytest.mark.parametrize("state,expected", [("balance_checking","failed"), ("paying","unknown")])
async def test_restart_does_not_repeat_balance_attempt(db, registry, state, expected):
    service, order, site, _ = await prepare(db, registry)
    async with db[1].begin() as session:
        (await session.get(WalletOrder, order.id)).state = state
    await service.recover()
    assert (await saved(db, order)).state == expected
    assert site.payments == 0


async def test_direct_mini_app_returns_result_without_exposing_payment_page(db, registry):
    from aiohttp.test_utils import TestClient, TestServer

    service, order, site, windows = await prepare(db, registry)
    async with TestClient(TestServer(handoff_app(service, TOKEN, windows=windows))) as client:
        headers = {"Origin":service.base_url}
        url = "/buy/" + delivery_reference(101, 2)
        response = await client.post(url+"/auth", headers=headers, json={"init_data":init_data()})
        data = await response.json()
        assert data["mode"] == "balance_first"
        async with client.ws_connect(data["stream_path"], headers=headers) as ws:
            await ws.send_json({"ticket":data["ticket"]})
            result = await ws.receive_json()
            assert result["type"] == "result" and result["state"] == "succeeded"
        response = await client.post(url+"/auth", headers=headers, json={"init_data":init_data()})
        assert (await response.json())["result"] == "succeeded"
        assert site.payments == 1


@pytest.mark.parametrize("balance,expected,payments", [(5000,"succeeded",1), (0,None,0)])
async def test_real_checkout_dom_pays_or_hands_off_same_tab(db, registry, browser_page, balance, expected, payments):
    service, order, _, _ = await prepare(db, registry)
    adapter, _ = await fixture_checkout(browser_page)
    adapter.contract["expected_account"] = "101"
    await browser_page.locator("#item_id").evaluate("(e,v)=>e.textContent=v", order.item_id)
    await browser_page.locator("#order_item_id").evaluate("(e,v)=>e.textContent=v", order.item_id)
    await browser_page.locator("#account_key").evaluate("e=>e.textContent='101'")
    if balance == 0:
        for key, value in {"wallet_balance":"0.00 €", "wallet_charge":"0.00 €",
                           "external_charge":"13.50 €", "payment_method":"Card"}.items():
            await browser_page.locator("#"+key).evaluate("(e,v)=>e.textContent=v", value)
    before = await browser_page.locator("#delivery_key").inner_text()
    site = FakeBrowser()
    async def balance_adapter(account):
        return adapter
    site.balance_adapter = balance_adapter
    @asynccontextmanager
    async def factory(*args):
        yield site
    windows = HandoffWindows(service, factory=factory)
    window = await windows.attach(101, order.id)
    assert window.result == expected
    assert await browser_page.evaluate("window.payments||0") == payments
    assert await browser_page.locator("#delivery_key").inner_text() == before
    assert window.browser is site
    await windows.close(window)
