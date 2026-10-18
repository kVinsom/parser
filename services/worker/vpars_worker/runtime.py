import asyncio
import json
import logging
import os
import subprocess
from collections import Counter
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import ClientSession, ClientTimeout
from pydantic import AnyHttpUrl, field_validator
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from vpars_bot.config import Settings
from vpars_bot.db import SearchRun, database
from vpars_bot.domain import SourceRegistry
from vpars_bot.latency import LatencyMetrics
from vpars_bot.runtime import monitor_receiver_lock
from vpars_bot.search_control import run_state
from vpars_bot.transport import RedisTelegramBudget, TelegramRequestPolicy
from vpars_notifier.service import Notifier

from .activity import SourceActivity
from .db import Delivery, Job, migrate
from .freshness import MAX_AGE_SECONDS
from .shared_catalog import SharedMonitor as Monitor, SharedStore as Store
from .parser import ParserClient

ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = ROOT.parent / "telegram-bot"
PARSER_ROOT = ROOT.parent / "parser"


class WorkerSettings(Settings):
    source_config: Path = BOT_ROOT / "config/sources.json"
    worker_parser_url: AnyHttpUrl = "http://127.0.0.1:8002"
    worker_manage_parser: bool = True
    worker_state_dir: Path = ROOT / ".artifacts"

    @field_validator("source_config")
    @classmethod
    def resolve_source_config(cls, value):
        # The shared Windows .env uses a path relative to the bot, not worker cwd.
        return value if value.is_absolute() else BOT_ROOT / value


def settings():
    return WorkerSettings(_env_file=BOT_ROOT / ".env")


def validate_publication_policy(policy):
    """Reject the legacy recipient allowlist rather than silently excluding users."""
    if (
        not isinstance(policy, dict)
        or policy.get("applies_to") != "all_users"
        or type(policy.get("max_age_seconds")) is not int
        or policy["max_age_seconds"] != MAX_AGE_SECONDS
        or "one_minute_user_ids" in policy
    ):
        raise ValueError("Publication age must be enforced for all users with a 60-second limit")


async def supervise_parser():
    """Own only a parser started here; leave an existing local parser untouched."""
    python = PARSER_ROOT / ".venv/Scripts/python.exe"
    if not python.exists():
        python = PARSER_ROOT / ".venv/bin/python"
    async with ClientSession(timeout=ClientTimeout(total=3)) as http:
        failures = 0
        while failures < 3:
            try:
                async with http.get("http://127.0.0.1:8002/health/live") as response:
                    failures = 0 if response.status == 200 else failures + 1
            except (OSError, TimeoutError):
                failures += 1
            if failures < 3:
                await asyncio.sleep(2)
    process = None
    log_path = ROOT / ".artifacts/parser.log"
    try:
        while True:
            with log_path.open("ab") as logfile:
                process = await asyncio.create_subprocess_exec(
                    str(python),
                    "-m",
                    "uvicorn",
                    "vpars_parser.api:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8002",
                    "--log-level",
                    "warning",
                    "--no-access-log",
                    cwd=str(PARSER_ROOT),
                    stdout=logfile,
                    stderr=logfile,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
                )
                await process.wait()
            logging.getLogger("vpars.monitor").warning("Owned parser exited; restarting")
            await asyncio.sleep(5)
    finally:
        if process and process.returncode is None:
            process.terminate()
            await process.wait()


async def wait_for_parser(url="http://127.0.0.1:8002"):
    async with ClientSession(timeout=ClientTimeout(total=3)) as http:
        while True:
            try:
                async with http.get(str(url).rstrip("/") + "/health/ready") as response:
                    if response.status == 200:
                        return
            except (OSError, TimeoutError):
                pass
            await asyncio.sleep(0.5)


