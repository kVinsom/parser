"""Local launcher using the project's existing Telegram Python environment."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [
    str(ROOT),
    str(ROOT.parent / "telegram-bot"),
    str(ROOT.parent / "parser"),
    str(ROOT.parent / "notifier"),
]

from vpars_bot.db import database  # noqa: E402
from vpars_bot.supervision import supervise  # noqa: E402

from vpars_worker.db import migrate  # noqa: E402
from vpars_worker.runtime import run, settings, status  # noqa: E402
from vpars_worker.watchdog import check as watchdog_check  # noqa: E402


async def schema():
    engine, _ = database(settings().database_url.get_secret_value())
    try:
        await migrate(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "migrate", "status", "watchdog"])
    parser.add_argument("--interval", type=int, default=4)
    parser.add_argument(
        "--pages",
        type=int,
        default=3,
        help="Pages per catch-up portion; unfinished work has no total page cap",
    )
    args = parser.parse_args()
    (ROOT / ".artifacts").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    logging.getLogger("vpars.monitor").setLevel(logging.INFO)
    logging.getLogger("vpars.notifier").setLevel(logging.INFO)
    logging.getLogger("vpars.watchdog").setLevel(logging.INFO)
    try:
        asyncio.run(
            supervise(lambda: run(args.interval, args.pages))
            if args.command == "run"
            else watchdog_check()
            if args.command == "watchdog"
            else schema()
            if args.command == "migrate"
            else status()
        )
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Never print credentials, HTTP URLs containing tokens or raw exception payloads.
        print(f"Monitoring stopped ({type(exc).__name__}); check local services.", file=sys.stderr)
        raise SystemExit(1) from None
