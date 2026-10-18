"""A busy filter must share preparation and sending with other matching searches."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from test_delivery_recovery import choose_query
from test_monitoring import Bot, Parser, lot, rows
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute

from vpars_worker.db import Delivery, DeliveryTurn, SentLot
from vpars_worker.monitor import Monitor, Store


async def queue_filters(environment, brands, second_user, lane):
    store, catalog = environment
    if second_user != 101:
        await catalog.register(second_user, second_user, "second", "Second", 1, start=True)
    searches = []
    for user, brand in zip((101, second_user), brands, strict=True):
        search = await catalog.save(
            user, Filters(brands=[brand], sources={"vinted_fr": ["FR"]}), brand
        )
        searches.append(search)
        await store.control.start(user, "start-" + brand)
    await store.ingest()
    monitor = Monitor(store, Parser(items=[lot("999")]))
    for brand in brands:
        await choose_query(store, brand)
        await monitor.once("FR")

    async def publish(brand, identities):
        await choose_query(store, brand)
        monitor.parser.items = [
            lot(
                identity,
                title=brand,
                brand=Attribute(raw=brand, normalized=brand.casefold()),
                description="Clean",
            )
            for identity in identities
        ]
        await monitor.once("FR")
        if lane == "ready":
            async with store.sessions.begin() as session:
                for row in await session.scalars(
                    select(Delivery).where(Delivery.state == "pending")
                ):
                    row.media_progress = {**row.media_progress, "dispatch_ready": True}

    # The quiet filter's lot is older but still fresh. Repeated large batches
    # from the other filter used to push it behind every new incoming lot.
    await publish(brands[1], ["20001"])
    await publish(brands[0], [str(30000 + n) for n in range(12)])
    return searches, publish


@pytest.mark.parametrize("brands", [("Stone Island", "Amiri"), ("Adidas", "Nike")])
@pytest.mark.parametrize("second_user", [101, 202])
@pytest.mark.parametrize("lane", ["prepare", "ready"])
async def test_concurrent_lanes_share_capacity_across_filters(
    environment, brands, second_user, lane
):
    store, _ = environment
    await queue_filters(environment, brands, second_user, lane)
    # Separate notifier instances exercise the transaction boundary as well as
    # the production case of two concurrent lanes sharing one notifier.
    claimed = await asyncio.gather(
        *(Notifier(store, Bot()).claim(lane, fresh_first=True) for _ in range(2))
    )
    assert len({row.id for row in claimed}) == 2
    assert {row.item["brand"]["normalized"] for row in claimed} == {
        brand.casefold() for brand in brands
    }


@pytest.mark.parametrize("brands", [("Stone Island", "Amiri"), ("Nike", "Adidas")])
@pytest.mark.parametrize("lane", ["prepare", "ready"])
async def test_new_batches_and_restart_do_not_reset_another_filters_turn(environment, brands, lane):
    store, _ = environment
    _, publish = await queue_filters(environment, brands, 202, lane)
    initial = {d.id: d.item["first_seen_at"] for d in await rows(store, Delivery)}
    first = await Notifier(store, Bot()).claim(lane, fresh_first=True)
    assert first.item["brand"]["normalized"] == brands[0].casefold()
    await publish(brands[0], ["40000", "40001"])
    restarted = Notifier(Store(store.sessions, store.registry), Bot())
    second = await restarted.claim(lane, fresh_first=True)
    assert second.item["source_item_id"] == "20001"
    assert second.item["first_seen_at"] == initial[second.id]
    third = await restarted.claim(lane, fresh_first=True)
    assert third.item["source_item_id"] == "40000"


@pytest.mark.parametrize("brands", [("Stone Island", "Amiri"), ("Adidas", "Nike")])
@pytest.mark.parametrize("second_user", [101, 202])
async def test_slow_busy_filter_does_not_block_other_filters_actual_delivery(
    environment, brands, second_user
):
    store, _ = environment
    await queue_filters(environment, brands, second_user, "prepare")
    async with store.sessions.begin() as session:
        for row in await session.scalars(select(Delivery)):
            row.item = {**row.item, "published_at": None, "photos_complete": False}
    busy_entered, release_busy = asyncio.Event(), asyncio.Event()

    class Details:
        async def details(self, item):
            if item.brand.normalized == brands[0].casefold():
                busy_entered.set()
                await release_busy.wait()
            return SimpleNamespace(
                photo_urls=item.photo_urls,
                photos_complete=True,
                description="Clean",
                published_at=utcnow(),
                fetched_at=utcnow(),
                publication_evidence="exact",
                seller=None,
            )

    notifier = Notifier(store, Bot(), parser=Details())
    busy = asyncio.create_task(notifier.once("prepare", fresh_first=True))
    try:
        await asyncio.wait_for(busy_entered.wait(), 2)
        assert await asyncio.wait_for(notifier.once("prepare", fresh_first=True), 2)
        assert await notifier.once("ready", fresh_first=True)
        assert {(s.user_id, s.source_item_id) for s in await rows(store, SentLot)} == {
            (second_user, "20001")
        }
        assert not busy.done()
    finally:
        release_busy.set()
        await asyncio.wait_for(busy, 2)
    assert await notifier.once("ready", fresh_first=True)
    assert len(await rows(store, SentLot)) == len(notifier.bot.calls) == 2


async def test_upgrade_adds_fairness_for_existing_queue_without_rewriting_it(
    environment, monkeypatch
):
    import vpars_worker.db as db

    store, _ = environment
    await queue_filters(environment, ("Stone Island", "Amiri"), 202, "prepare")
    before = {
        d.id: (d.state, d.item, d.candidates, d.media_progress) for d in await rows(store, Delivery)
    }
    engine = store.sessions.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][db.SCHEMA]
    monkeypatch.setattr(db, "SCHEMA", schema)
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP TABLE {schema}.delivery_turns"))
        await connection.execute(text(f"INSERT INTO {schema}.schema_revision(id) VALUES (6)"))
    # create_all adds the scheduling table; no delivery or settings backfill is required.
    await db.migrate(engine)
    await db.migrate(engine)
    assert not await rows(store, DeliveryTurn)
    assert before == {
        d.id: (d.state, d.item, d.candidates, d.media_progress) for d in await rows(store, Delivery)
    }
    notifier = Notifier(store, Bot())
    claimed = [await notifier.claim("prepare", fresh_first=True) for _ in range(2)]
    assert {d.item["brand"]["normalized"] for d in claimed} == {"stone island", "amiri"}
