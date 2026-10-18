"""Linux container entry points, graceful shutdown and read-only health probes."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = ROOT / "services/telegram-bot"
sys.path[:0] = [
    str(ROOT / "services" / name)
    for name in ("telegram-bot", "worker", "parser", "notifier")
]


async def serve(factory):
    from vpars_bot.supervision import supervise

    task = asyncio.create_task(supervise(factory))
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


async def health(role):
    from redis.asyncio import Redis
    from sqlalchemy import text
    from vpars_bot.config import Settings
    from vpars_bot.db import database

    config = Settings()
    engine, _ = database(config.database_url.get_secret_value())
    redis = Redis.from_url(config.redis_url.get_secret_value())
    try:
        async with asyncio.timeout(8):
            await redis.ping()
            async with engine.connect() as connection:
                owned = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                        "AND classid=:role AND objid=:bot AND objsubid=2 AND granted)"
                    ),
                    {
                        "role": 198401 if role == "bot" else 198403,
                        "bot": int(
                            config.telegram_bot_token.get_secret_value().split(":", 1)[
                                0
                            ]
                        )
                        % 2147483647,
                    },
                )
                if not owned:
                    raise RuntimeError("ProcessNotReady")
    finally:
        await redis.aclose()
        await engine.dispose()


def migrate():
    from alembic import command
    from alembic.config import Config
    from vpars_bot.config import Settings
    from vpars_bot.db import database
    from vpars_worker.db import migrate as worker_migrate

    config = Config(str(BOT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = Settings().database_url.get_secret_value()
    command.upgrade(config, "head")

    async def worker():
        engine, _ = database(config.attributes["database_url"])
        try:
            await worker_migrate(engine)
        finally:
            await engine.dispose()

    asyncio.run(worker())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "role", choices=["bot", "worker", "parser", "migrate", "health"]
    )
    parser.add_argument("health_role", nargs="?", choices=["bot", "worker"])
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    for name in ("vpars_bot", "vpars.monitor", "vpars.notifier", "vpars.source_rate"):
        logging.getLogger(name).setLevel(logging.INFO)
    try:
        if args.role == "parser":
            os.execv(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "vpars_parser.api:create_app",
                    "--factory",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8002",
                    "--no-access-log",
                ],
            )
        elif args.role == "migrate":
            migrate()
        elif args.role == "health":
            if not args.health_role:
                parser.error("health requires bot or worker")
            asyncio.run(health(args.health_role))
        elif args.role == "bot":
            from vpars_bot.config import Settings
            from vpars_bot.runtime import run

            config = Settings()
            asyncio.run(serve(lambda: run(config)))
        else:
            from vpars_worker.runtime import run

            asyncio.run(
                serve(
                    lambda: run(
                        int(os.environ.get("WORKER_INTERVAL", "4")),
                        int(os.environ.get("WORKER_PAGES", "3")),
                    )
                )
            )
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - never log raw configuration or credentials
        print(
            f"{args.role} failed ({type(error).__name__}); inspect configuration and dependencies.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