async def run(interval=4, max_pages=3):
    config = settings()
    if not 4 <= interval <= 3600 or not 1 <= max_pages <= 10:
        raise ValueError("Interval must be 4–3600 seconds and pages 1–10")
    engine, sessions = database(config.database_url.get_secret_value())
    redis = Redis.from_url(config.redis_url.get_secret_value())
    bot = Bot(config.telegram_bot_token.get_secret_value(), session=AiohttpSession(timeout=7))
    bot.session.middleware(
        TelegramRequestPolicy(
            RedisTelegramBudget(
                redis,
                bot.id,
                per_second=config.telegram_messages_per_second,
                chat_interval=config.telegram_chat_interval,
            ),
            LatencyMetrics(),
            concurrency=3,
        )
    )
    parser = ParserClient(
        str(config.worker_parser_url), state_path=config.worker_state_dir / "source-cooldowns.json"
    )
    try:
        await redis.ping()
        await migrate(engine)
        policy = json.loads((ROOT / "publication_policy.json").read_text(encoding="utf-8"))
        validate_publication_policy(policy)
        store = Store(sessions, SourceRegistry.read(config.source_config))
        activity = SourceActivity(store.has_active_searches)
        parser.activity = activity
        await store.registry.exchange.refresh(redis)
        async with engine.connect() as connection:
            params = {"bot_id": bot.id % 2147483647}
            if not await connection.scalar(
                text("SELECT pg_try_advisory_lock(198403, :bot_id)"), params
            ):
                raise RuntimeError("A monitoring process already owns this bot")
            try:
                await store.recover_executions()
                await store.refresh_queries()
                async with asyncio.TaskGroup() as group:
                    group.create_task(activity.watch())
                    group.create_task(store.registry.exchange.run(redis))
                    group.create_task(monitor_receiver_lock(connection))
                    if config.worker_manage_parser:
                        group.create_task(supervise_parser())
                    notifier = Notifier(
                        store, bot, parser=parser, handoff_url=config.checkout_window_url
                    )
                    # Both lanes handle fresh arrivals immediately. One also takes
                    # a FIFO item every fifth turn so durable backlog keeps moving.
                    for backlog_every in (0, 5):
                        for lane in ("ready", "prepare"):
                            group.create_task(
                                notifier.run(
                                    lane=lane, fresh_first=True, backlog_every=backlog_every
                                )
                            )
                    await wait_for_parser(config.worker_parser_url)
                    group.create_task(
                        Monitor(store, parser, interval=interval, max_pages=max_pages).run()
                    )
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(198403, :bot_id)"), params)
    finally:
        await parser.close()
        await redis.aclose()
        await bot.session.close()
        await engine.dispose()


async def status():
    config = settings()
    engine, sessions = database(config.database_url.get_secret_value())
    try:
        async with sessions() as session:
            jobs = list(await session.scalars(select(Job)))
            delivery_counts = dict(
                (
                    await session.execute(
                        select(Delivery.state, func.count()).group_by(Delivery.state)
                    )
                ).all()
            )
            run_counts = dict(
                Counter(run_state(run) for run in await session.scalars(select(SearchRun)))
            )
            params = {
                "bot_id": int(config.telegram_bot_token.get_secret_value().split(":", 1)[0])
                % 2147483647
            }
            available = await session.scalar(
                text("SELECT pg_try_advisory_lock(198403, :bot_id)"), params
            )
            if available:
                await session.execute(text("SELECT pg_advisory_unlock(198403, :bot_id)"), params)
            print(
                json.dumps(
                    {
                        "worker_running": not available,
                        "search_states": run_counts,
                        "deliveries": delivery_counts,
                        "jobs": [
                            {
                                "region": j.query["region_id"],
                                "state": j.state,
                                "checks": j.checks,
                                "items_checked": j.items_checked,
                                "matches": j.matches,
                                "last_error": j.last_error,
                                "coverage": j.coverage,
                                "next_page": j.page,
                                "checkpoint": j.checkpoint,
                                "unfinished": j.scan_state,
                                "last_checked": j.last_checked.isoformat()
                                if j.last_checked
                                else None,
                                "next_due": j.next_due.isoformat(),
                            }
                            for j in jobs
                        ],
                    },
                    indent=2,
                )
            )
    finally:
        await engine.dispose()
