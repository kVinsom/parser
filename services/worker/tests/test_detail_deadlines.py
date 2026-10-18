import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_filter_isolation import overlapping_filters
from test_monitoring import Bot, lot, rows
from vpars_bot.db import utcnow
from vpars_notifier.service import Notifier, detail_budget

from vpars_worker.db import Delivery
from vpars_worker.parser import DETAIL_TIMEOUT_SECONDS, ParserClient


@pytest.mark.parametrize("user,brand", [(101, "Amiri"), (202, "Stone Island")])
async def test_slow_optional_details_release_verified_lot_for_send(
    environment, monkeypatch, user, brand
):
    store, _ = environment
    await overlapping_filters(environment, user, brand)
    cancelled = asyncio.Event()

    class SlowDetails:
        async def details(self, item):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr("vpars_notifier.service.detail_budget", lambda *args, **kwargs: 0.01)
    notifier = Notifier(store, Bot(), parser=SlowDetails())
    await notifier.once("prepare")
    assert cancelled.is_set()
    row = (await rows(store, Delivery))[0]
    assert row.media_progress["dispatch_ready"]
    assert row.last_error == "DetailsDeadlineExceeded"
    await notifier.once("ready")
    assert (await rows(store, Delivery))[0].state == "sent"
    assert notifier.bot.calls[0][1] == user


async def test_slow_mandatory_description_never_bypasses_exclusions(environment, monkeypatch):
    store, catalog = environment
    _, wide = await overlapping_filters(environment, 101, "Amiri")
    await catalog.toggle_filter(101, wide.id, "disable-wide")

    class SlowDetails:
        async def details(self, item):
            await asyncio.Event().wait()

    monkeypatch.setattr("vpars_notifier.service.detail_budget", lambda *args, **kwargs: 0.01)
    notifier = Notifier(store, Bot(), parser=SlowDetails())
    await notifier.once("prepare")
    assert not notifier.bot.calls
    row = (await rows(store, Delivery))[0]
    assert row.state == "pending" and not row.media_progress.get("dispatch_ready")


def test_detail_deadline_reserves_send_time_without_extending_any_age_evidence():
    now = utcnow()
    item = lot(published_at=now - timedelta(seconds=59))
    assert detail_budget(item, {}, now, required=False) == 0
    item = lot(published_at=None, first_seen_at=now - timedelta(seconds=58))
    assert detail_budget(item, {}, now, required=True) == 0
    item = lot(published_at=None, first_seen_at=now - timedelta(seconds=5))
    progress = {
        "publication_checked_at": (now - timedelta(seconds=4)).isoformat(),
        "publication_evidence": "relative:just now",
    }
    assert detail_budget(item, progress, now, required=False) == 0
    assert detail_budget(item, progress, now, required=True) == DETAIL_TIMEOUT_SECONDS + 5


async def test_mandatory_publication_can_finish_after_old_deadline_without_renewing_age(
    environment,
):
    store, _ = environment
    await overlapping_filters(environment, 101, "Nike")
    published = utcnow()
    async with store.sessions.begin() as session:
        row = (await session.scalars(select(Delivery))).one()
        row.item = {**row.item, "published_at": None}

    class SlowPublication:
        async def details(self, item):
            await asyncio.sleep(12.1)
            return SimpleNamespace(
                published_at=published,
                publication_evidence="exact",
                fetched_at=utcnow(),
                photo_urls=item.photo_urls,
                photos_complete=True,
                description="Good condition",
            )

    notifier = Notifier(store, Bot(), parser=SlowPublication())
    await notifier.once("prepare")
    row = (await rows(store, Delivery))[0]
    assert row.media_progress["dispatch_ready"]
    assert row.item["published_at"] == published.isoformat().replace("+00:00", "Z")
    await notifier.once("ready")
    assert len(notifier.bot.calls) == 1
    assert (await rows(store, Delivery))[0].state == "sent"


async def test_cancelled_recipient_keeps_shared_fetch_until_last_waiter_leaves(monkeypatch):
    client = ParserClient()
    started, stopped = asyncio.Event(), asyncio.Event()

    async def stalled(*args, **kwargs):
        async with client.detail_access:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr(client, "_details", stalled)
    tasks = [asyncio.create_task(client.details(lot())) for _ in range(2)]
    try:
        await asyncio.wait_for(started.wait(), 1)
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        assert not stopped.is_set() and len(client.detail_tasks) == 1
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        assert stopped.is_set() and not client.detail_tasks and not client.detail_waiters
        # Both slots are immediately available to fresh recipients.
        await asyncio.wait_for(client.detail_access.acquire(), 1)
        await asyncio.wait_for(client.detail_access.acquire(), 1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
