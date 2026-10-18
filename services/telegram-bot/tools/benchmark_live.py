"""Opt-in, at most five real replies to the single private /start sender; no raw secrets in output."""

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import GetUpdates
from benchmark_total import distribution, environment

from vpars_bot.config import Settings
from vpars_bot.db import database
from vpars_bot.domain import Filters
from vpars_bot.runtime import exclusive_receiver


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--send-test-responses", action="store_true")
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    if not args.send_test_responses or not 1 <= args.count <= 5:
        parser.error("Explicit --send-test-responses and --count between 1 and 5 are required")
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    settings = Settings()
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise RuntimeError("Missing local Telegram token")
    bot = Bot(token, session=AiohttpSession(timeout=7))
    engine, _ = database(settings.database_url.get_secret_value())
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "real_telegram": True,
        "boundary": "local webhook POST -> real Telegram Bot API response received",
        "excluded": [
            "user-device latency",
            "Telegram inbound delivery",
            "message rendering on device",
        ],
        "recipient_selection": "single private /start sender, explicitly authorized by operator",
        "data_scope": "disposable PostgreSQL schema and Redis prefix; normal bot data unchanged",
        "samples": [],
        "status": "running",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    try:
        async with exclusive_receiver(engine, bot.id):
            webhook = await bot.get_webhook_info()
            if webhook.url:
                raise RuntimeError("A webhook is configured; benchmark will not disable it")
            # No offset: inspect without acknowledging or discarding existing Telegram updates.
            updates = await bot(GetUpdates(timeout=0, limit=100, allowed_updates=["message"]))
            senders = {
                update.message.from_user.id
                for update in updates
                if update.message
                and update.message.chat.type == "private"
                and update.message.from_user
                and update.message.from_user.id == update.message.chat.id
                and (update.message.text or "").split("@")[0].strip() == "/start"
            }
            if len(senders) != 1:
                raise RuntimeError("Expected exactly one private /start sender; no messages sent")
            user_id = senders.pop()
            report["get_me_ms"] = []
            for _ in range(10):
                started = perf_counter()
                await bot.get_me()
                report["get_me_ms"].append((perf_counter() - started) * 1000)
                await asyncio.sleep(0.1)
            report["get_me_summary_ms"] = distribution(report["get_me_ms"])
            async with environment(
                settings.database_url.get_secret_value(),
                settings.redis_url.get_secret_value(),
                None,
                live_token=token,
                live_limit=args.count,
            ) as env:
                catalog, _, _, submit, _, metrics = env
                await catalog.register(user_id, user_id, None, "Benchmark", 1, start=True)
                await catalog.save(user_id, Filters(sources={"vinted": ["FR"]}), "benchmark-only")
                for index in range(args.count):
                    async with asyncio.timeout(25):
                        trace = await submit(10 + index, user_id)
                        await trace.done.wait()
                    report["samples"].append({"sample": index + 1, **trace.result()})
                    save()  # Keep completed evidence if a later call fails; never resend a whole run.
                    print(json.dumps(report["samples"][-1]), flush=True)
                    await asyncio.sleep(1.1)  # No intentional pacing backlog in the idle test.
                report["metrics"] = metrics.snapshot()
            report["summary_ms"] = {
                name: distribution([sample[name] for sample in report["samples"]])
                for name in report["samples"][0]
                if name.endswith("_ms")
            }
            if len(report["samples"]) > 1:
                report["warm_response_ms"] = distribution(
                    [sample["response_ms"] for sample in report["samples"][1:]]
                )
            report["status"] = "completed"
            save()
            print(json.dumps({"summary_ms": report["summary_ms"]}), flush=True)
    except Exception as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        save()
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
