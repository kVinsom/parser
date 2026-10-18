import json
from datetime import timedelta

from sqlalchemy import select
from test_monitoring import Parser, lot, rows, start
from vpars_bot.db import utcnow

from vpars_worker.db import Delivery, Job
from vpars_worker.maintenance import hold, restore
from vpars_worker.monitor import Monitor


async def test_restart_drain_keeps_active_sends_and_restores_exact_due_dates(environment, tmp_path):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot("10001"), lot("10002")])).once()
    journal = {
        "until": (utcnow() + timedelta(minutes=10)).isoformat(),
        "jobs": {},
        "deliveries": {},
    }
    path = tmp_path / "restart.json"
    async with store.sessions.begin() as session:
        deliveries = list(await session.scalars(select(Delivery).order_by(Delivery.id)))
        deliveries[0].state = "sending"
        deliveries[0].lease_until = utcnow() + timedelta(seconds=120)
        deliveries[0].media_progress = {"transport_started": True}
        sending_id = deliveries[0].id
    original = {j.id: j.next_due for j in await rows(store, Job)}
    assert await hold(store.sessions, journal, path) == 1
    assert sending_id not in journal["deliveries"]
    assert not await store.claim()
    async with store.sessions.begin() as session:
        sending = await session.get(Delivery, sending_id)
        assert sending.state == "sending" and sending.media_progress["transport_started"]
        sending.state = "sent"
        other = await session.scalar(select(Delivery).where(Delivery.id != sending_id))
        future_backoff = utcnow() + timedelta(minutes=15)
        other.next_due = future_backoff
    assert await hold(store.sessions, journal, path) == 0
    await restore(store.sessions, json.loads(path.read_text()))
    assert {j.id: j.next_due for j in await rows(store, Job)} == original
    assert (
        next(d.next_due for d in await rows(store, Delivery) if d.id != sending_id)
        == future_backoff
    )


def test_priority_refresh_cannot_confirm_a_changed_head():
    from types import SimpleNamespace

    from vpars_worker.catchup import advance, refresh_head_if_due

    now = utcnow()
    checkpoint = {"ids": ["old"]}
    job = SimpleNamespace(
        baseline_ready=True,
        page=2,
        checkpoint=checkpoint,
        scan_state={
            "step": "confirm_head",
            "phase": "verify",
            "started_at": now.isoformat(),
            "head": {"1": ["old"], "2": ["older"]},
            "requests": 4,
            "new_in_sweep": 0,
        },
    )
    refresh_head_if_due(job, now, force=True)
    result = SimpleNamespace(
        observed_item_ids=["new", "old"],
        items=[],
        completeness="complete",
        identities_complete=True,
        pagination=SimpleNamespace(has_more="true"),
    )
    advance(job, job.scan_state, result, {"new"}, now, 3)
    assert job.page == 1 and job.scan_state["phase"] == "verify"
    assert job.scan_state["step"] == "forward" and job.checkpoint == checkpoint
