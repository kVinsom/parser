import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, lot, rows, start
from vpars_bot.db import utcnow
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery, SentLot
from vpars_worker.monitor import Monitor


async def test_ready_queue_drains_without_per_item_or_enrichment_sleep(environment, monkeypatch):
    """Old cancelled work must not add 0.5 s per row before a new lot can send."""
    import vpars_notifier.service as service

    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(photos_complete=False)])).once()
    async with store.sessions.begin() as session:
        current = await session.scalar(select(Delivery))
        for index in range(30):
            session.add(
                Delivery(
                    id=f"{index:064x}",
                    user_id=101,
                    item=current.item,
                    candidates=[],
                    created_at=current.created_at - timedelta(minutes=1),
                    next_due=utcnow() - timedelta(minutes=1),
                )
            )

    class Details:
        async def details(self, item):
            return SimpleNamespace(
                photo_urls=item.photo_urls,
                photos_complete=True,
                published_at=None,
                description="Good condition",
                seller=None,
            )

    class ConfirmCommittedBot(Bot):
        async def send_rich_message(self, *args, **kwargs):
            # Even with no scheduling pause, enrichment must already be durable.
            async with store.sessions() as session:
                enriched = await session.get(Delivery, current.id)
                assert enriched.item["photos_complete"]
                assert enriched.media_progress["description_checked"]
            return await super().send_rich_message(*args, **kwargs)

    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if seconds:
            raise asyncio.CancelledError  # First idle wait ends this bounded test.

    monkeypatch.setattr(service, "asyncio", SimpleNamespace(sleep=sleep, wait_for=asyncio.wait_for))
    bot = ConfirmCommittedBot()
    with pytest.raises(asyncio.CancelledError):
        await Notifier(store, bot, parser=Details()).run()
    assert sleeps == [0] * 32 + [0.5]  # 30 stale rows + enrichment + send, then idle.
    assert len(bot.calls) == 1
    assert len(await rows(store, SentLot)) == 1
    states = [d.state for d in await rows(store, Delivery)]
    assert states.count("cancelled") == 30 and states.count("sent") == 1


async def test_oldest_ready_delivery_is_not_starved_by_new_arrivals(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot("10001"), lot("10002")])).once()
    async with store.sessions.begin() as session:
        for row in await session.scalars(select(Delivery)):
            row.media_progress = {"dispatch_ready": True}
            row.created_at = utcnow() - timedelta(
                seconds=30 if row.item["source_item_id"] == "10001" else 1
            )
    notifier = Notifier(store, Bot())
    claimed = await notifier.claim(lane="ready")
    assert claimed.item["source_item_id"] == "10001"
