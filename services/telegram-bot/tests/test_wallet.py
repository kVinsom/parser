from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_reply_menu import menu as menu

from vpars_bot.application import Catalog
from vpars_bot.db import Notification, WalletAccount, WalletOrder, utcnow
from vpars_bot.domain import UserError
from vpars_bot.notifications import NotificationSender
from vpars_bot.wallet import (
    CONNECT_TEXT,
    Quote,
    WalletError,
    WalletService,
    listing_identity,
    minor,
)
from vpars_bot.wallet_accounts import wallet_account
from vpars_bot.wallet_browser import money_text
from vpars_bot.wallet_worker import WalletWorker


def good_quote():
    return Quote(
        "123",
        "EUR",
        1000,
        200,
        150,
        1350,
        5000,
        1350,
        0,
        "wallet",
        "pickup-1",
        "account-1",
        "checkout-1",
    )


class Site:
    def __init__(self):
        self.quote = good_quote()
        self.payments = 0
        self.error = None
        self.before_pay = None

    @asynccontextmanager
    async def adapter(self, account):
        yield self

    async def checkout(self, order):
        if self.error == "checkout":
            raise OSError("private upstream response must not be persisted")
        return self.quote

    async def pay(self, order, quote):
        if self.before_pay:
            await self.before_pay(order)
        self.payments += 1
        if self.error == "payment":
            raise TimeoutError("secret payment URL must not be persisted")
        return "order-456"


async def setup(sessions, registry):
    catalog = Catalog(sessions, registry)
    await catalog.register(101, 101, "alice", "Alice", 1, start=True)
    await catalog.register(202, 202, "bob", "Bob", 2, start=True)
    async with sessions.begin() as session:
        session.add(
            WalletAccount(
                user_id=101,
                region="DE",
                currency="EUR",
                max_total=2000,
                enabled=True,
                contract_hash="a" * 64,
            )
        )

    async def resolver(user_id, message_id):
        assert user_id == 101 and message_id in {10, 11}
        return {
            "item_url": "https://www.vinted.de/items/123-shirt",
            "price": {"amount": "10.00", "currency": "EUR"},
        }

    service, site = WalletService(sessions, resolver), Site()
    worker = WalletWorker(sessions, site.adapter)
    return service, site, worker


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"wallet_balance": 1349}, "InsufficientBalance"),
        ({"payment_method": "card"}, "WalletOnly"),
        ({"external_charge": 1}, "WalletOnly"),
        ({"wallet_charge": 1349}, "WalletOnly"),
        ({"currency": "GBP"}, "CheckoutChanged"),
        ({"item_id": "999"}, "CheckoutChanged"),
        ({"item_price": 1001}, "CheckoutChanged"),
        ({"total": 1349}, "CheckoutChanged"),
        ({"delivery_key": ""}, "ManualAction"),
        ({"account_key": ""}, "ManualAction"),
        ({"total": float("nan")}, "ContractMismatch"),
        ({"shipping": True}, "ContractMismatch"),
        ({"protection": -1}, "ContractMismatch"),
    ],
)
def test_wallet_policy_rejects_unsafe_checkout(changes, error):
    with pytest.raises(WalletError, match=error):
        replace(good_quote(), **changes).check(
            SimpleNamespace(item_id="123", currency="EUR", listed_price=1000),
            SimpleNamespace(currency="EUR", max_total=2000),
        )


def test_total_budget_includes_shipping_and_protection():
    with pytest.raises(WalletError, match="BudgetExceeded"):
        good_quote().check(
            SimpleNamespace(item_id="123", currency="EUR", listed_price=1000),
            SimpleNamespace(currency="EUR", max_total=1000),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.vinted.de/items/123",
        "https://www.vinted.de.evil/items/123",
        "https://evil@www.vinted.de/items/123",
        "https://www.vinted.de:443/items/123",
        "https://127.0.0.1/items/123",
        "https://www.vinted.de/items/123?redirect=evil",
        "https://www.vinted.de/items/123#pay",
        "https://www.vinted.de/items/1/../123",
        "https://www.vinted.de/items/0",
        "https://www.vinted.de/items/123\n",
    ],
)
def test_only_known_vinted_listing_urls(url):
    with pytest.raises(WalletError):
        listing_identity(url)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "1.001", "1000001"])
def test_money_rejects_invalid_minor_units(amount):
    with pytest.raises(WalletError):
        minor(amount)


def test_currency_parse_is_strict_and_locale_explicit():
    assert money_text("€ 13,50", "EUR", ",") == 1350
    assert money_text("13.50 GBP", "GBP", ".") == 1350
    for raw in ("13.50", "13.50 GBP", "€ 13.50 or € 20.00", "€ 1,350", "-€13.50"):
        with pytest.raises(WalletError):
            money_text(raw, "EUR", ".")


