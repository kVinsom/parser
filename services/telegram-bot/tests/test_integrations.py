import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Update
from aiohttp.test_utils import TestClient, TestServer
from alembic import command
from alembic.config import Config
from conftest import ChatHarness, OfflineSession
from redis.asyncio import Redis
from sqlalchemy import func, inspect, select, text
from test_application import register, token_for

from vpars_bot.application import AdminService, Catalog
from vpars_bot.config import Settings
from vpars_bot.db import ControlCommand, Inbox, RoleEvent, User, utcnow
from vpars_bot.dialogs import new_draft
from vpars_bot.domain import Filters, UserError
from vpars_bot.runtime import UpdateInbox, exclusive_receiver, webhook_app
from vpars_bot.search_control import PersistentSearchControl
from vpars_bot.telegram import BotUI, make_dispatcher

pytestmark = pytest.mark.integration


async def test_postgres_migrations_concurrent_saves_and_starts(pg_db, registry):
    engine, sessions = pg_db
    catalog = Catalog(sessions, registry)
    control = PersistentSearchControl(sessions, registry)
    await register(catalog, 101, "alice", 1)
    filters = Filters(models=["Nike"], sources={"vinted": ["FR"]})
    items = await asyncio.gather(*(catalog.save(101, filters, "same-operation") for _ in range(6)))
    assert len({item.id for item in items}) == 1
    assert (await catalog.list(101))[1] == 1
    await asyncio.gather(*(control.start(101, f"click-{i}") for i in range(6)))
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ControlCommand)
                .where(ControlCommand.kind == "start")
            )
            == 1
        )
    token = await token_for(sessions)
    assert await control.acknowledge(token)
    await asyncio.gather(control.stop(101, "stop"), control.acknowledge(token))
    assert not await control.can_deliver(token)
    assert not await control.acknowledge(token)
    assert await control.stop(101, "stop") == 1
    async with engine.connect() as connection:
        assert "bot_searches" in await connection.run_sync(lambda c: inspect(c).get_table_names())


async def test_concurrent_last_admin_removals(pg_db, registry, profiles):
    _, sessions = pg_db
    catalog = Catalog(sessions, registry)
    admins = AdminService(sessions, profiles)
    await register(catalog, 101, "kV1nsom", 1)
    await register(catalog, 202, "bob", 2)
    profiles.names = {101: "kv1nsom", 202: "bob"}
    await admins.bootstrap(101)
    await admins.change(101, 202, "bob", True, False, "grant")
    results = await asyncio.gather(
        admins.change(101, 202, "bob", False, True, "revoke-bob"),
        admins.change(202, 101, "kv1nsom", False, True, "revoke-owner"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, UserError) for result in results) == 1
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(User).where(User.is_admin.is_(True))
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(RoleEvent)) == 3


async def test_delayed_start_cannot_undo_stop(pg_db, registry):
    _, sessions = pg_db
    catalog = Catalog(sessions, registry)
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowCapabilities:
        async def issues(self, filters):
            entered.set()
            await release.wait()
            return []

    control = PersistentSearchControl(sessions, registry, SlowCapabilities())
    await register(catalog, 101, "alice", 1)
    item = await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "save")
    start = asyncio.create_task(control.start(101, "slow-start"))
    await entered.wait()
    await control.stop(101, "stop")
    release.set()
    with pytest.raises(UserError, match="зупинено"):
        await start
    assert (await control.status(101))[item.id] == "stopped"


async def test_redis_dialog_restart_and_expiry(pg_db, registry, profiles):
    _, sessions = pg_db
    redis_url = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:16379/0")
    prefix = "test-bot-" + uuid4().hex

    def storage():
        return RedisStorage(
            redis=Redis.from_url(redis_url),
            key_builder=DefaultKeyBuilder(prefix=prefix, with_bot_id=True),
            state_ttl=60,
            data_ttl=60,
        )

    store = storage()
    key = StorageKey(bot_id=42, chat_id=101, user_id=101)
    state = FSMContext(store, key)
    await state.set_state("filters")
    await state.set_data({"draft": new_draft(), "screen": "abcd"})
    await store.close()
    restored = storage()
    try:
        again = FSMContext(restored, key)
        assert await again.get_state() == "filters"
        assert (await again.get_data())["draft"]["kind"] == "filters"
        assert await restored.redis.ttl(restored.key_builder.build(key, "data")) > 0
        catalog = Catalog(sessions, registry)
        control = PersistentSearchControl(sessions, registry)
        ui = BotUI(catalog, control, AdminService(sessions, profiles), registry)
        transport = OfflineSession()
        bot = Bot("42:offline", session=transport)
        dp = make_dispatcher(restored, restored.create_isolation(), ui)
        chat = ChatHarness(dp, bot, transport)
        await chat.send("/start")
        await chat.click("Фільтр")
        await chat.click("Додати предмет")
        await chat.send("Nike")
        await chat.click("Далі")
        assert (await again.get_data())["draft"]["step"] == 1
        assert (await again.get_data())["draft"]["values"]["models"] == ["Nike"]
        await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "saved")
        stale = chat.button("Далі")
        await again.clear()
        await chat.click(data=stale)
        assert "застаріла" in chat.transport.calls[-1].text
        assert (await catalog.list(101))[1] == 1
        await bot.session.close()
    finally:
        # Only this test's random key namespace is removed.
        keys = [key async for key in restored.redis.scan_iter(match=prefix + ":*")]
        if keys:
            await restored.redis.delete(*keys)
        await restored.close()


