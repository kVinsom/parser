import asyncio
import json
from datetime import UTC, datetime, timedelta
from time import perf_counter
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update
from aiohttp.test_utils import TestClient, TestServer
from conftest import ChatHarness, OfflineSession
from redis.asyncio import Redis
from sqlalchemy import func, select, text

from vpars_bot.application import Catalog
from vpars_bot.config import Settings
from vpars_bot.db import ControlCommand, Inbox, Operation, utcnow
from vpars_bot.domain import Filters, UserError
from vpars_bot.latency import LatencyMetrics
from vpars_bot.reply_menu import make_menu_dispatcher
from vpars_bot.runtime import UpdateInbox, webhook_app
from vpars_bot.search_control import PersistentSearchControl
from vpars_bot.transport import (
    CallbackAcknowledger,
    RedisTelegramBudget,
    TelegramDeferred,
    TelegramRequestPolicy,
)
from vpars_bot.update_context import (
    SEARCH_START,
    SEARCH_STOP,
    UpdateContext,
    current_update,
    is_search_start,
    is_stop,
)


def incoming(number, user=101, text="hello", action=None):
    author = {"id": user, "is_bot": False, "first_name": "Test", "username": "alice"}
    message = {
        "message_id": number,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": user, "type": "private"},
        "from": author,
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"offset": 0, "length": len(text.split()[0]), "type": "bot_command"}]
    return Update.model_validate(
        {
            "update_id": number,
            **(
                {
                    "callback_query": {
                        "id": str(number),
                        "chat_instance": "test",
                        "from": author,
                        "message": message,
                        "data": action,
                    }
                }
                if action
                else {"message": message}
            ),
        }
    )


@pytest.mark.integration
async def test_migration_preserves_and_partitions_existing_pending_updates(pg_db, registry):
    from alembic import command
    from alembic.config import Config

    engine, sessions = pg_db
    catalog = Catalog(sessions, registry)
    await catalog.register(101, 101, "alice", "Test", 1, start=True)

    def migrate(connection, direction, revision):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        getattr(command, direction)(config, revision)

    async with engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0001")
        await connection.execute(text("UPDATE bot_users SET delivery_epoch = 7 WHERE id = 101"))
        await connection.execute(
            text("""INSERT INTO bot_inbox
                (bot_id, update_id, payload, state, attempts, next_attempt_at, received_at)
                VALUES (42, :id, CAST(:payload AS json), :state, 2, now(), now())"""),
            [
                {"id": 1, "payload": incoming(1).model_dump_json(), "state": "received"},
                {
                    "id": 2,
                    "payload": incoming(2, action="old:start").model_dump_json(),
                    "state": "processing",
                },
                {"id": 3, "payload": json.dumps(None), "state": "processed"},
            ],
        )
        await connection.run_sync(migrate, "upgrade", "head")
    async with sessions() as session:
        rows = (await session.scalars(select(Inbox).order_by(Inbox.update_id))).all()
        assert [row.lane_key for row in rows] == ["chat:101", "chat:101", "update:3"]
        assert [row.search_epoch for row in rows] == [7, 7, None]
        assert [row.state for row in rows] == ["received", "processing", "processed"]
        assert all(row.attempts == 2 and row.lease_token is None for row in rows)
        assert rows[1].payload["callback_query"]["data"] == "old:start"
    assert (await catalog.user(101)).delivery_epoch == 7


@pytest.mark.integration
async def test_slow_chat_does_not_block_others_and_fifo_stays_intact(pg_db):
    inbox = UpdateInbox(pg_db[1], 42, concurrency=2)
    await inbox.receive_many([incoming(1, 101), incoming(2, 101), incoming(3, 202)])
    entered, release, other_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    class Dispatcher:
        async def feed_update(self, bot, update):
            if update.update_id == 1:
                entered.set()
                await release.wait()
            order.append(update.update_id)
            if update.update_id == 3:
                other_finished.set()

    task = asyncio.create_task(inbox.pump(Dispatcher(), None))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(other_finished.wait(), 3)
        assert order == [3]
        release.set()
        async with asyncio.timeout(3):
            while len(order) < 3:
                await asyncio.sleep(0.01)
        assert order == [3, 1, 2]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.integration