async def test_confirmed_purchase_single_charge_and_durable_notifications(db, registry, menu):
    sessions = db[1]
    service, site, worker = await setup(sessions, registry)
    order = await service.request(101, 10)
    assert (await service.request(101, 11)).id == order.id  # Second filter/message, same item.
    assert await worker.once() and site.payments == 0
    async with sessions() as session:
        saved = await session.get(WalletOrder, order.id)
        assert saved.state == "quoted"
    sender = NotificationSender(sessions, menu.bot)
    assert await sender.once()
    sent = menu.transport.calls[-1]
    assert "13.50€" in sent.text and "Доставка: 2.00€" in sent.text
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == f"wallet:pay:{order.id}"
    assert (await service.decide(101, order.id, pay=True)).state == "queued"
    assert (await service.decide(101, order.id, pay=True)).state == "queued"

    async def barrier(current):
        async with sessions() as session:
            assert (await session.get(WalletOrder, current.id)).state == "paying"

    site.before_pay = barrier
    assert await worker.once() and site.payments == 1
    await WalletWorker(sessions, site.adapter).recover()
    assert not await worker.once()
    assert (await service.request(101, 10)).state == "succeeded"
    assert (await service.decide(101, order.id, pay=True)).state == "succeeded"
    assert site.payments == 1
    async with sessions() as session:
        assert (await wallet_account(session, 101)).active_order is None
        assert (await session.get(WalletOrder, order.id)).external_order_id == "order-456"
        assert await session.scalar(select(func.count()).select_from(Notification)) == 2


async def test_wrong_user_cannot_confirm_cancel_or_initiate(db, registry):
    service, _, worker = await setup(db[1], registry)
    order = await service.request(101, 10)
    await worker.once()
    for pay in (True, False):
        with pytest.raises(UserError, match="не знайдено"):
            await service.decide(202, order.id, pay=pay)
    with pytest.raises(UserError, match="не підключений"):
        await service.request(202, 10)


@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_and_expired_quote_never_pay(db, registry, cancel):
    service, site, worker = await setup(db[1], registry)
    order = await service.request(101, 10)
    await worker.once()
    if not cancel:
        async with db[1].begin() as session:
            (await session.get(WalletOrder, order.id)).expires_at = utcnow() - timedelta(seconds=1)
    result = await service.decide(101, order.id, pay=not cancel)
    assert result.state == ("cancelled" if cancel else "failed")
    assert not await worker.once() and site.payments == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"delivery_key": "pickup-2"},
        {"account_key": "other"},
        {"wallet_balance": 0},
        {"external_charge": 1},
    ],
)
async def test_changed_checkout_after_confirmation_never_pays(db, registry, changes):
    service, site, worker = await setup(db[1], registry)
    order = await service.request(101, 10)
    await worker.once()
    await service.decide(101, order.id, pay=True)
    site.quote = replace(site.quote, **changes)
    await worker.once()
    assert site.payments == 0
    async with db[1]() as session:
        assert (await session.get(WalletOrder, order.id)).state == "failed"


async def test_uncertain_payment_holds_account_and_never_retries(db, registry):
    service, site, worker = await setup(db[1], registry)
    order = await service.request(101, 10)
    await worker.once()
    await service.decide(101, order.id, pay=True)
    site.error = "payment"
    await worker.once()
    assert site.payments == 1
    await worker.recover()
    assert not await worker.once()
    assert (await service.decide(101, order.id, pay=True)).state == "unknown"
    async with db[1]() as session:
        assert (await wallet_account(session, 101)).active_order == order.id
        saved = await session.get(WalletOrder, order.id)
        assert saved.error == "ContractMismatch" and "secret" not in str(saved.quote)


@pytest.mark.parametrize(
    "state,expected", [("checking", "failed"), ("executing", "failed"), ("paying", "unknown")]
)
async def test_crash_recovery_before_and_after_payment_barrier(db, registry, state, expected):
    service, site, worker = await setup(db[1], registry)
    order = await service.request(101, 10)
    async with db[1].begin() as session:
        (await session.get(WalletOrder, order.id)).state = state
    await worker.recover()
    await worker.recover()
    assert not await worker.once() and site.payments == 0
    async with db[1]() as session:
        assert (await session.get(WalletOrder, order.id)).state == expected
        assert await session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_buy_disconnected_is_informative_without_changing_search(menu, services):
    await menu.send("/start")
    state = await (await menu.state()).get_data()
    user = await services[0].user(101)
    await menu.click(data="lot:buy")
    assert menu.transport.calls[-1].text == CONNECT_TEXT
    assert await (await menu.state()).get_data() == state
    assert (await services[0].user(101)).delivery_epoch == user.delivery_epoch


@pytest.mark.integration
async def test_pg_concurrent_double_click_one_order_and_one_confirmation(pg_db, registry):
    import asyncio

    sessions = pg_db[1]
    service, site, worker = await setup(sessions, registry)
    first, second = await asyncio.gather(service.request(101, 10), service.request(101, 11))
    assert first.id == second.id
    await worker.once()
    await asyncio.gather(
        service.decide(101, first.id, pay=True), service.decide(101, first.id, pay=True)
    )
    await worker.once()
    assert site.payments == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 1
