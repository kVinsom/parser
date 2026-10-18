from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from vpars_bot.application import AdminService, Catalog
from vpars_bot.db import Search, SearchRun, Subscription, User, WalletOrder, utcnow
from vpars_bot.domain import Filters, UserError
from vpars_bot.entitlements import PLANS, autobuy_access
from vpars_bot.search_control import WorkToken
from vpars_bot.subscriptions import SubscriptionService


async def provision(catalog, profiles, level="LITE", days=30):
    await catalog.register(101, 101, "alice", "Alice", 1, start=True)
    await catalog.register(202, 202, "support", "Support", 2, start=True)
    profiles.names.update({101: "alice", 202: "support"})
    async with catalog.sessions.begin() as session:
        (await session.get(User, 202)).is_admin = True
        (await session.get(User, 101)).enabled_sources = ["vinted_fr"]
    subs = SubscriptionService(catalog.sessions, AdminService(catalog.sessions, profiles))
    await subs.grant(202, 101, "alice", level, days, "initial")
    return subs


@pytest.mark.parametrize("level", ["LITE", "PRO", "ULTRA"])
async def test_real_filter_limit_archive_restore_and_exclusions(services, profiles, level):
    catalog, _, _ = services
    await provision(catalog, profiles, level)
    saved = []
    for n in range(PLANS[level].filters):
        saved.append(await catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), f"save:{n}"))
    await catalog.toggle_filter(101, saved[0].id, "disable")
    with pytest.raises(UserError, match="можна зберегти"):
        await catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), "overflow")
    await catalog.archive(101, saved[0].id, True, saved[0].version, "archive")
    await catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), "replacement")
    with pytest.raises(UserError, match="можна зберегти"):
        await catalog.archive(101, saved[0].id, False, saved[0].version + 1, "restore")
    filters = Filters(sources={"vinted_fr": ["FR"]}, excluded_keywords=["fake"])
    if level == "LITE":
        with pytest.raises(UserError, match="Виключення"):
            await catalog.save(101, filters, "edit", search_id=saved[1].id, version=1)
    else:
        await catalog.save(101, filters, "edit", search_id=saved[1].id, version=1)


@pytest.mark.parametrize("level,regions", [("LITE", 1), ("PRO", 3), ("ULTRA", 4)])
async def test_region_limit_is_shared_between_searches(services, profiles, level, regions):
    catalog, _, _ = services
    await provision(catalog, profiles, level)
    all_sources = ["vinted_fr", "vinted_gb", "vinted_de", "vinted_it"]
    for source in all_sources[1:regions]:
        assert await catalog.toggle_marketplace(101, source, source)
    assert len(await catalog.marketplaces(101)) == regions
    if regions < 4:
        with pytest.raises(UserError, match="регіонів"):
            await catalog.toggle_marketplace(101, all_sources[regions], "too-many")
        # Legacy explicit source fields cannot bypass the global selection.
        source = all_sources[regions]
        with pytest.raises(UserError, match="регіон"):
            await catalog.save(
                101, Filters(sources={source: catalog.registry.sources[source].regions}), "forged"
            )


async def test_no_subscription_cannot_save_start_or_use_autobuy(services):
    catalog, control, _ = services
    await catalog.register(101, 101, "alice", "Alice", 1, start=True)
    with pytest.raises(UserError, match="активна підписка"):
        await catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), "save")
    with pytest.raises(UserError, match="активна підписка"):
        await control.start(101, "start")
    async with catalog.sessions() as session:
        assert (await autobuy_access(session, await session.get(User, 101)))[
            0
        ] == "SubscriptionRequired"


async def test_expiry_blocks_stale_token_and_renewal_requires_start(services, profiles):
    catalog, control, _ = services
    subs = await provision(catalog, profiles)
    item = await catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), "save")
    await control.start(101, "start")
    token = WorkToken(101, item.id, "vinted_fr", 0, 1, 1)
    assert await control.acknowledge(token)
    assert await control.can_deliver(token)
    async with catalog.sessions.begin() as session:
        (await session.get(Subscription, 101)).expires_at = utcnow() - timedelta(seconds=1)
    assert not await control.can_deliver(token)
    await subs.grant(202, 101, "alice", "LITE", 5, "renew")
    assert not await control.can_deliver(token)
    assert (await control.status(101))[item.id] == "paused_limit"
    await control.start(101, "new-start")
    async with catalog.sessions() as session:
        run = await session.get(SearchRun, (item.id, "vinted_fr"))
        assert run.state == "requested" and run.generation > token.generation