async def test_long_429_wait_preserves_chat_order_without_blocking_other_chats(pg_db):
    inbox = UpdateInbox(pg_db[1], 42)
    await inbox.receive_many([incoming(1, 101), incoming(2, 101), incoming(3, 202)])
    seen = []

    class Dispatcher:
        async def feed_update(self, bot, update):
            seen.append(update.update_id)
            if update.update_id == 1:
                raise TelegramRetryAfter(
                    SendMessage(chat_id=101, text="test"), "slow down", retry_after=90
                )

    started = utcnow()
    await inbox.once(Dispatcher(), None)
    await inbox.once(Dispatcher(), None)
    assert seen == [1, 3]
    assert not await inbox.once(Dispatcher(), None)
    async with pg_db[1]() as session:
        row = await session.get(Inbox, (42, 1))
        assert row.next_attempt_at >= started + timedelta(seconds=90)
        assert row.attempts == 0 and row.state == "received"


@pytest.mark.integration
async def test_leases_prevent_duplicate_claims_and_fence_expired_completion(pg_db):
    inbox = UpdateInbox(pg_db[1], 42)
    await inbox.receive(incoming(1))
    first, second = await asyncio.gather(inbox.claim(), inbox.claim())
    old = first or second
    assert (first is None) != (second is None)
    async with pg_db[1].begin() as session:
        row = await session.get(Inbox, (42, 1))
        row.lease_until = utcnow() - timedelta(seconds=1)
    new = await inbox.claim()
    assert new.lease_token != old.lease_token
    dp = AsyncMock()
    await inbox.process(old, dp, None)
    async with pg_db[1]() as session:
        row = await session.get(Inbox, (42, 1))
        assert row.state == "processing" and row.lease_token == new.lease_token
    await inbox.process(new, dp, None)
    async with pg_db[1]() as session:
        assert (await session.get(Inbox, (42, 1))).state == "processed"


@pytest.mark.integration
@pytest.mark.parametrize("menu_mode", ["full", "menu"])
async def test_stop_commits_at_admission_and_fences_older_start(pg_db, registry, menu_mode):
    catalog = Catalog(pg_db[1], registry)
    control = PersistentSearchControl(pg_db[1], registry)
    await catalog.register(101, 101, "alice", "Test", 1, start=True)
    item = await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "save")
    await control.start(101, "running")
    inbox = UpdateInbox(pg_db[1], 42, control=control, menu_mode=menu_mode)
    older_start = (
        incoming(11, action="old:start") if menu_mode == "full" else incoming(11, text=SEARCH_START)
    )
    stop = incoming(12, text="/stop" if menu_mode == "full" else SEARCH_STOP)
    newer_start = (
        incoming(13, action="new:start") if menu_mode == "full" else incoming(13, text=SEARCH_START)
    )
    # A slow earlier menu reply must not prevent Stop's local effect.
    await inbox.receive(incoming(10))
    async with pg_db[1].begin() as session:
        (await session.get(Inbox, (42, 10))).next_attempt_at = utcnow() + timedelta(seconds=90)
    await inbox.receive_many([older_start, stop, newer_start])
    assert (await control.status(101))[item.id] == "stopped"
    async with pg_db[1]() as session:
        before = await session.get(Inbox, (42, 11))
        after = await session.get(Inbox, (42, 13))
        assert before.search_epoch == 0 and after.search_epoch == 1
    token = current_update.set(UpdateContext(11, before.search_epoch))
    try:
        with pytest.raises(UserError, match="до зупинки"):
            await control.start(101, "old-start")
    finally:
        current_update.reset(token)
    token = current_update.set(UpdateContext(13, after.search_epoch))
    try:
        await control.start(101, "new-start")
    finally:
        current_update.reset(token)
    # Redelivery of the old Stop cannot stop the newer run again.
    await inbox.receive(stop)
    assert (await control.status(101))[item.id] == "requested"
    assert (await catalog.user(101)).delivery_epoch == 1


