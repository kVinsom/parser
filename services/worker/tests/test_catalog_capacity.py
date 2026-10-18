from datetime import timedelta

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor, Store
from vpars_worker.parser import ParseFailure, ParserClient
from vpars_worker.query import source_text


def test_model_remains_local_when_brand_already_anchors_the_catalog():
    assert source_text({"brands": ["Stone Island"], "models": ["Off-Dye"]}) == "Stone Island"
    assert source_text({"models": ["Off-Dye"]}) == "Off-Dye"
    assert source_text({"brands": ["Nike", "Adidas"], "models": ["Hoodie"]}) == "Hoodie"


async def test_brand_and_model_share_requests_but_keep_their_own_matches(environment):
    store, catalog = environment
    searches = []
    for key, filters in (
        ("brand", {"keywords": ["Stone Island", "Good"], "keywords_all": True}),
        ("model", {"brands": ["Stone Island"], "models": ["Off-Dye"], "sizes": ["L"]}),
    ):
        searches.append(
            await catalog.save(
                101,
                Filters(**filters, sources={"vinted_fr": ["FR"]}),
                key,
            )
        )
    await store.control.start(101, "start-both")
    parser = Parser(items=[lot("999")])
    monitor = Monitor(store, parser)
    await monitor.once()
    assert len(parser.calls) == 1
    assert not await rows(store, Delivery)
    await due(store, Job)
    condition = Attribute(raw="Good", normalized="good")
    parser.items = [
        lot("10001", condition=condition),
        lot("10002", title="Stone Island Off-Dye", condition=condition),
    ]
    await monitor.once()
    assert len(parser.calls) == 2
    assert parser.calls[-1].search_text == "Stone Island"
    assert not parser.calls[-1].filters.models  # Verified separately for each search.
    deliveries = {d.item["source_item_id"]: d for d in await rows(store, Delivery)}
    assert {c["token"]["search_id"] for c in deliveries["10001"].candidates} == {searches[0].id}
    assert {c["token"]["search_id"] for c in deliveries["10002"].candidates} == {
        s.id for s in searches
    }


@pytest.mark.parametrize("code", ["UnexpectedContent", "GatewayTimeout", "GatewayServiceError"])
async def test_persistently_unreadable_baseline_releases_capacity_and_recovers(environment, code):
    store, catalog = environment
    await catalog.save(101, Filters(keywords=["missing"], sources={"vinted_fr": ["FR"]}), "save")
    await store.control.start(101, "start")
    parser = Parser()
    parser.error = code
    monitor = Monitor(store, parser)
    for _ in range(8):
        await due(store, Job)
        await monitor.once()
    saved = (await rows(store, Job))[0]
    assert 295 < (saved.next_due - utcnow()).total_seconds() <= 300
    assert not saved.baseline_ready and not saved.checkpoint
    restarted = Store(store.sessions, store.registry)
    assert not await restarted.claim("FR")
    await due(store, Job)
    parser.error = None
    await Monitor(restarted, parser).once()
    saved = (await rows(store, Job))[0]
    assert saved.failures == 0 and saved.last_error is None and saved.baseline_ready
    assert not await rows(store, Delivery)


async def test_new_head_lots_are_prepared_before_newly_discovered_deep_inventory(environment):
    store, _ = environment
    await start(environment)
    monitor = Monitor(store, Parser(items=[lot("10001")]))
    await monitor.once()  # Head listing.
    await due(store, Job)
    async with store.sessions.begin() as session:
        job = await session.scalar(select(Job))
        job.page = 2
    monitor.parser.items = [lot("10002")]
    await monitor.once()  # Old inventory may be discovered later on a deep page.
    notifier = Notifier(store, Bot())
    head = await notifier.claim("prepare", fresh_first=True)
    assert head.item["source_item_id"] == "10001"
    assert head.media_progress["catalog_page"] == 1
    deep = await notifier.claim("prepare", fresh_first=False)
    assert deep.item["source_item_id"] == "10002"
    assert deep.media_progress["catalog_page"] == 2


async def test_source_retry_after_remains_authoritative(environment):
    store, _ = environment
    await start(environment)
    job = (await store.claim())[0]
    await store.fail(job, "SourceRateLimited", retry_after=900)
    saved = (await rows(store, Job))[0]
    assert saved.next_due >= utcnow() + timedelta(seconds=895)


@pytest.mark.parametrize("brand", ["Amiri", "Stone Island"])
@pytest.mark.parametrize("region", ["FR", "GB"])
async def test_shared_pause_does_not_multiply_each_users_existing_failures(
    environment, brand, region
):
    store, catalog = environment
    await catalog.register(202, 202, "second", "Second", 1, start=True)
    for user in (101, 202):
        await catalog.save(
            user, Filters(keywords=[brand], sources={"vinted_" + region.lower(): [region]}), "save"
        )
        await store.control.start(user, "start")
    await store.ingest()
    async with store.sessions.begin() as session:
        for job in await session.scalars(select(Job)):
            job.failures = 14
    before = {job.id: job for job in await rows(store, Job)}
    parser = ParserClient()
    try:
        parser.remember_failure(region, ParseFailure("SourceRateLimited", retry_after=45))
        await Monitor(store, parser).once(region)
    finally:
        await parser.close()
    jobs = await rows(store, Job)
    assert len(jobs) == 2
    for job in jobs:
        assert 43 <= (job.next_due - utcnow()).total_seconds() <= 46
        assert job.failures == 14
        assert job.page == before[job.id].page
        assert job.checkpoint == before[job.id].checkpoint
        assert job.coverage == before[job.id].coverage
        assert job.state == "waiting" and job.lease_id is None