async def test_durable_inbox_duplicate_and_crash_recovery(pg_db):
    _, sessions = pg_db
    inbox = UpdateInbox(sessions, 42)
    update = Update(update_id=1)
    await asyncio.gather(inbox.receive(update), inbox.receive(update))
    assert await inbox.offset() == 2

    class Dispatcher:
        calls = 0

        async def feed_update(self, bot, update):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Simulated connection loss")

    dp = Dispatcher()
    await inbox.once(dp, None)
    async with sessions.begin() as session:
        row = await session.get(Inbox, (42, 1))
        assert row.attempts == 1 and row.state == "received"
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        row.state = "processing"  # Process terminated while working.
    restarted = UpdateInbox(sessions, 42)
    await restarted.once(dp, None)
    await restarted.receive(update)
    assert not await restarted.once(dp, None)
    async with sessions() as session:
        row = await session.get(Inbox, (42, 1))
        assert row.state == "processed" and row.payload is None
        assert await session.scalar(select(func.count()).select_from(Inbox)) == 1


async def test_webhook_auth_persists_before_ok_and_rejects_bad_data(pg_db):
    _, sessions = pg_db
    secret = "test_" + "s" * 32
    settings = Settings(
        _env_file=None,
        update_mode="webhook",
        webhook_base_url="https://example.com",
        webhook_secret=secret,
    )
    inbox = UpdateInbox(sessions, 42)
    async with TestClient(TestServer(webhook_app(settings, inbox))) as client:
        response = await client.post("/telegram", json={"update_id": 5})
        assert response.status == 403
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret}
        response = await client.post("/telegram", json={"update_id": 5}, headers=headers)
        assert response.status == 200
        async with sessions() as session:
            assert (await session.get(Inbox, (42, 5))).state == "received"
        assert (
            await client.post("/telegram", json={"update_id": 5}, headers=headers)
        ).status == 200
        assert (await client.post("/telegram", json={}, headers=headers)).status == 400
        assert (await client.get("/health")).status == 200


async def test_polling_and_webhook_cannot_coexist(pg_db):
    engine, _ = pg_db
    bot_id = int(uuid4().hex[:6], 16)
    async with exclusive_receiver(engine, bot_id):
        with pytest.raises(RuntimeError, match="already running"):
            async with exclusive_receiver(engine, bot_id):
                pass
    async with exclusive_receiver(engine, bot_id):
        pass


async def test_migration_downgrade_upgrade_and_no_drift(pg_db):
    engine, _ = pg_db

    def roundtrip(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.check(config)
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        command.check(config)

    async with engine.begin() as connection:
        await connection.run_sync(roundtrip)
        assert await connection.scalar(text("SELECT count(*) FROM bot_guards")) == 2


async def test_real_handler_replays_commit_after_crash_once(pg_db, registry, profiles, monkeypatch):
    from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation

    _, sessions = pg_db
    catalog = Catalog(sessions, registry)
    control = PersistentSearchControl(sessions, registry)
    bot = Bot("42:offline", session=OfflineSession())
    storage = MemoryStorage()
    dp = make_dispatcher(
        storage,
        SimpleEventIsolation(),
        BotUI(catalog, control, AdminService(sessions, profiles), registry),
    )
    await register(catalog, 101, "alice", 1)
    state = dp.fsm.get_context(bot=bot, chat_id=101, user_id=101)
    draft = new_draft({"models": ["Nike"], "sources": {"vinted": ["FR"]}})
    draft["step"] = 9
    await state.set_data({"screen": "abcdefgh", "draft": draft})
    update = Update.model_validate(
        {
            "update_id": 100,
            "callback_query": {
                "id": "100",
                "chat_instance": "offline",
                "data": "abcdefgh:save",
                "from": {"id": 101, "is_bot": False, "first_name": "Alice", "username": "alice"},
                "message": {
                    "message_id": 10,
                    "date": int(utcnow().timestamp()),
                    "chat": {"id": 101, "type": "private"},
                    "text": "Review",
                },
            },
        }
    )
    save = catalog.save
    calls = 0

    async def crash_after_commit(*args, **kwargs):
        nonlocal calls
        result = await save(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise ConnectionError("Process lost after commit")
        return result

    monkeypatch.setattr(catalog, "save", crash_after_commit)
    inbox = UpdateInbox(sessions, bot.id)
    await inbox.receive(update)
    await inbox.once(dp, bot)
    assert (await catalog.list(101))[1] == 1
    async with sessions.begin() as session:
        row = await session.get(Inbox, (bot.id, 100))
        assert row.state == "received"
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
    await inbox.once(dp, bot)
    assert (await catalog.list(101))[1] == 1 and calls == 2
    async with sessions() as session:
        assert (await session.get(Inbox, (bot.id, 100))).state == "processed"
    await storage.close()
    await bot.session.close()
