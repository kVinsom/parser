"""Recover a missing local worker without creating or changing search intent."""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger("vpars.watchdog")
ROOT = Path(__file__).resolve().parents[1]


async def ensure_worker(engine, bot_id, launch):
    """Share the maintenance lock with safe restarts; never replace a live owner."""
    async with engine.connect() as connection:
        if not await connection.scalar(text("SELECT pg_try_advisory_lock(198403, 2)")):
            return "maintenance"
        try:
            running = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                    "AND classid=198403 AND objid=:bot_id AND objsubid=2 AND granted)"
                ),
                {"bot_id": bot_id % 2147483647},
            )
            if running:
                return "running"
            # A journal survives interruption of a manual restart. Its owner
            # must restore schedules before recovery can launch the service.
            if (ROOT / ".artifacts/restart-schedule.json").exists():
                return "maintenance_recovery_required"
            await launch()
            return "checked_process"
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(198403, 2)"))


async def launch_worker():
    executable = shutil.which("pwsh") or "powershell.exe"
    process = await asyncio.create_subprocess_exec(
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(ROOT / "run.ps1"),
        "-Mode",
        "ensure",
        cwd=ROOT,
        **({"creationflags": 0x08000000} if os.name == "nt" else {}),
    )
    if await process.wait():
        raise RuntimeError("Worker recovery launcher failed")


async def check():
    from vpars_bot.db import database

    from .runtime import settings

    config = settings()
    engine, _ = database(config.database_url.get_secret_value())
    try:
        bot_id = int(config.telegram_bot_token.get_secret_value().split(":", 1)[0])
        async with asyncio.timeout(25):
            result = await ensure_worker(engine, bot_id, launch_worker)
        logger.info("Worker recovery check: %s", result)
    except Exception as error:
        # The next scheduled check retries dependency outages; never print secrets.
        logger.warning("Worker recovery unavailable (%s)", type(error).__name__)
        raise SystemExit(1) from None
    finally:
        await engine.dispose()
