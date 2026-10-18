"""Local inbox benchmark; real PostgreSQL, simulated Telegram delay, no token/network API."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import statistics
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from aiogram.types import Update
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpars_bot.db import Base
from vpars_bot.runtime import UpdateInbox


async def measure(url, count, delay, lanes):
    schema = "bench_bot_" + uuid4().hex
    root = create_async_engine(url, hide_parameters=True)
    async with root.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, hide_parameters=True, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        inbox = UpdateInbox(async_sessionmaker(engine, expire_on_commit=False), 42)
        latencies, completed = [], asyncio.Event()
        updates = [
            Update.model_validate(
                {
                    "update_id": i + 1,
                    "message": {
                        "message_id": i + 1,
                        "date": 1,
                        "chat": {"id": 100 + i % lanes, "type": "private"},
                        "from": {"id": 100 + i % lanes, "is_bot": False, "first_name": "Benchmark"},
                        "text": "/id",
                    },
                }
            )
            for i in range(count)
        ]
        ingest = perf_counter()
        if hasattr(inbox, "receive_many"):
            await inbox.receive_many(updates)
        else:
            for update in updates:
                await inbox.receive(update)
        ingest_ms = (perf_counter() - ingest) * 1000
        started = perf_counter()

        class Dispatcher:
            async def feed_update(self, bot, update):
                await asyncio.sleep(delay)
                latencies.append((perf_counter() - started) * 1000)
                if len(latencies) == count:
                    completed.set()

        task = asyncio.create_task(inbox.pump(Dispatcher(), None))
        try:
            await asyncio.wait_for(completed.wait(), timeout=60)
            await asyncio.sleep(0.15)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        ordered = sorted(latencies)
        return {
            "ingest_ms": round(ingest_ms, 2),
            "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[int((count - 1) * 0.95)], 2),
            "last_response_ms": round(max(ordered), 2),
        }
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    url = os.environ.get(
        "TEST_POSTGRES_URL", "postgresql+asyncpg://vpars:vpars_local@127.0.0.1:15432/vpars_bot"
    )
    result = {
        "kind": "synthetic Telegram latency, real local PostgreSQL",
        "updates": 32,
        "simulated_api_delay_ms": 50,
        "runtime_sha256": hashlib.sha256(Path("vpars_bot/runtime.py").read_bytes()).hexdigest(),
        "runs": [await measure(url, 32, 0.05, 32) for _ in range(args.runs)],
    }
    result["median"] = {
        key: round(statistics.median(r[key] for r in result["runs"]), 2)
        for key in result["runs"][0]
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps(result["median"]))


if __name__ == "__main__":
    asyncio.run(main())
