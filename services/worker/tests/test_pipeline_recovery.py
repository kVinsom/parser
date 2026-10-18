import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import Inbox, utcnow
from vpars_notifier.service import Notifier
from vpars_parser.models import ParseRequest

from vpars_worker.db import Delivery, Job, SeenItem
from vpars_worker.monitor import Monitor


async def test_new_item_missing_attributes_can_match_later_without_renewing_age(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(size=None)])).once()
    assert not await rows(store, Delivery)
    seen = next(s for s in await rows(store, SeenItem) if s.item_id == "10001")
    assert seen.eligible
    await due(store, Job)
    await Monitor(store, Parser()).once()
    queued = (await rows(store, Delivery))[0]
    assert queued.item["first_seen_at"].replace("Z", "+00:00") == seen.first_seen_at.isoformat()


async def test_expired_missing_attributes_cannot_become_new_again(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(size=None)])).once()
    job = (await rows(store, Job))[0]
    async with store.sessions.begin() as session:
        seen = await session.get(SeenItem, (job.id, "10001"))
        seen.first_seen_at = utcnow() - timedelta(seconds=61)
    await due(store, Job)
    await Monitor(store, Parser()).once()
    assert not await rows(store, Delivery)


async def test_first_visit_to_deeper_page_delivers_unknown_age_after_checkpoint(environment):
    store, _ = environment
    await start(environment)
    parser = Parser()
    parser.more = True
    await Monitor(store, parser).once()
    assert (await rows(store, Job))[0].page == 2
    await due(store, Job)
    await Monitor(store, Parser(items=[lot("20001", published_at=None), lot("20002")])).once()
    assert {d.item["source_item_id"] for d in await rows(store, Delivery)} == {
        "10001",
        "20001",
        "20002",
    }
    assert (await rows(store, Job))[0].coverage == "catchup_verifying"


@pytest.mark.parametrize("deferred", [False, True])
async def test_stuck_or_deferred_menu_cannot_starve_lots(environment, deferred):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    async with store.sessions.begin() as session:
        session.add(
            Inbox(
                bot_id=42,
                update_id=10,
                state="received",
                lane_key="chat:101",
                received_at=utcnow() if deferred else utcnow() - timedelta(seconds=3),
                next_attempt_at=utcnow() + timedelta(seconds=30) if deferred else utcnow(),
            )
        )
    bot = Bot()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1


async def test_preparation_exception_releases_lease_without_ambiguous_resend(
    environment, monkeypatch
):
    import vpars_notifier.service as service

    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    original = service.prepare_lot

    async def broken(*args):
        raise ValueError("synthetic formatting failure")

    monkeypatch.setattr(service, "prepare_lot", broken)
    bot = Bot()
    with pytest.raises(ValueError):
        await Notifier(store, bot).once()
    failed = (await rows(store, Delivery))[0]
    assert failed.state == "pending" and failed.lease_id is None and failed.uncertain == 0
    assert not bot.calls
    monkeypatch.setattr(service, "prepare_lot", original)
    await due(store, Delivery)
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1


async def test_crashed_preparation_recovers_without_sixty_second_delay(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    notifier = Notifier(store, Bot())
    claimed = await notifier.claim()
    async with store.sessions.begin() as session:
        (await session.get(Delivery, claimed.id)).lease_until = utcnow() - timedelta(seconds=1)
    assert await notifier.claim() is None
    assert (await rows(store, Delivery))[0].state == "pending"
    assert await notifier.once()
    assert len(notifier.bot.calls) == 1


@pytest.mark.parametrize("transport_started", [False, True])
async def test_restart_recovers_preparation_but_quarantines_ambiguous_send(
    environment, transport_started
):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    notifier = Notifier(store, Bot())
    claimed = await notifier.claim()
    async with store.sessions.begin() as session:
        saved = await session.get(Delivery, claimed.id)
        saved.media_progress = {"transport_started": transport_started}
    await store.recover_executions()
    saved = (await rows(store, Delivery))[0]
    assert saved.state == ("unknown" if transport_started else "pending")
    assert saved.lease_id is None and saved.next_due <= utcnow()


async def test_ready_sender_continues_while_another_gallery_is_blocked(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot("10001"), lot("10002", photos_complete=False)])).once()
    async with store.sessions.begin() as session:
        ready = await session.scalar(
            select(Delivery).where(Delivery.item["source_item_id"].as_string() == "10001")
        )
        ready.media_progress = {"dispatch_ready": True, "seller_checked": True}
    entered, release = asyncio.Event(), asyncio.Event()

    class Details:
        async def details(self, item):
            entered.set()
            await release.wait()
            return SimpleNamespace(
                photo_urls=[],
                photos_complete=False,
                published_at=item.published_at,
                description="",
                fetched_at=utcnow(),
                publication_evidence="exact",
                seller=None,
            )

    bot = Bot()
    notifier = Notifier(store, bot, parser=Details())
    preparing = asyncio.create_task(notifier.once("prepare"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert await asyncio.wait_for(notifier.once("ready"), 3)
        assert len(bot.calls) == 1 and not preparing.done()
    finally:
        release.set()
        await preparing
    assert await notifier.once("ready")
    assert len(bot.calls) == 2


async def test_transient_catalog_failure_recovers_quickly_and_releases_lease(environment):
    store, _ = environment
    await start(environment)
    parser = Parser()
    parser.error = "GatewayTimeout"
    await Monitor(store, parser).once()
    job = (await rows(store, Job))[0]
    assert job.state == "waiting" and job.lease_id is None
    assert 0 < (job.next_due - utcnow()).total_seconds() <= 4


async def test_observed_invalid_card_can_be_repaired_after_baseline(environment):
    store, _ = environment
    await start(environment)
    job = (await store.claim())[0]
    result = await Parser(items=[lot("10002")]).parse(ParseRequest(**job.query))
    result.observed_item_ids = ["10001", "10002"]
    result.completeness = "partial"
    result.identities_complete = True
    await store.finish(job, result, interval=4, max_pages=3)
    await due(store, Job)
    await Monitor(store, Parser()).once()
    assert {d.item["source_item_id"] for d in await rows(store, Delivery)} == {"10001", "10002"}
