import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from test_application import register

from vpars_bot.application import Catalog
from vpars_bot.db import ControlCommand
from vpars_bot.domain import Filters, UserError
from vpars_bot.search_control import PersistentSearchControl, WorkToken


async def tokens(sessions):
    async with sessions() as session:
        starts = await session.scalars(
            select(ControlCommand)
            .where(ControlCommand.kind == "start")
            .order_by(ControlCommand.created_at)
        )
        return [WorkToken(**item.payload["token"]) for item in starts]


async def test_selection_persists_and_stops_only_disabled_filter(services, db, registry):
    catalog, control, _ = services
    await register(catalog, 101, "alice", 1)
    filters = Filters(keywords=["Nike"], sources={"vinted_fr": ["FR"], "vinted_de": ["DE"]})
    one = await catalog.save(101, filters, "one")
    two = await catalog.save(101, filters, "two")
    assert one.enabled and two.enabled
    assert await control.start(101, "start") == 2
    original_tokens = await tokens(db[1])
    for token in original_tokens:
        assert await control.acknowledge(token)

    assert not await catalog.toggle_filter(101, one.id, "disable")
    assert not await catalog.toggle_filter(101, one.id, "disable")
    saved = await Catalog(db[1], registry).get(101, one.id)
    assert not saved.enabled and not saved.archived
    assert saved.filters == one.filters and saved.version == one.version
    assert await catalog.count(101) == 2
    assert await catalog.count(101, enabled_only=True) == 1
    for token in original_tokens:
        expected = token.search_id == two.id
        assert await control.can_deliver(token) == expected
        assert await control.acknowledge(token) == expected
    async with db[1]() as session:
        invalidations = list(
            await session.scalars(select(ControlCommand).where(ControlCommand.kind == "invalidate"))
        )
    assert len(invalidations) == 1

    await control.stop(101, "stop")
    assert await control.start(101, "restart") == 1
    restarted_tokens = (await tokens(db[1]))[len(original_tokens) :]
    assert {token.search_id for token in restarted_tokens} == {two.id}
    with pytest.raises(UserError, match="Увімкни хоча б один фільтр"):
        await control.start(101, "direct-start-disabled", one.id)

    assert await catalog.toggle_filter(101, one.id, "enable")
    assert (await control.status(101))[one.id] == "stopped"
    assert await control.start(101, "start-enabled") == 2
    latest = (await tokens(db[1]))[-2:]
    assert {token.search_id for token in latest} == {one.id}
    for token in latest:
        assert await control.acknowledge(token)
        assert await control.can_deliver(token)
    for token in original_tokens:
        assert not await control.can_deliver(token)


async def test_no_enabled_filters_does_not_create_work_and_skips_disabled_capabilities(
    services, db
):
    catalog, control, _ = services
    await register(catalog, 101, "alice", 1)
    bad = await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "legacy")
    async with db[1].begin() as session:
        from vpars_bot.db import Search

        saved = await session.get(Search, bad.id)
        saved.filters = {**saved.filters, "colors": ["Black"]}
    good = await catalog.save(101, Filters(keywords=["Nike"], sources={"vinted": ["FR"]}), "good")
    await catalog.toggle_filter(101, bad.id, "disable-bad")
    assert await control.start(101, "start-good") == 1
    await catalog.toggle_filter(101, good.id, "disable-good")
    with pytest.raises(UserError, match="Включені фільтри"):
        await control.start(101, "no-filters")
    assert len(await tokens(db[1])) == 1
    assert all(state == "stopped" for state in (await control.status(101)).values())
    assert await catalog.count(101) == 2


async def test_selection_ownership_archive_and_edit(services):
    catalog, _, _ = services
    await register(catalog, 101, "alice", 1)
    await register(catalog, 202, "bob", 2)
    filters = Filters(keywords=["Nike"], sources={"vinted": ["FR"]})
    item = await catalog.save(101, filters, "save")
    with pytest.raises(UserError, match="не знайдено"):
        await catalog.toggle_filter(202, item.id, "foreign")
    await catalog.toggle_filter(101, item.id, "disable")
    updated = await catalog.save(101, filters, "edit", search_id=item.id, version=item.version)
    assert not updated.enabled
    await catalog.archive(101, item.id, True, updated.version, "archive")
    with pytest.raises(UserError, match="прибрано"):
        await catalog.toggle_filter(101, item.id, "archived")
    archived = await catalog.get(101, item.id)
    await catalog.archive(101, item.id, False, archived.version, "restore")
    assert not (await catalog.get(101, item.id)).enabled


async def test_selection_changed_during_start_cannot_launch_disabled_filter(services, db, registry):
    catalog = services[0]
    await register(catalog, 101, "alice", 1)
    item = await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "save")

    class ChangingCapabilities:
        async def issues(self, filters):
            await catalog.toggle_filter(101, item.id, "disable-during-start")
            return []

    control = PersistentSearchControl(db[1], registry, ChangingCapabilities())
    with pytest.raises(UserError, match="Список змінився"):
        await control.start(101, "delayed")
    assert not await tokens(db[1])


@pytest.mark.integration
async def test_concurrent_toggle_retries_change_selection_once(pg_db, registry):
    catalog = Catalog(pg_db[1], registry)
    await register(catalog, 101, "alice", 1)
    item = await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "save")
    results = await asyncio.gather(
        *(catalog.toggle_filter(101, item.id, "same-update") for _ in range(6))
    )
    assert results == [False] * 6
    assert not (await catalog.get(101, item.id)).enabled


@pytest.mark.integration
async def test_migration_preserves_existing_filters_and_running_work(pg_db, registry):
    engine, sessions = pg_db
    catalog = Catalog(sessions, registry)
    control = PersistentSearchControl(sessions, registry)
    await register(catalog, 101, "alice", 1)
    item = await catalog.save(101, Filters(keywords=["Nike"], sources={"vinted": ["FR"]}), "save")
    await control.start(101, "start")
    token = (await tokens(sessions))[0]
    assert await control.acknowledge(token)

    def migrate(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0008")
        command.upgrade(config, "head")

    async with engine.begin() as connection:
        await connection.run_sync(migrate)
        assert await connection.scalar(text("SELECT enabled FROM bot_searches")) is True
    saved = await catalog.get(101, item.id)
    assert saved.enabled and saved.filters == item.filters and saved.version == item.version
    assert await control.can_deliver(token)
