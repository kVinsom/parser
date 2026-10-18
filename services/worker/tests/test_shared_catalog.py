"""Shared regional fetches with real DB transactions and offline source/Telegram stubs."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rows
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute

from vpars_worker.db import Delivery, Job, RegionalCatalog, RegionalIdentity, SeenItem, SentLot
from vpars_worker.shared_catalog import SharedMonitor, SharedStore


class Inventory(Parser):
    async def parse(self, request):
        result = await super().parse(request)
        return result.model_copy(update={
            "observed_item_ids": [item.source_item_id for item in self.items],
            "identities_complete": True, "empty_confirmed": not self.items and not self.more,
        })


def shared(environment):
    old, catalog = environment
    return SharedStore(old.sessions, old.registry), catalog


async def subscribe(store, catalog, user=101, brand="Amiri", region="FR", key=None, **extra):
    if user != 101:
        await catalog.register(user, user, "synthetic", "Synthetic", 1, start=True)
    key = key or str(uuid4())
    search = await catalog.save(user, Filters(brands=[brand],
        sources={"vinted_" + ("gb" if region == "UK" else region.lower()): [region]}, **extra), key)
    await store.control.start(user, "start-" + key)
    return search


def card(number, brand="Amiri", region="FR", **extra):
    return lot(number, title=brand + " Hoodie", brand=Attribute(raw=brand, normalized=brand.casefold()),
               region_id=region, source_namespace="vinted:" + region, **extra)


async def tick(store):
    await due(store, RegionalCatalog)
    await due(store, Job)


@pytest.mark.parametrize("region", ["FR", "IT", "DE", "UK"])
async def test_one_page_serves_different_brands_keywords_users_and_delivery(environment, region):
    store, catalog = shared(environment)
    await subscribe(store, catalog, 101, "Amiri", region, keywords=["hoodie"])
    await subscribe(store, catalog, 202, "Stone Island", region, keywords=["Stone"])
    parser = Inventory([card("999", region=region)])
    monitor = SharedMonitor(store, parser)
    await monitor.once(region)
    assert not await rows(store, Delivery)  # quiet baseline for both subscribers
    await tick(store)
    parser.items = [card("10001", region=region), card("10002", "Stone Island", region)]
    await monitor.once(region)
    assert len(parser.calls) == 2
    assert all(not r.search_text and not r.filters.brands and not r.filters.keywords for r in parser.calls)
    deliveries = await rows(store, Delivery)
    assert {(d.user_id, d.item["source_item_id"]) for d in deliveries} == {(101, "10001"), (202, "10002")}
    assert len(await rows(store, RegionalCatalog)) == 1
    bot = Bot()
    notifier = Notifier(store, bot)
    for _ in range(6):
        await notifier.once()
    assert {call[1] for call in bot.calls} == {101, 202}
    assert len(await rows(store, SentLot)) == 2
    await tick(store)
    await monitor.once(region)
    assert len(await rows(store, Delivery)) == 2


async def test_every_page_shared_and_cursor_survives_restart(environment):
    store, catalog = shared(environment)
    await subscribe(store, catalog)
    await subscribe(store, catalog, 202, "Stone Island")
    parser = Inventory([card("999")])
    parser.more = True
    await SharedMonitor(store, parser).once("FR")
    await tick(store)
    restarted = SharedStore(store.sessions, store.registry)
    await restarted.recover_executions()
    parser.items = [card("998")]
    await SharedMonitor(restarted, parser).once("FR")
    assert [r.page for r in parser.calls] == [1, 2]
    assert all(j.baseline_ready and j.checks == 2 for j in await rows(store, Job))
    assert len(await rows(store, RegionalIdentity)) == 2


async def test_no_subscribers_no_request_and_stop_during_fetch(environment):
    store, catalog = shared(environment)
    parser = Inventory([card("999")])
    monitor = SharedMonitor(store, parser)
    assert not await monitor.once("FR")
    await subscribe(store, catalog)
    await subscribe(store, catalog, 202, "Stone Island")
    await monitor.once("FR")
    await tick(store)
    parser.items = [card("10001"), card("10002", "Stone Island")]
    async def stop():
        await store.control.stop(101, "stop-during-fetch")
    parser.hook = stop
    await monitor.once("FR")
    assert {d.user_id for d in await rows(store, Delivery)} == {202}
    await store.control.stop(202, "stop-last")
    await tick(store)
    assert not await monitor.once("FR")
    assert len(parser.calls) == 2


async def test_new_subscriber_has_own_baseline_and_existing_history_survives(environment):
    store, catalog = shared(environment)
    await subscribe(store, catalog)
    parser = Inventory([card("999")])
    monitor = SharedMonitor(store, parser)
    await monitor.once("FR")
    await subscribe(store, catalog, 202)
    await tick(store)
    parser.items = [card("10001")]
    await monitor.once("FR")
    assert {d.user_id for d in await rows(store, Delivery)} == {101}
    before = {(s.job_id, s.item_id): (s.eligible, s.first_seen_at) for s in await rows(store, SeenItem)}
    await store.refresh_queries()
    assert before == {(s.job_id, s.item_id): (s.eligible, s.first_seen_at) for s in await rows(store, SeenItem)}
    await tick(store)
    parser.items = [card("10002")]
    await monitor.once("FR")
    assert {(d.user_id,d.item["source_item_id"]) for d in await rows(store, Delivery)} == {
        (101,"10001"),(101,"10002"),(202,"10002")}


async def test_concurrent_claim_cannot_duplicate_upstream_request(environment):
    store, catalog = shared(environment)
    await subscribe(store, catalog)
    parser = Inventory([card("999")])
    started, release = asyncio.Event(), asyncio.Event()
    async def hold():
        started.set()
        await release.wait()
    parser.hook = hold
    first = asyncio.create_task(SharedMonitor(store, parser).once("FR"))
    await started.wait()
    try:
        assert not await SharedMonitor(store, parser).once("FR", ingest=False)
    finally:
        release.set()
        await first
    assert len(parser.calls) == 1


async def test_bounded_async_fanout_and_failed_subscriber_retries_same_page(environment, monkeypatch):
    store, catalog = shared(environment)
    for user in (101,202,303,404,505,606):
        await subscribe(store, catalog, user)
    parser = Inventory([card("999")])
    monitor = SharedMonitor(store, parser)
    original = store.finish
    active = peak = 0
    fail = True
    async def wrapped(job, result, **kwargs):
        nonlocal active, peak, fail
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.02)
            if job.token["user_id"] == 303 and fail:
                fail = False
                raise RuntimeError("synthetic failure")
            return await original(job, result, **kwargs)
        finally:
            active -= 1
    monkeypatch.setattr(store, "finish", wrapped)
    await monitor.once("FR")
    assert 1 < peak <= 4
    assert all(j.lease_id is None for j in await rows(store, Job))
    assert (await rows(store, RegionalCatalog))[0].cursor["page"] == 1
    await tick(store)
    await monitor.once("FR")
    assert [r.page for r in parser.calls] == [1,1]
    assert all(j.baseline_ready for j in await rows(store, Job))


async def test_budget_pause_is_regional_and_cannot_be_bypassed_by_new_subscriber(environment):
    from vpars_worker.parser import ParseFailure
    store, catalog = shared(environment)
    await subscribe(store, catalog)
    class Paused(Inventory):
        async def parse(self, request):
            self.calls.append(request)
            raise ParseFailure("ProxyBudgetDailyExceeded", retry_after=3600, deferred=True)
    parser = Paused()
    monitor = SharedMonitor(store, parser)
    await monitor.once("FR")
    await subscribe(store, catalog, 202)
    assert not await monitor.once("FR")
    assert len(parser.calls) == 1
    feed = (await rows(store, RegionalCatalog))[0]
    assert feed.next_due > utcnow() + timedelta(seconds=3500)
    assert feed.cursor["page"] == 1
    assert feed.cursor["failures"] == 0


async def test_expired_lease_never_advances_feed(environment):
    store, catalog = shared(environment)
    await subscribe(store, catalog)
    parser = Inventory([card("999")])
    async def expire():
        async with store.sessions.begin() as session:
            for model in (Job, RegionalCatalog):
                for row in await session.scalars(select(model)):
                    row.lease_until = utcnow() - timedelta(seconds=1)
    parser.hook = expire
    await SharedMonitor(store, parser).once("FR")
    feed = (await rows(store, RegionalCatalog))[0]
    assert feed.cursor["checks"] == 0 and feed.cursor["page"] == 1
    assert not await rows(store, RegionalIdentity)


async def test_claim_does_not_drop_subscribers_above_old_hundred_limit(environment):
    store, catalog = shared(environment)
    await subscribe(store, catalog)
    await store.ingest()
    original = (await rows(store, Job))[0]
    async with store.sessions.begin() as session:
        for _ in range(105):
            session.add(Job(id=str(uuid4()), token=original.token, query=original.query,
                            query_key=original.query_key))
    assert len(await store.claim("FR")) == 106
