import pytest
from sqlalchemy import text
from test_monitoring import rows, start
from vpars_bot.db import Search, SearchRun

from vpars_worker import watchdog
from vpars_worker.db import Job


@pytest.mark.parametrize("condition", ["absent", "running", "maintenance", "journal"])
async def test_recovery_only_launches_missing_worker_and_preserves_searches(
    environment, monkeypatch, tmp_path, condition
):
    store, _ = environment
    await start(environment)
    engine = store.sessions.kw["bind"]
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    # Avoid the real bot's monitor key; advisory locks are database-wide.
    bot_id = 987654
    before = {
        Search: [(s.id, s.filters, s.enabled, s.version) for s in await rows(store, Search)],
        SearchRun: [(r.search_id, r.generation, r.state) for r in await rows(store, SearchRun)],
        Job: [(j.id, j.token, j.checkpoint, j.state) for j in await rows(store, Job)],
    }
    if condition == "journal":
        (tmp_path / ".artifacts").mkdir()
        (tmp_path / ".artifacts/restart-schedule.json").write_text("{}")
    calls = []

    async def launch():
        calls.append(1)

    async with engine.connect() as owner:
        key = bot_id if condition == "running" else 2
        if condition in {"running", "maintenance"}:
            await owner.execute(text("SELECT pg_advisory_lock(198403, :key)"), {"key": key})
        try:
            result = await watchdog.ensure_worker(engine, bot_id, launch)
        finally:
            if condition in {"running", "maintenance"}:
                await owner.execute(text("SELECT pg_advisory_unlock(198403, :key)"), {"key": key})
    assert calls == ([1] if condition == "absent" else [])
    assert (
        result
        == {
            "absent": "checked_process",
            "running": "running",
            "maintenance": "maintenance",
            "journal": "maintenance_recovery_required",
        }[condition]
    )
    assert before == {
        Search: [(s.id, s.filters, s.enabled, s.version) for s in await rows(store, Search)],
        SearchRun: [(r.search_id, r.generation, r.state) for r in await rows(store, SearchRun)],
        Job: [(j.id, j.token, j.checkpoint, j.state) for j in await rows(store, Job)],
    }


async def test_recovery_launcher_failure_releases_maintenance_lock(
    environment, monkeypatch, tmp_path
):
    store, _ = environment
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    engine = store.sessions.kw["bind"]

    async def fail():
        raise OSError("unavailable")

    with pytest.raises(OSError):
        await watchdog.ensure_worker(engine, 987654, fail)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT pg_try_advisory_lock(198403, 2)"))
        await connection.execute(text("SELECT pg_advisory_unlock(198403, 2)"))
