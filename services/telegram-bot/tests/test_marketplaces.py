import asyncio

import pytest
from sqlalchemy import func, select
from test_application import register, token_for

from vpars_bot.application import Catalog
from vpars_bot.db import ControlCommand
from vpars_bot.domain import Filters, SourceRegistry, UserError
from vpars_bot.search_control import PersistentSearchControl


async def test_disabled_sources_are_excluded_from_new_work_and_old_tokens(db, registry):
    registry = SourceRegistry({key: registry.sources[key] for key in ("vinted_fr", "vinted_de")})
    catalog = Catalog(db[1], registry)
    control = PersistentSearchControl(db[1], registry)
    await register(catalog, 101, "alice", 1)
    await catalog.save(101, Filters(sources={"vinted_fr": ["FR"], "vinted_de": ["DE"]}), "save")
    await control.start(101, "start")
    first = await token_for(db[1], source_id="vinted_fr")
    other = await token_for(db[1], source_id="vinted_de")
    assert await control.acknowledge(first) and await control.acknowledge(other)
    assert not await catalog.toggle_marketplace(101, "vinted_fr", "off")
    assert not await control.can_deliver(first)
    assert not await control.acknowledge(first)
    assert await control.can_deliver(other)
    await control.stop(101, "stop")
    await control.start(101, "only-other")
    async with db[1]() as session:
        starts = list(
            (
                await session.scalars(
                    select(ControlCommand)
                    .where(ControlCommand.kind == "start")
                    .order_by(ControlCommand.created_at)
                )
            ).all()
        )
    assert len(starts) == 3
    assert starts[-1].payload["filters"]["sources"] == {"vinted_de": ["DE"]}
    assert starts[-1].payload["token"]["source_id"] == "vinted_de"
    assert await catalog.toggle_marketplace(101, "vinted_fr", "on")
    assert not await control.can_deliver(first)
    assert not await control.acknowledge(first)
    await control.start(101, "restart-vinted")
    fresh = await token_for(db[1])
    assert fresh.generation > first.generation
    assert await control.acknowledge(fresh)
    assert await control.can_deliver(fresh)
    await catalog.toggle_marketplace(101, "vinted_fr", "off-again")
    await catalog.toggle_marketplace(101, "vinted_de", "off-other")
    with pytest.raises(UserError, match="хоча б один"):
        await control.start(101, "none")


@pytest.mark.integration
async def test_concurrent_marketplace_retries_toggle_once(pg_db, registry):
    catalog = Catalog(pg_db[1], registry)
    control = PersistentSearchControl(pg_db[1], registry)
    await register(catalog, 101, "alice", 1)
    await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "save")
    await control.start(101, "start")
    token = await token_for(pg_db[1])
    assert await control.acknowledge(token)
    results = await asyncio.gather(
        *(catalog.toggle_marketplace(101, "vinted_fr", "same-update") for _ in range(6))
    )
    assert results == [False] * 6
    assert await catalog.marketplaces(101) == {"vinted_gb", "vinted_it", "vinted_de"}
    assert not await control.can_deliver(token)
    async with pg_db[1]() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ControlCommand)
            .where(ControlCommand.kind == "invalidate")
        )
    assert count == 1


@pytest.mark.integration
async def test_selection_changed_during_start_cannot_start_disabled_marketplace(pg_db, registry):
    catalog = Catalog(pg_db[1], registry)
    await register(catalog, 101, "alice", 1)
    await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "save")
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowCapabilities:
        async def issues(self, filters):
            entered.set()
            await release.wait()
            return []

    control = PersistentSearchControl(pg_db[1], registry, SlowCapabilities())
    start = asyncio.create_task(control.start(101, "delayed"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await catalog.toggle_marketplace(101, "vinted_fr", "disable")
        release.set()
        with pytest.raises(UserError, match="Вибір маркетплейсів змінився"):
            await start
    finally:
        release.set()
        if not start.done():
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)


@pytest.mark.integration
async def test_reply_draft_survives_recreated_dispatcher_with_redis(pg_db, registry):
    import os
    from uuid import uuid4

    from aiogram import Bot
    from aiogram.fsm.storage.base import DefaultKeyBuilder
    from aiogram.fsm.storage.redis import RedisStorage
    from conftest import ChatHarness, OfflineSession
    from redis.asyncio import Redis

    from vpars_bot.reply_menu import ADD, make_menu_dispatcher

    prefix = "test-reply-" + uuid4().hex
    url = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:16379/15")
    catalog = Catalog(pg_db[1], registry)
    bot = Bot("42:offline", session=OfflineSession())

    def setup():
        storage = RedisStorage(Redis.from_url(url), key_builder=DefaultKeyBuilder(prefix=prefix))
        dp = make_menu_dispatcher(
            storage,
            storage.create_isolation(),
            Catalog(pg_db[1], registry),
            PersistentSearchControl(pg_db[1], registry),
        )
        return dp, ChatHarness(dp, bot, bot.session)

    first, chat = setup()
    try:
        await chat.send("/start")
        await chat.send(ADD)
        assert (await (await chat.state()).get_data())["draft"]["kind"] == "keyword_filter"
        await chat.send("Nike, Air Max 90, EU 42")
        await chat.send("Пропустити")
        assert "Яка ціна в євро" in chat.panel().text
        last_update = chat.update_id
    finally:
        await first.fsm.close()
    second, chat = setup()
    try:
        chat.update_id = last_update
        assert (await (await chat.state()).get_data())["draft"]["step"] == "price"
        await chat.send("12,50 80")
        items, count, _ = await catalog.list(101)
        assert count == 1
        assert items[0].filters["keywords"] == ["Nike", "Air Max 90", "EU 42"]
        assert items[0].filters["keywords_all"]
        assert items[0].filters["price"] == {
            "currency": "EUR",
            "minimum": "12.50",
            "maximum": "80",
        }
        assert "EU 42" in chat.panel().text
        assert (await (await chat.state()).get_data())["draft"] is None
    finally:
        await (await chat.state()).clear()
        await second.fsm.close()
        await bot.session.close()
