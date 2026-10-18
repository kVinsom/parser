import asyncio
from types import SimpleNamespace

from test_catchup import MovingCatalog, drain, setup_catalog
from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import utcnow
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery, Job, SeenItem
from vpars_worker.monitor import Monitor, Store


def details(item, evidence="relative:just now"):
    return SimpleNamespace(
        photo_urls=item.photo_urls,
        photos_complete=True,
        published_at=None,
        publication_evidence=evidence,
        fetched_at=utcnow(),
    )


async def test_new_lot_below_reencountered_boundary_gets_age_check_and_delivery(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    # A promoted old card shifts the last checkpoint anchor onto page 3;
    # a new lot is displayed immediately below it on the same page.
    parser.ids = ["110"] + parser.ids[:8] + ["3000"] + parser.ids[8:]
    parser.expected = {"3000"}
    for _ in range(90):
        await due(store, Job)
        await monitor.once()
        if (await rows(store, Job))[0].coverage == "catchup_complete":
            break
    observed = next(s for s in await rows(store, SeenItem) if s.item_id == "3000")
    assert observed.eligible and observed.decision["code"] in {"DeliveryCreated", "DeliveryExists"}
    bot = await drain(store)
    assert len(bot.calls) == 1
    delivered = next(d for d in await rows(store, Delivery) if d.item["source_item_id"] == "3000")
    assert delivered.state == "sent"


async def test_unknown_age_retries_html_across_restart_without_resetting_discovery(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    original = (await rows(store, Delivery))[0].item["first_seen_at"]
    calls = []

    class Source:
        async def details(self, item):
            calls.append("initial")
            return details(item, "unknown")

        async def refresh_details(self, item):
            calls.append("html")
            return details(item)

    bot = Bot()
    await Notifier(store, bot, parser=Source()).once()
    waiting = (await rows(store, Delivery))[0]
    assert waiting.state == "pending" and waiting.last_error == "PublicationAgeRetry"
    assert waiting.media_progress["publication_attempts"] == 1 and not bot.calls
    store = Store(store.sessions, store.registry)
    await due(store, Delivery)
    notifier = Notifier(store, bot, parser=Source())
    await notifier.once()
    await notifier.once()
    saved = (await rows(store, Delivery))[0]
    assert saved.state == "sent" and saved.item["first_seen_at"] == original
    assert saved.media_progress["publication_attempts"] == 2
    assert calls == ["initial", "html"] and len(bot.calls) == 1


async def test_unknown_age_stays_bounded_even_if_worker_restarts_every_attempt(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    calls = []

    class Source:
        async def details(self, item):
            calls.append(item.source_item_id)
            return details(item, "unknown")

    bot = Bot()
    for _ in range(6):
        store = Store(store.sessions, store.registry)
        await due(store, Delivery)
        await Notifier(store, bot, parser=Source()).once()
    saved = (await rows(store, Delivery))[0]
    assert len(calls) == 3 and not bot.calls
    assert saved.state == "expired" and saved.last_error == "PublicationAgeUnknown"


async def test_slow_item_verification_does_not_hold_a_second_ready_item(environment):
    store, _ = environment
    await start(environment)
    await Monitor(
        store, Parser(items=[lot("10001", published_at=None), lot("10002", published_at=None)])
    ).once()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Source:
        async def details(self, item):
            calls.append(item.source_item_id)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return details(item)

    bot = Bot()
    notifier = Notifier(store, bot, parser=Source())
    slow = asyncio.create_task(notifier.once(lane="prepare"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(notifier.once(lane="prepare"), 3)
        await asyncio.wait_for(notifier.once(lane="ready"), 3)
        assert not slow.done() and len(bot.calls) == 1
        assert len(calls) == 2 and len(set(calls)) == 2
    finally:
        release.set()
        await slow
    await notifier.once(lane="ready")
    assert len(bot.calls) == 2
