"""Exercise the same search and delivery path for administrators and other users."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rows
from vpars_bot.db import ControlCommand, User, utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery, Job, SeenItem, SentLot
from vpars_worker.monitor import Monitor, Store


@pytest.mark.parametrize("admin_user", [101, 202])
async def test_same_search_age_delivery_and_recovery_rules_for_every_user(environment, admin_user):
    store, catalog = environment
    await catalog.register(202, 202, "second", "Second", 2, start=True)
    async with store.sessions.begin() as session:
        for user in await session.scalars(select(User)):
            user.is_admin = user.id == admin_user
    filters = Filters(brands=["Stone Island"], sources={"vinted_fr": ["FR"]})
    for recipient in (101, 202):
        await catalog.save(recipient, filters, f"save-{recipient}")
        await store.control.start(recipient, f"start-{recipient}")

    baseline = Parser(items=[lot("999")])
    await Monitor(store, baseline).once()
    jobs = await rows(store, Job)
    assert len(jobs) == 2 and all(job.baseline_ready for job in jobs)
    assert len(baseline.calls) == 1  # Both users share the same source observation.
    assert len({job.query_key for job in jobs}) == 1
    assert jobs[0].query == jobs[1].query
    assert not await rows(store, Delivery)

    # A known one-hour-old listing appeared after Start, so only its age rejects it.
    async with store.sessions.begin() as session:
        for command in await session.scalars(select(ControlCommand)):
            command.created_at = utcnow() - timedelta(hours=2)
    await due(store, Job)
    parser = Parser(
        items=[
            lot("10001", published_at=None),
            lot("10002", published_at=utcnow() - timedelta(hours=1)),
            lot("10003", published_at=None),
        ]
    )
    await Monitor(store, parser).once()
    assert len(parser.calls) == 1 and parser.calls[0].timeout_seconds == 20
    expected = {
        (recipient, identity) for recipient in (101, 202) for identity in ("10001", "10003")
    }
    assert {(d.user_id, d.item["source_item_id"]) for d in await rows(store, Delivery)} == expected
    old_observations = [s for s in await rows(store, SeenItem) if s.item_id == "10002"]
    assert len(old_observations) == 2
    assert all(s.item and s.decision["code"] == "PublicationTooOld" for s in old_observations)

    class Details:
        async def details(self, item):
            return SimpleNamespace(
                published_at=None,
                publication_evidence="relative:just now"
                if item.source_item_id == "10001"
                else "relative:3 hours ago",
                fetched_at=utcnow(),
                photo_urls=item.photo_urls,
                photos_complete=True,
            )

    # A fresh process still applies the same checks to both queued recipients.
    store = Store(store.sessions, store.registry)
    bot = Bot()
    notifier = Notifier(store, bot, parser=Details())
    for _ in range(12):
        if not await notifier.once():
            break
    assert sorted(call[1] for call in bot.calls) == [101, 202]
    assert bot.calls[0][2] == bot.calls[1][2]  # Identical lot content and message format.
    assert {(s.user_id, s.source_item_id) for s in await rows(store, SentLot)} == {
        (101, "10001"),
        (202, "10001"),
    }
    rejected = [d for d in await rows(store, Delivery) if d.item["source_item_id"] == "10003"]
    assert len(rejected) == 2
    assert all(
        d.state == "expired" and d.last_error == "PublicationAgeNotAllowed" for d in rejected
    )

    # Timeout backoff/adaptation is driven by the shared query, never by recipient role.
    before = {job.id: job.checkpoint for job in await rows(store, Job)}
    await due(store, Job)
    parser.error = "GatewayTimeout"
    await Monitor(store, parser).once()
    for job in await rows(store, Job):
        assert job.last_error == "GatewayTimeout" and job.scheduling["head_timeouts"] == 1
        assert job.checkpoint == before[job.id]
    store = Store(store.sessions, store.registry)
    await due(store, Job)
    parser.error = None
    await Monitor(store, parser).once()
    assert parser.calls[-1].timeout_seconds == 24
    assert all(job.scheduling["head_timeouts"] == 0 for job in await rows(store, Job))

    # Stop is personal even though the processing rules are shared.
    await store.control.stop(admin_user, "stop-admin")
    await due(store, Job)
    parser.items = [lot("10004")]
    await Monitor(store, parser).once()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 3 and bot.calls[-1][1] == ({101, 202} - {admin_user}).pop()