async def test_downgrade_preserves_filters_and_pauses_only_disallowed_runs(services, profiles):
    catalog, control, _ = services
    subs = await provision(catalog, profiles, "PRO")
    for source in ("vinted_gb", "vinted_it"):
        await catalog.toggle_marketplace(101, source, source)
    items = [
        await catalog.save(
            101, Filters(sources={"vinted_fr": ["FR"]}, follow_enabled_sources=True), f"save:{n}"
        )
        for n in range(6)
    ]
    await control.start(101, "start")
    await subs.grant(202, 101, "alice", "LITE", 1, "downgrade")
    async with catalog.sessions() as session:
        rows = list(await session.scalars(select(SearchRun)))
        assert len([r for r in rows if r.state == "requested"]) == 3
        assert len([r for r in rows if r.state == "paused_limit"]) == 15
        for item in items:
            assert await session.get(Search, item.id)
    assert await catalog.count(101) == 6


@pytest.mark.parametrize("days", [5, 30, 90])
@pytest.mark.parametrize("level,limit", [("LITE", 1), ("PRO", 5)])
async def test_monthly_autobuy_quota_short_term_replay_extension_and_reset(
    services, profiles, monkeypatch, days, level, limit
):
    catalog, _, _ = services
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    monkeypatch.setattr("vpars_bot.entitlements.utcnow", lambda: now)
    monkeypatch.setattr("vpars_bot.subscriptions.utcnow", lambda: now)
    subs = await provision(catalog, profiles, level, days)
    async with catalog.sessions.begin() as session:
        user = await session.get(User, 101)
        error, period = await autobuy_access(session, user)
        assert error is None and period == now
        for n in range(limit):
            session.add(
                WalletOrder(
                    id=f"order-{n}",
                    user_id=101,
                    item_id=str(n),
                    url="https://www.vinted.fr/items/1",
                    listed_price=100,
                    currency="EUR",
                    state="unknown" if n == 0 else "succeeded",
                    quota_period_start=period,
                )
            )
    await subs.grant(202, 101, "alice", level, 30, "extension")
    async with catalog.sessions() as session:
        assert (await autobuy_access(session, await session.get(User, 101)))[
            0
        ] == "SubscriptionAutobuyLimit"
    now += timedelta(days=30)
    async with catalog.sessions() as session:
        error, period = await autobuy_access(session, await session.get(User, 101))
        assert error is None and period == now


@pytest.mark.integration
async def test_concurrent_saves_do_not_exceed_paid_limit(pg_db, registry, profiles):
    import asyncio

    _, sessions = pg_db
    catalog = Catalog(sessions, registry)
    await provision(catalog, profiles)
    results = await asyncio.gather(
        *[catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), f"save:{n}") for n in range(7)],
        return_exceptions=True,
    )
    assert sum(isinstance(r, UserError) for r in results) == 4
    assert await catalog.count(101) == 3


@pytest.mark.integration
async def test_quota_migration_preserves_existing_term_and_backfills_anchor(
    pg_db, registry, profiles
):
    from alembic import command
    from alembic.config import Config

    engine, sessions = pg_db
    catalog = Catalog(sessions, registry)
    await provision(catalog, profiles, "PRO", 45)
    async with sessions() as session:
        before = await session.get(Subscription, 101)
        expiry, updated = before.expires_at, before.updated_at

    def migrate(connection, direction, revision):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        getattr(command, direction)(config, revision)

    async with engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0009")
        await connection.run_sync(migrate, "upgrade", "head")
    async with sessions() as session:
        after = await session.get(Subscription, 101)
        assert after.level == "PRO" and after.expires_at == expiry
        assert after.period_started_at == updated
