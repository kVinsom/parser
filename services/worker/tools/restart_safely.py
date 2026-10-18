"""Apply local changes after draining requests/sends, with durable schedule recovery."""

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), *(str(ROOT.parent / s) for s in ("telegram-bot", "parser", "notifier"))]
from sqlalchemy import text  # noqa: E402
from vpars_bot.db import database, utcnow  # noqa: E402

from vpars_worker.maintenance import hold, restore  # noqa: E402
from vpars_worker.runtime import settings  # noqa: E402


async def checkpoint_source_pauses(sessions, journal):
    """Adopt pauses from older workers too, after all source work has drained."""
    path = ROOT / ".artifacts/source-cooldowns.json"
    saved = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"version": 1, "regions": {}}
    )
    now = utcnow()

    def keep(region, code, until):
        region = "UK" if region == "GB" else region
        old = saved["regions"].get(region)
        if until > now and (old is None or until > datetime.fromisoformat(old["until"])):
            saved["regions"][region] = {"code": code, "until": until.isoformat()}

    async with sessions() as session:
        for row in (
            await session.execute(
                text("""
            SELECT j.query->>'region_id' region, p.result->>'error' code,
                p.observed_at + make_interval(secs => COALESCE((p.result->>'retry_after')::int, 120)) AS until
            FROM vpars_monitor.catalog_pages p JOIN vpars_monitor.catalog_jobs j ON j.id=p.job_id
            WHERE p.observed_at>now()-interval '1 day'
              AND p.result->>'error' IN ('SourceRateLimited','AccessBlocked')
        """)
            )
        ).mappings():
            keep(row["region"], row["code"], row["until"])
        for row in (
            await session.execute(
                text("""
            SELECT id,item->>'region_id' region,media_progress->>'detail_error' code,next_due
            FROM vpars_monitor.deliveries
            WHERE created_at>now()-interval '1 day'
              AND last_error IN ('SourceRateLimited','AccessBlocked','FreshnessWindowExpired')
              AND media_progress->>'detail_error' IN ('SourceRateLimited','AccessBlocked')
        """)
            )
        ).mappings():
            due = journal["deliveries"].get(row["id"])
            keep(
                row["region"], row["code"], datetime.fromisoformat(due) if due else row["next_due"]
            )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(saved), encoding="utf-8")
    temporary.replace(path)


async def run(restore_only, reload_gateway):
    engine, sessions = database(settings().database_url.get_secret_value())
    path = ROOT / ".artifacts/restart-schedule.json"
    try:
        async with engine.connect() as guard:
            if not await guard.scalar(text("SELECT pg_try_advisory_lock(198403, 2)")):
                raise RuntimeError("Another restart is already in progress")
            if path.exists():
                await restore(sessions, json.loads(path.read_text(encoding="utf-8")))
                path.unlink()
            if restore_only:
                print("Saved work schedules restored.", flush=True)
                return
            journal = {
                "until": (utcnow() + timedelta(minutes=10)).isoformat(),
                "jobs": {},
                "deliveries": {},
            }
            started = monotonic()
            try:
                while True:
                    active = await hold(sessions, journal, path)
                    if active == 0:
                        break
                    if monotonic() - started > 90:
                        raise TimeoutError("Requests did not drain; restart cancelled")
                    await asyncio.sleep(0.5)
                print("In-flight requests and sends drained; applying update.", flush=True)
                await checkpoint_source_pauses(sessions, journal)
                args = [
                    shutil.which("pwsh") or "powershell.exe",
                    "-NoProfile",
                    "-File",
                    str(ROOT / "run.ps1"),
                    "-Mode",
                    "restart",
                    "-Interval",
                    "4",
                    "-Pages",
                    "3",
                ]
                if reload_gateway:
                    args.append("-ReloadGateway")
                process = await asyncio.create_subprocess_exec(*args, cwd=ROOT)
                if await process.wait():
                    raise RuntimeError("Restart command failed; schedules will be restored")
            finally:
                await restore(sessions, journal)
                if path.exists():
                    path.unlink()
                print("Work schedules restored; pending catch-up preserved.", flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore-only", action="store_true")
    parser.add_argument("--reload-gateway", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.restore_only, args.reload_gateway))
