from datetime import timedelta

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rows
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor, Store


@pytest.mark.parametrize("user,brand", [(101, "Stone Island"), (202, "Amiri"), (303, "Nike")])
@pytest.mark.parametrize("lane", ["prepare", "ready"])
async def test_recent_batch_keeps_catalog_order_across_restarts(environment, user, brand, lane):
    store, catalog = environment
    if user != 101:
        await catalog.register(user, user, f"user{user}", "Test", 1, start=True)
    await catalog.save(user, Filters(brands=[brand], sources={"vinted_fr": ["FR"]}), "save")
    await store.control.start(user, "start")
    parser = Parser(items=[lot("baseline")])
    monitor = Monitor(store, parser)
    await monitor.once()
    await due(store, Job)
    # IDs deliberately do not encode source order. All dates are equally fresh;
    # discovery priority must not be confused with publication-age proof.
    stamp = utcnow()
    parser.items = [
        lot(
            identity,
            title=f"{brand} Jeans",
            brand=Attribute(raw=brand, normalized=brand.lower()),
            published_at=stamp,
        )
        for identity in ("400", "900", "200")
    ]
    await monitor.once()
    original = {d.id: d.item["first_seen_at"] for d in await rows(store, Delivery)}
    if lane == "ready":
        async with store.sessions.begin() as session:
            for row in await session.scalars(select(Delivery)):
                row.media_progress = {**row.media_progress, "dispatch_ready": True}
    notifier = Notifier(Store(store.sessions, store.registry), Bot())
    claimed = [await notifier.claim(lane, fresh_first=True) for _ in range(3)]
    assert [d.item["source_item_id"] for d in claimed] == ["400", "900", "200"]
    assert {d.id: d.item["first_seen_at"] for d in claimed} == original


async def test_new_batch_precedes_older_batch_but_fifo_remains_available(environment):
    store, catalog = environment
    await catalog.save(101, Filters(keywords=["Jeans"], sources={"vinted_fr": ["FR"]}), "save")
    await store.control.start(101, "start")
    parser = Parser(items=[lot("baseline")])
    monitor = Monitor(store, parser)
    await monitor.once()
    for identities in (("old-first", "old-last"), ("new-first", "new-last")):
        await due(store, Job)
        parser.items = [lot(i, title="Jeans") for i in identities]
        await monitor.once()
    notifier = Notifier(store, Bot())
    assert (await notifier.claim("prepare", fresh_first=True)).item["source_item_id"] == "new-first"
    assert (await notifier.claim("prepare", fresh_first=False)).item[
        "source_item_id"
    ] == "old-first"
    assert (await notifier.claim("prepare", fresh_first=True)).item["source_item_id"] == "new-last"


async def test_learned_head_timeout_survives_success_and_restart(environment):
    from test_monitoring import start

    from vpars_worker.scheduling import head_timeout

    store, _ = environment
    await start(environment)
    parser = Parser()
    parser.error = "GatewayTimeout"
    await Monitor(store, parser).once()
    assert parser.calls[-1].timeout_seconds == 20
    await due(store, Job)
    parser.error = None
    await Monitor(Store(store.sessions, store.registry), parser).once()
    assert parser.calls[-1].timeout_seconds == 24
    job = (await rows(store, Job))[0]
    assert job.scheduling["head_timeouts"] == 0
    assert head_timeout([job]) == 24
    assert head_timeout([job], configured=8) == 8
    # A subsequent timeout increases the learned budget rather than oscillating
    # between a slow success and the original too-short deadline.
    await due(store, Job)
    parser.error = "GatewayTimeout"
    await Monitor(store, parser).once()
    assert parser.calls[-1].timeout_seconds == 24
    job = (await rows(store, Job))[0]
    assert head_timeout([job]) == 24
    job.scheduling = {
        **job.scheduling,
        "head_timeouts": 0,
        "head_budget_until": (utcnow() - timedelta(seconds=1)).isoformat(),
    }
    assert head_timeout([job]) == 20