@pytest.mark.integration
@pytest.mark.parametrize("menu_mode", ["full", "menu"])
async def test_admission_stop_rolls_back_with_failed_inbox_write(
    pg_db, registry, monkeypatch, menu_mode
):
    from sqlalchemy.ext.asyncio import AsyncSession

    catalog = Catalog(pg_db[1], registry)
    control = PersistentSearchControl(pg_db[1], registry)
    await catalog.register(101, 101, "alice", "Test", 1, start=True)
    original = AsyncSession.execute

    async def fail_insert(self, statement, *args, **kwargs):
        if getattr(statement, "is_insert", False) and statement.table.name == "bot_inbox":
            raise ConnectionError("inbox insert failed")
        return await original(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_insert)
    with pytest.raises(ConnectionError):
        await UpdateInbox(pg_db[1], 42, control=control, menu_mode=menu_mode).receive(
            incoming(2, text="/stop" if menu_mode == "full" else SEARCH_STOP)
        )
    assert (await catalog.user(101)).delivery_epoch == 0
    async with pg_db[1]() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 0


@pytest.mark.integration
async def test_reply_search_inbox_stops_once_and_rejects_old_start(pg_db, registry):
    catalog = Catalog(pg_db[1], registry)
    control = PersistentSearchControl(pg_db[1], registry)
    bot = Bot("42:offline", session=OfflineSession())
    dispatcher = make_menu_dispatcher(MemoryStorage(), SimpleEventIsolation(), catalog, control)
    chat = ChatHarness(dispatcher, bot, bot.session)
    inbox = UpdateInbox(pg_db[1], bot.id, control=control, menu_mode="menu")
    try:
        await chat.send("/start")
        item = await catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), "saved")
        await inbox.receive_many(
            [
                incoming(10, text=SEARCH_START),
                incoming(11, text=SEARCH_STOP),
                incoming(12, text=SEARCH_START),
            ]
        )
        assert (await catalog.user(101)).delivery_epoch == 1
        assert await inbox.once(dispatcher, bot)
        assert "до зупинки" in bot.session.calls[-1].text
        assert (await control.status(101))[item.id] == "stopped"
        assert await inbox.once(dispatcher, bot)
        assert "❌ Пошук завершений" in chat.panel().text
        assert (await catalog.user(101)).delivery_epoch == 1
        assert await inbox.once(dispatcher, bot)
        assert (await control.status(101))[item.id] == "requested"
        assert "✅ Пошук початий" in chat.panel().text
        async with pg_db[1]() as session:
            commands = list(await session.scalars(select(ControlCommand)))
            assert sorted(c.kind for c in commands) == ["start", "stop"]
            assert all(row.state == "processed" for row in await session.scalars(select(Inbox)))
        # Old inline buttons are ignored at admission AND in the reply-menu handler.
        await inbox.receive(incoming(13, action="old:stop"))
        assert await inbox.once(dispatcher, bot)
        await inbox.receive(incoming(11, text=SEARCH_STOP))
        assert not await inbox.once(dispatcher, bot)
        assert (await control.status(101))[item.id] == "requested"
        assert (await catalog.user(101)).delivery_epoch == 1
    finally:
        await dispatcher.fsm.close()
        await bot.session.close()


@pytest.mark.parametrize("menu_mode", ["menu", "full", "preview"])
def test_search_admission_matches_active_menu_and_private_owner(menu_mode):
    for label in (SEARCH_START, "▶️ Почати пошук"):
        assert is_search_start(incoming(1, text=label), menu_mode=menu_mode) == (
            menu_mode == "menu"
        )
    for label in (SEARCH_STOP, "⏹ Завершити пошук"):
        assert is_stop(incoming(2, text=label), menu_mode=menu_mode) == (menu_mode == "menu")
    assert is_stop(incoming(3, text="/stop"), menu_mode=menu_mode) == (menu_mode != "preview")
    assert is_stop(incoming(4, action="old:stop"), menu_mode=menu_mode) == (menu_mode == "full")
    assert is_search_start(incoming(5, action="old:start"), menu_mode=menu_mode) == (
        menu_mode == "full"
    )
    for label in (SEARCH_START, SEARCH_STOP, "▶️ Почати пошук", "⏹ Завершити пошук"):
        payload = incoming(6, text=label).model_dump()
        payload["message"]["chat"]["type"] = "group"
        update = Update.model_validate(payload)
        assert not is_stop(update, menu_mode=menu_mode)
        assert not is_search_start(update, menu_mode=menu_mode)
        payload["message"]["chat"]["type"] = "private"
        payload["message"]["from_user"]["id"] = 202
        update = Update.model_validate(payload)
        assert not is_stop(update, menu_mode=menu_mode)
        assert not is_search_start(update, menu_mode=menu_mode)


