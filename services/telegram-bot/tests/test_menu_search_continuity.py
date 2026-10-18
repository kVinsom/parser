"""Exercise navigation through the real durable inbox, PostgreSQL and Redis FSM."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Update
from conftest import OfflineSession
from redis.asyncio import Redis
from sqlalchemy import select

from vpars_bot.application import AdminService, Catalog
from vpars_bot.db import ControlCommand, Inbox, Search, SearchRun, User, utcnow
from vpars_bot.domain import Filters
from vpars_bot.profile_menu import CONNECT_AUTOBUY, FAQ, FAQ_BACK, PAGES, PROFILE
from vpars_bot.reply_menu import (
    ADD,
    BACK,
    BUY_SUBSCRIPTION,
    FILTER_SETTINGS,
    HOME,
    MARKETS,
    REMOVE,
    make_menu_dispatcher,
)
from vpars_bot.runtime import UpdateInbox
from vpars_bot.search_control import PersistentSearchControl, WorkToken
from vpars_bot.transport import TelegramDeferred
from vpars_bot.update_context import SEARCH_START, SEARCH_STOP

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("lease_expired", [False, True])
async def test_menu_back_cancel_replay_and_fsm_loss_preserve_running_search(
    pg_db,
    registry,
    profiles,
    monkeypatch,
    lease_expired,
):
    _, sessions = pg_db
    prefix = "test-navigation-" + uuid4().hex
    redis = Redis.from_url(os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:16379/15"))
    storage = RedisStorage(redis, key_builder=DefaultKeyBuilder(prefix=prefix, with_bot_id=True))
    catalog = Catalog(sessions, registry)
    control = PersistentSearchControl(sessions, registry)
    transport = OfflineSession()
    bot = Bot("42:offline", session=transport)
    dp = make_menu_dispatcher(
        storage,
        storage.create_isolation(),
        catalog,
        control,
        admins=AdminService(sessions, profiles),
    )
    inbox = UpdateInbox(sessions, bot.id, control=control, menu_mode="menu")
    update_id = 0
    fail_next = False
    original = transport.make_request

    async def send_response(bot, method, timeout=None):
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise TelegramDeferred(2)
        return await original(bot, method, timeout)

    monkeypatch.setattr(transport, "make_request", send_response)

    async def feed(value, *, fail=False):
        nonlocal update_id, fail_next
        update_id += 1
        fail_next = fail
        message = {
            "message_id": update_id,
            "date": int(utcnow().timestamp()),
            "chat": {"id": 101, "type": "private"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alice", "username": "alice"},
            "text": value,
        }
        if value.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(value)}]
        update = Update.model_validate({"update_id": update_id, "message": message})
        await inbox.receive(update)
        assert await inbox.once(dp, bot)
        if fail:
            async with sessions.begin() as session:
                row = await session.get(Inbox, (42, update_id))
                assert row.state == "received" and row.last_error == "TelegramDeferred"
                row.next_attempt_at = utcnow() - timedelta(seconds=1)
            assert await inbox.once(dp, bot)
        async with sessions() as session:
            assert (await session.get(Inbox, (42, update_id))).state == "processed"
        await inbox.receive(update)  # Telegram redelivery must not mutate the search either.
        assert not await inbox.once(dp, bot)

    async def snapshot():
        async with sessions() as session:
            user = await session.get(User, 101)
            runs = list(
                await session.scalars(
                    select(SearchRun).order_by(
                        SearchRun.search_id,
                        SearchRun.source_id,
                    )
                )
            )
            searches = list(await session.scalars(select(Search).order_by(Search.id)))
            commands = list(
                await session.scalars(select(ControlCommand).order_by(ControlCommand.id))
            )
            return (
                user.delivery_epoch,
                [(r.search_id, r.source_id, r.generation, r.state, r.active_until) for r in runs],
                [(s.id, s.version, s.archived, s.filters) for s in searches],
                [(c.id, c.kind, c.payload) for c in commands],
            )

    def last_keyboard():
        return next(m for m in reversed(transport.calls) if getattr(m, "reply_markup", None))

    try:
        await feed("/start")
        for keyword in ("Nike", "Adidas"):
            await catalog.save(
                101,
                Filters(
                    keywords=[keyword],
                    sources={"vinted_fr": ["FR"]},
                    follow_enabled_sources=True,
                ),
                "save-" + keyword,
            )
        await feed(SEARCH_START)
        async with sessions() as session:
            tokens = [
                WorkToken(**c.payload["token"])
                for c in await session.scalars(
                    select(ControlCommand).where(ControlCommand.kind == "start")
                )
            ]
        assert len(tokens) == 8
        for token in tokens:
            assert await control.acknowledge(token, lease_seconds=300)
        if lease_expired:
            async with sessions.begin() as session:
                for run in await session.scalars(select(SearchRun)):
                    run.active_until = utcnow() - timedelta(seconds=1)
        initial = await snapshot()

        # Reading help, opening settings, cancelling edits and backing out of checkout
        # must not enqueue stop/invalidate/start or invalidate a delivery token.
        routes = [
            [FAQ, *[p[0] for p in PAGES.values()], CONNECT_AUTOBUY, FAQ_BACK],
            [FILTER_SETTINGS, BACK],
            [FILTER_SETTINGS, MARKETS, BACK, BACK],
            [FILTER_SETTINGS, ADD, "Gucci, L", "Скасувати", BACK],
            [FILTER_SETTINGS, REMOVE, BACK, BACK],
            [BUY_SUBSCRIPTION, "PRO · 3 місяці", BACK, BACK],
            [PROFILE, HOME],
            ["/start"],
        ]
        for route in routes:
            for value in route:
                await feed(value)
                assert await snapshot() == initial, value
            labels = [b.text for row in last_keyboard().reply_markup.keyboard for b in row]
            assert SEARCH_STOP in labels and SEARCH_START not in labels

        await feed(REMOVE)
        choice = last_keyboard().reply_markup.keyboard[0][0].text
        for value in (choice, BACK, BACK, BACK):
            await feed(value)
            assert await snapshot() == initial

        await feed(FAQ)
        await feed(FAQ_BACK, fail=True)
        assert await snapshot() == initial

        # Redis dialogue expiration/restart is separate from the persistent search.
        for stale_button in (CONNECT_AUTOBUY, FAQ_BACK, "✅ Vinted FR", "/menu"):
            await dp.fsm.get_context(bot=bot, chat_id=101, user_id=101).clear()
            responses = len(transport.calls)
            await feed(stale_button)
            assert len(transport.calls) > responses
            assert await snapshot() == initial
            assert last_keyboard().reply_markup is not None
        await dp.fsm.get_context(bot=bot, chat_id=101, user_id=101).clear()
        await feed("/start")
        assert await snapshot() == initial
        labels = [b.text for row in last_keyboard().reply_markup.keyboard for b in row]
        assert SEARCH_STOP in labels and SEARCH_START not in labels
        assert set((await control.status(101)).values()) == (
            {"requested"} if lease_expired else {"active"}
        )
        for token in tokens:
            assert await control.can_deliver(token) is (not lease_expired)
            if lease_expired:
                assert await control.acknowledge(token)  # Worker renews without another Start.
                assert await control.can_deliver(token)
    finally:
        keys = [key async for key in redis.scan_iter(match=prefix + ":*")]
        if keys:
            await redis.delete(*keys)
        await dp.fsm.close()
        await bot.session.close()
