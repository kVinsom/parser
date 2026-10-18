"""Telegram Buy -> full quote -> Approve, with durable wallet-only payment."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from conftest import ChatHarness, OfflineSession
from sqlalchemy import select
from test_wallet_fast import account, pool_for
from test_wallet_mock import checkout_mock as checkout_mock
from test_wallet_mock import configure, ledger, saved, seed

from vpars_bot.config import Settings
from vpars_bot.db import Notification, WalletAccount, WalletOrder, WalletSession, utcnow
from vpars_bot.notifications import NotificationSender
from vpars_bot.reply_menu import make_menu_dispatcher
from vpars_bot.wallet import WalletError, WalletService
from vpars_bot.wallet_accounts import wallet_session
from vpars_bot.wallet_handoff_web import handoff_server
from vpars_bot.wallet_mode import button_checkout, checkout_mode
from vpars_bot.wallet_worker import WalletWorker


async def connected(sessions):
    async with sessions.begin() as session:
        for row in await session.scalars(select(WalletAccount)):
            session.add(WalletSession(
                user_id=row.user_id, region=row.region, attempt_id=f"session-{row.user_id}",
                state="checkout_ready", account_key=f"mock-{row.user_id}",
            ))


@pytest.mark.parametrize("url", ["", "https://old-checkout.example"])
async def test_bot_mode_ignores_old_window_url_and_restores_policy(db, url):
    settings = Settings(_env_file=None, wallet_checkout_mode="bot", wallet_handoff_url=url)
    assert settings.checkout_window_url == ""
    assert not button_checkout(db[1])
    async with handoff_server(db[1], settings) as handoff:
        assert handoff is None and button_checkout(db[1])
    assert not button_checkout(db[1])


@pytest.mark.parametrize("user_id", [101, 202, 303, 404])
@pytest.mark.parametrize("message_id,total", [(10, 1350), (12, 1550)])
async def test_buy_waits_for_approve_for_all_regions_brands_and_notifies_chat(
    db, registry, checkout_mock, user_id, message_id, total,
):
    sessions = db[1]
    service, _ = await seed(sessions, registry, checkout_mock)
    await connected(sessions)
    with checkout_mode(sessions, "bot"):
        async with pool_for(checkout_mock, sessions) as pool:
            worker = WalletWorker(sessions, pool.adapter)
            order = await service.request(user_id, message_id)
            assert order.state == "bot_requested"
            assert await worker.once()
            assert (await saved(sessions, order)).state == "bot_quoted"
            assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 0
            await worker.recover()
            assert not await worker.once()
            transport = OfflineSession()
            async with Bot("42:offline", session=transport) as bot:
                sender = NotificationSender(sessions, bot)
                assert await sender.once() and not await sender.once()
                message = transport.calls[-1]
                symbol = "£" if order.currency == "GBP" else "€"
                assert f"Підтвердіть автобай на {total / 100:.2f}{symbol}" in message.text
                assert "Доставка:" in message.text and "Захист покупця:" in message.text
                button = message.reply_markup.inline_keyboard[0][0]
                assert button.text == "Approve"
                assert button.callback_data == f"wallet:approve:{order.id}"
            assert (await service.decide(user_id, order.id, pay=True, approve=True)).state == "bot_queued"
            assert (await service.decide(user_id, order.id, pay=True, approve=True)).state == "bot_queued"
            assert await worker.once()
            assert (await saved(sessions, order)).state == "succeeded"
            assert (await ledger(checkout_mock, user_id))["balance"] == 5000 - total
            assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 1
            assert (await service.request(user_id, message_id)).id == order.id
            if message_id == 10:
                assert (await service.request(user_id, 11)).id == order.id
            await worker.recover()
            assert not await worker.once()
            transport = OfflineSession()
            async with Bot("42:offline", session=transport) as bot:
                sender = NotificationSender(sessions, bot)
                assert await sender.once() and not await sender.once()
                assert transport.calls[-1].chat_id == user_id
                assert "Покупку оформлено" in transport.calls[-1].text
                assert transport.calls[-1].reply_markup is None
            async with sessions() as session:
                assert await session.scalar(select(Notification).where(
                    Notification.id == f"wallet:{order.id}:bot_quoted",
                ))


@pytest.mark.parametrize("changes,error", [
    ({"balance": 1349}, "InsufficientBalance"),
    ({"balance": 1000}, "InsufficientBalance"),  # Item price alone is not enough.
    ({"external_charge": 1}, "WalletOnly"),
    ({"payment_method": "Card"}, "WalletOnly"),
    ({"shipping": 1500}, "BudgetExceeded"),
    ({"delivery": None}, "ManualAction"),
])
async def test_bot_stops_unsafe_checkout_without_manual_handoff(db, registry, checkout_mock, changes, error):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    await configure(checkout_mock, **changes)
    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        await worker.once()
        result = await saved(db[1], order)
        assert (result.state, result.error) == ("failed", error)
        assert (await ledger(checkout_mock))["payment_attempts"] == 0
        async with db[1]() as session:
            note = await session.get(Notification, f"wallet:{order.id}:failed")
            assert "reply_markup" not in note.payload
        assert (await account(db[1])).active_order is None


async def test_exact_balance_pays_and_reuses_session_for_next_item(db, registry, checkout_mock):
    service, _ = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        async with pool_for(checkout_mock, db[1]) as pool:
            worker = WalletWorker(db[1], pool.adapter)
            first = await service.request(101, 10)
            await worker.once()
            await service.decide(101, first.id, pay=True, approve=True)
            await worker.once()
            page = pool.entries[(101, "DE")].adapter.page
            await configure(checkout_mock, balance=1550)
            second = await service.request(101, 12)
            await worker.once()
            await service.decide(101, second.id, pay=True, approve=True)
            await worker.once()
            assert pool.entries[(101, "DE")].adapter.page is page
            assert (await saved(db[1], first)).state == (await saved(db[1], second)).state == "succeeded"
            assert (await ledger(checkout_mock))["balance"] == 0
            assert (await ledger(checkout_mock))["payment_attempts"] == 2
            assert checkout_mock.site.requests.count(("GET", "/items/456")) == 1


@pytest.mark.parametrize("field,value", [
    ("attempt_id", "replacement"), ("account_key", "different-account"),
    ("error", "CheckoutPaused"), ("state", "failed"),
])
@pytest.mark.parametrize("after_checkout", [False, True])
async def test_rebound_or_revoked_session_never_pays(db, registry, checkout_mock, field, value, after_checkout):
    service, _ = await seed(db[1], registry, checkout_mock)
    await connected(db[1])

    async def change():
        async with db[1].begin() as session:
            setattr(await wallet_session(session, 101, "DE"), field, value)

    @asynccontextmanager
    async def factory(acc):
        async with checkout_mock.bridge.adapter(acc) as adapter:
            original = adapter.checkout

            async def checkout(order):
                quote = await original(order)
                await change()
                return quote

            if after_checkout:
                adapter.checkout = checkout
            yield adapter

    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        if not after_checkout:
            await change()
        await WalletWorker(db[1], factory).once()
        assert (await saved(db[1], order)).state == "failed"
        assert (await ledger(checkout_mock))["payment_attempts"] == 0


@pytest.mark.parametrize("state", ["bot_checking", "bot_executing", "paying"])
async def test_restart_never_replays_interrupted_or_ambiguous_buy(db, registry, checkout_mock, state):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        async with db[1].begin() as session:
            (await session.get(WalletOrder, order.id)).state = state
        await worker.recover()
        result = await saved(db[1], order)
        assert result.state == ("unknown" if state == "paying" else "failed")
        assert not await worker.once()
        assert (await service.request(101, 10)).state == result.state
        assert (await ledger(checkout_mock))["payment_attempts"] == 0


async def test_stale_buy_and_mode_switch_do_not_create_payment_consent(db, registry, checkout_mock):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    legacy = await service.request(101, 10)
    with checkout_mode(db[1], "bot"):
        assert not await worker.once()  # Old two-step Buy must not become one-click consent.
        order = await service.request(202, 12)
        async with db[1].begin() as session:
            (await session.get(WalletOrder, order.id)).expires_at = utcnow() - timedelta(seconds=1)
        await worker.once()
        assert (await saved(db[1], order)).error == "Expired"
    assert (await saved(db[1], legacy)).state == "requested"
    assert (await ledger(checkout_mock, 202))["payment_attempts"] == 0


async def test_cached_session_reconnect_evicts_old_context_and_pause_rejects_stale_account(db, registry, checkout_mock):
    await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        async with pool_for(checkout_mock, db[1]) as pool:
            stale = await account(db[1])
            async with pool.adapter(stale) as adapter:
                first = adapter.page
            async with db[1].begin() as session:
                (await wallet_session(session, 101, "DE")).attempt_id = "reconnected"
            async with pool.adapter(stale) as adapter:
                assert first.is_closed() and adapter.page is not first
                second = adapter.page
            async with db[1].begin() as session:
                (await wallet_session(session, 101, "DE")).error = "CheckoutPaused"
            with pytest.raises(WalletError, match="NotConnected"):
                async with pool.adapter(stale):
                    pytest.fail("A warm connection is not authorization")
            await pool.maintain()
            assert second.is_closed() and not pool.entries


async def test_real_telegram_callback_needs_approve_and_preserves_menu(services, registry, checkout_mock, monkeypatch):
    sessions = services[0].sessions
    service, worker = await seed(sessions, registry, checkout_mock)
    await connected(sessions)

    async def delivered(self, uid, mid):
        assert uid == 101
        return await service.resolver(uid, 10)

    monkeypatch.setattr(WalletService, "delivered_item", delivered)
    storage = MemoryStorage()
    transport = OfflineSession()
    async with Bot("42:offline", session=transport) as bot:
        dispatcher = make_menu_dispatcher(storage, SimpleEventIsolation(), services[0], services[1], admins=services[2])
        menu = ChatHarness(dispatcher, bot, transport)
        try:
            await menu.send("/start")
            before = await (await menu.state()).get_data()
            with checkout_mode(sessions, "bot"):
                await menu.click(data="lot:buy")
                await menu.click(data="lot:buy")
                await worker.once()
                assert (await ledger(checkout_mock))["payment_attempts"] == 0
                sender = NotificationSender(sessions, bot)
                assert await sender.once()
                button = transport.calls[-1].reply_markup.inline_keyboard[0][0]
                assert button.text == "Approve"
                await menu.click(data=button.callback_data)
                await menu.click(data=button.callback_data)
                await worker.once()
                assert (await ledger(checkout_mock))["payment_attempts"] == 1
                assert not await worker.once()
                assert await (await menu.state()).get_data() == before
        finally:
            await storage.close()


@pytest.mark.integration
async def test_concurrent_buy_commits_one_order_and_payment(pg_db, registry, checkout_mock):
    import asyncio

    sessions = pg_db[1]
    service, worker = await seed(sessions, registry, checkout_mock)
    await connected(sessions)
    with checkout_mode(sessions, "bot"):
        first, duplicate = await asyncio.gather(service.request(101, 10), service.request(101, 11))
        assert first.id == duplicate.id
        await worker.once()
        assert (await ledger(checkout_mock))["payment_attempts"] == 0
        approved, duplicate_approval = await asyncio.gather(
            service.decide(101, first.id, pay=True, approve=True),
            service.decide(101, first.id, pay=True, approve=True),
        )
        assert approved.state == duplicate_approval.state == "bot_queued"
        await worker.once()
        assert (await saved(sessions, first)).state == "succeeded"
        assert not await worker.once()
        assert (await ledger(checkout_mock))["payment_attempts"] == 1


@pytest.mark.parametrize("error", [
    "CheckoutChallenge", "CheckoutLoginRequired", "CheckoutRejected", "CheckoutPhoneRequired",
])
async def test_site_block_stops_before_payment_preserves_session_and_notifies_chat(db, registry, checkout_mock, error):
    service, _ = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    calls = 0

    @asynccontextmanager
    async def blocked(account):
        nonlocal calls
        calls += 1
        raise WalletError(error)
        yield  # pragma: no cover - context manager protocol

    with checkout_mode(db[1], "bot"):
        worker = WalletWorker(db[1], blocked)
        order = await service.request(101, 10)
        await worker.once()
        result = await saved(db[1], order)
        assert (result.state, result.error) == ("failed", error)
        await worker.recover()
        assert not await worker.once()
        assert (await service.request(101, 11)).id == order.id
        assert calls == 1
        assert (await ledger(checkout_mock))["payment_attempts"] == 0
        async with db[1]() as session:
            note = await session.get(Notification, f"wallet:{order.id}:failed")
            assert "reply_markup" not in note.payload
            assert "в цьому вікні" not in note.payload["text"]
            connection = await wallet_session(session, 101, "DE")
            assert connection.attempt_id == "session-101"
            assert connection.account_key == "mock-101"


@pytest.mark.parametrize("state", ["requested", "queued", "quoted", "human_waiting", "human_active", "balance_waiting"])
async def test_mode_change_releases_old_unpaid_orders_but_keeps_ambiguous_manual_payment(db, registry, checkout_mock, state):
    service, worker = await seed(db[1], registry, checkout_mock)
    order = await service.request(101, 10)
    async with db[1].begin() as session:
        (await session.get(WalletOrder, order.id)).state = state
    with checkout_mode(db[1], "bot"):
        await worker.recover()
        result = await saved(db[1], order)
        assert result.state == ("unknown" if state == "human_active" else "failed")
        assert bool((await account(db[1])).active_order) == (state == "human_active")
        assert not await worker.once()
        assert (await ledger(checkout_mock))["payment_attempts"] == 0