@pytest.mark.integration
async def test_webhook_button_ack_is_in_response_after_persistence(pg_db):
    settings = Settings(
        _env_file=None,
        update_mode="webhook",
        webhook_base_url="https://example.com",
        webhook_secret="test_" + "s" * 32,
    )
    inbox = UpdateInbox(pg_db[1], 42)
    async with TestClient(TestServer(webhook_app(settings, inbox))) as client:
        response = await client.post(
            "/telegram",
            json=incoming(7, action="view:home").model_dump(mode="json", exclude_none=True),
            headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret.get_secret_value()},
        )
        assert await response.json() == {"method": "answerCallbackQuery", "callback_query_id": "7"}
        async with pg_db[1]() as session:
            assert await session.get(Inbox, (42, 7))


async def test_failed_edit_retries_pending_screen_without_advancing_twice(chat, monkeypatch):
    await chat.send("/start")
    await chat.click("Фільтр")
    await chat.click("Додати предмет")
    data = chat.button("Далі")
    update = incoming(100, action=data)
    original = chat.transport.make_request

    async def unavailable(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramRetryAfter(method, "slow down", retry_after=20)
        return await original(bot, method, timeout)

    monkeypatch.setattr(chat.transport, "make_request", unavailable)
    with pytest.raises(TelegramRetryAfter):
        await chat.dp.feed_update(chat.bot, update)
    saved = await (await chat.state()).get_data()
    assert saved["draft"]["step"] == 1 and saved["pending_panel"]
    monkeypatch.setattr(chat.transport, "make_request", original)
    await chat.dp.feed_update(chat.bot, update)
    restored = await (await chat.state()).get_data()
    assert restored["draft"]["step"] == 1 and restored["pending_panel"] is None
    assert "Який розмір" in chat.panel().text


async def test_identical_panel_avoids_unnecessary_edit(chat):
    await chat.send("/start")
    await chat.click("Фільтр")
    await chat.click("Додати предмет")
    before = sum(isinstance(call, EditMessageText) for call in chat.transport.calls)
    await chat.click("Додати ще")
    assert sum(isinstance(call, EditMessageText) for call in chat.transport.calls) == before


async def test_ack_failure_never_prevents_stop(chat, services, monkeypatch):
    await chat.send("/start")
    item = await services[0].save(101, Filters(sources={"vinted": ["FR"]}), "save")
    await services[1].start(101, "start")
    original = chat.transport.make_request

    async def ack_failure(bot, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            raise TelegramNetworkError(method, "offline")
        return await original(bot, method, timeout)

    monkeypatch.setattr(chat.transport, "make_request", ack_failure)
    await chat.click("Завершити пошук")
    assert (await services[1].status(101))[item.id] == "stopped"


async def test_callback_ack_is_bounded_and_nonblocking():
    bot = AsyncMock()
    release = asyncio.Event()

    async def slow(*args, **kwargs):
        await release.wait()

    bot.answer_callback_query.side_effect = slow
    metrics = LatencyMetrics()
    ack = CallbackAcknowledger(bot, metrics, timeout=0.02, capacity=2)
    started = perf_counter()
    for i in range(3):
        ack.offer(incoming(i + 1, action="view:home"))
    assert perf_counter() - started < 0.1 and len(ack.tasks) == 2
    await asyncio.gather(*ack.tasks)
    assert metrics.counters["callback_ack_overload"] == 1
    assert metrics.counters["callback_ack_failed"] == 2
    await ack.close()


@pytest.mark.parametrize("rich", [False, True])
async def test_request_policy_records_429_and_does_not_retry_uncertain_sends(rich, monkeypatch):
    from aiogram.methods import SendRichMessage
    from aiogram.types import InputRichMessage

    budget, request = AsyncMock(), AsyncMock()
    metrics = LatencyMetrics()
    policy = TelegramRequestPolicy(budget, metrics)
    method = (
        SendRichMessage(chat_id=101, rich_message=InputRichMessage(html="<p>Test</p>"))
        if rich
        else SendMessage(chat_id=101, text="Test")
    )
    request.side_effect = TelegramRetryAfter(method, "limited", retry_after=90)
    with pytest.raises(TelegramRetryAfter):
        await policy(request, None, method)
    budget.cooldown.assert_awaited_once_with(90)
    budget.acquire.assert_awaited_once_with(101)
    assert request.await_count == 1 and metrics.counters["telegram_429"] == 1
    later = perf_counter() + 91
    monkeypatch.setattr("vpars_bot.transport.perf_counter", lambda: later)
    request.reset_mock()
    request.side_effect = TelegramNetworkError(method, "uncertain outcome")
    with pytest.raises(TelegramNetworkError):
        await policy(request, None, method)
    assert request.await_count == 1


async def test_album_uses_shared_rate_budget_for_every_photo():
    from aiogram.methods import SendMediaGroup
    from aiogram.types import InputMediaPhoto

    budget, request = AsyncMock(), AsyncMock()
    policy = TelegramRequestPolicy(budget, LatencyMetrics())
    method = SendMediaGroup(
        chat_id=101,
        media=[InputMediaPhoto(media=f"https://images1.vinted.net/{i}") for i in range(10)],
    )
    await policy(request, None, method)
    budget.acquire.assert_awaited_once_with(101, cost=10)
    request.side_effect = TelegramRetryAfter(method, "limited", retry_after=90)
    with pytest.raises(TelegramRetryAfter):
        await policy(request, None, method)
    budget.cooldown.assert_awaited_once_with(90)


@pytest.mark.integration
async def test_redis_rate_budget_shared_between_instances(pg_db):
    import os

    redis = Redis.from_url(os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:16379/0"))
    prefix = "test-budget-" + uuid4().hex
    a = RedisTelegramBudget(redis, 42, per_second=25, chat_interval=1, prefix=prefix)
    b = RedisTelegramBudget(redis, 42, per_second=25, chat_interval=1, prefix=prefix)
    try:
        # Warm both pool connections: this checks atomic admission, not DNS/connect timing.
        await asyncio.gather(redis.ping(), redis.ping())
        outcomes = await asyncio.gather(a.acquire(101), b.acquire(101), return_exceptions=True)
        assert sum(isinstance(outcome, TelegramDeferred) for outcome in outcomes) == 1
        await a.cooldown(90)
        await b.cooldown(1)  # A short later response must never shorten the shared cooldown.
        with pytest.raises(TelegramDeferred) as exc:
            await b.acquire(202)
        assert exc.value.retry_after > 88
        assert await redis.pttl(a.prefix + ":cooldown") > 88000
    finally:
        keys = [key async for key in redis.scan_iter(match=prefix + ":*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


def test_latency_metrics_bounded_and_does_not_include_payloads():
    metrics = LatencyMetrics(window=3)
    for i in range(100):
        metrics.observe("telegram.sendMessage", i / 1000)
    result = metrics.snapshot()
    assert result["milliseconds"]["telegram.sendMessage"] == {
        "samples": 3,
        "p50": 98,
        "p95": 99,
        "max": 99,
    }


def test_latency_p95_does_not_understate_small_samples():
    metrics = LatencyMetrics()
    metrics.observe("sample", 0.01)
    metrics.observe("sample", 0.03)
    result = metrics.snapshot()["milliseconds"]["sample"]
    assert result["p50"] == 20 and result["p95"] == result["max"] == 30
