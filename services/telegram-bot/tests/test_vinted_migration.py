import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from test_application import register, token_for

from vpars_bot.application import Catalog
from vpars_bot.db import SearchRun, SearchVersion, User
from vpars_bot.domain import Filters, Source, SourceRegistry
from vpars_bot.search_control import PersistentSearchControl


@pytest.mark.integration
async def test_split_migration_preserves_filters_preferences_and_unrelated_work(pg_db, registry):
    engine, sessions = pg_db

    def migrate(connection, direction, revision):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        getattr(command, direction)(config, revision)

    other = Source(label="Other source", regions=["FR"])
    legacy = SourceRegistry(
        {
            "vinted": Source(
                label="Vinted", regions=["GB", "FR", "IT", "DE"], aliases={"UK": "GB"}
            ),
            "other": other,
        }
    )
    old_catalog = Catalog(sessions, legacy)
    old_control = PersistentSearchControl(sessions, legacy)
    for uid, username in ((101, "alice"), (202, "bob"), (303, "carol")):
        await register(old_catalog, uid, username, uid)
    filters = Filters(
        models=["Nike"], sizes=["EU 42"], sources={"vinted": ["UK", "FR"], "other": ["FR"]}
    )
    item = await old_catalog.save(101, filters, "save")
    item = await old_catalog.save(
        101, filters.model_copy(update={"brands": ["Nike"]}), "edit", search_id=item.id, version=1
    )
    archived = await old_catalog.save(202, Filters(sources={"vinted": ["IT"]}), "archived")
    await old_catalog.archive(202, archived.id, True, 1, "archive")
    await old_control.start(101, "before-migration")
    old_token = await token_for(sessions, source_id="vinted")
    other_token = await token_for(sessions, source_id="other")
    assert await old_control.acknowledge(old_token)
    assert await old_control.acknowledge(other_token)
    async with sessions.begin() as session:
        (await session.get(User, 101)).enabled_sources = ["vinted", "other"]
        (await session.get(User, 202)).enabled_sources = []

    async with engine.begin() as connection:
        # Seed legacy source IDs with the current ORM, then remove newer schema
        # columns before exercising the migration. Never query an old schema
        # with an ORM that now requires last_error.
        await connection.run_sync(migrate, "downgrade", "0003")
        await connection.run_sync(migrate, "upgrade", "head")

    registry = SourceRegistry({**registry.sources, "other": other})
    # Downgrading to 0003 deliberately drops subscriptions (added in 0006).
    # Restore this fixture's paid account before checking unrelated live work.
    from datetime import timedelta

    from vpars_bot.db import Subscription, utcnow

    async with sessions.begin() as session:
        session.add(
            Subscription(
                user_id=101, level="ULTRA", expires_at=utcnow() + timedelta(days=30), granted_by=101
            )
        )
    catalog = Catalog(sessions, registry)
    control = PersistentSearchControl(sessions, registry)
    migrated = await catalog.get(101, item.id)
    assert migrated.version == 2 and not migrated.archived
    assert migrated.filters == {
        **item.filters,
        "sources": {"vinted_gb": ["GB"], "vinted_fr": ["FR"], "other": ["FR"]},
    }
    assert (await catalog.get(202, archived.id)).archived
    assert (await catalog.get(202, archived.id)).filters["sources"] == {"vinted_it": ["IT"]}
    assert (await catalog.user(202)).enabled_sources == []
    assert (await catalog.user(303)).enabled_sources is None
    assert set((await catalog.user(101)).enabled_sources) == set(registry.sources)
    assert not await control.can_deliver(old_token)
    assert not await control.acknowledge(old_token)
    assert await control.can_deliver(other_token)
    async with sessions() as session:
        assert await session.get(SearchRun, (item.id, "vinted")) is None
        for key in ("vinted_gb", "vinted_fr"):
            run = await session.get(SearchRun, (item.id, key))
            assert run.state == "stopped" and run.generation > old_token.generation
        history = list(
            (
                await session.scalars(
                    select(SearchVersion)
                    .where(SearchVersion.search_id == item.id)
                    .order_by(SearchVersion.version)
                )
            ).all()
        )
    assert [row.version for row in history] == [1, 2]
    assert all(row.filters["sources"] == migrated.filters["sources"] for row in history)
    assert history[0].filters["brands"] == [] and history[1].filters["brands"] == ["Nike"]
    await control.start(101, "after-migration")
    gb, fr = (
        await token_for(sessions, source_id="vinted_gb"),
        await token_for(sessions, source_id="vinted_fr"),
    )
    assert await control.acknowledge(gb) and await control.acknowledge(fr)
    await catalog.toggle_marketplace(101, "vinted_fr", "only-fr-off")
    assert not await control.can_deliver(fr)
    assert await control.can_deliver(gb) and await control.can_deliver(other_token)
