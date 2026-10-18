"""Read-only live audit; --record stores hashes of settings for post-restart comparison."""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), *(str(ROOT.parent / s) for s in ("telegram-bot", "parser", "notifier"))]

from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from vpars_bot.db import Search, SearchRun, Subscription, User, database  # noqa: E402
from vpars_bot.domain import Filters, SourceRegistry, euro_price  # noqa: E402
from vpars_bot.exchange import CACHE_KEY  # noqa: E402

from vpars_worker.db import Job, SentLot  # noqa: E402
from vpars_worker.monitor import Store  # noqa: E402
from vpars_worker.runtime import settings  # noqa: E402


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def run(record):
    config = settings()
    engine, sessions = database(config.database_url.get_secret_value())
    redis = Redis.from_url(config.redis_url.get_secret_value())
    path = ROOT / ".artifacts/euro-price-settings-before.json"
    report = {}
    try:
        registry = SourceRegistry.read(config.source_config)
        cached = await redis.get(CACHE_KEY)
        if cached:
            registry.exchange.load(json.loads(cached))
        report["rate_date"] = str(registry.exchange.as_of)
        report["GBP_per_EUR"] = str(registry.exchange.rates.get("GBP"))
        snapshot = {}
        async with sessions() as session:
            for cls in (User, Search, Subscription):
                columns = list(cls.__table__.columns)
                if cls is User:
                    columns = [User.id, User.is_admin, User.delivery_epoch, User.enabled_sources]
                records = [list(row) for row in (await session.execute(select(*columns))).all()]
                snapshot[cls.__name__] = sorted(digest(row) for row in records)
                report[cls.__name__ + "_count"] = len(records)
            runs = list(await session.scalars(select(SearchRun)))
            snapshot["intent"] = sorted(
                digest(
                    [
                        r.search_id,
                        r.source_id,
                        r.generation,
                        "running" if r.state in {"active", "requested"} else r.state,
                    ]
                )
                for r in runs
            )
            snapshot["history"] = sorted(await session.scalars(select(SentLot.id)))
            report["history_count"] = len(snapshot["history"])
            store = Store(sessions, registry)
            active, converted, mismatch, errors = 0, 0, 0, 0
            for job in await session.scalars(
                select(Job).where(Job.state.in_(["waiting", "running"]))
            ):
                from vpars_bot.search_control import WorkToken

                # _current checks intent without triggering entitlement reconciliation.
                token = WorkToken(**job.token)
                user = await session.get(User, token.user_id)
                search = await session.get(Search, token.search_id)
                search_run = await session.get(SearchRun, (token.search_id, token.source_id))
                if not store.control._current(user, search, search_run, token):
                    continue
                active += 1
                saved = Filters.model_validate(search.filters)
                if euro_price(saved):
                    try:
                        expected = registry.for_source(saved, token.source_id).price.model_dump(
                            mode="json"
                        )
                    except Exception:
                        errors += 1
                        continue
                    converted += expected["currency"] != "EUR"
                    actual = job.query["filters"].get("price")
                    from vpars_bot.domain import Price

                    mismatch += Price.model_validate(actual) != Price.model_validate(expected)
            report.update(
                active_jobs=active,
                converted_jobs=converted,
                price_mismatches=mismatch,
                rate_errors=errors,
            )
            params = {
                "bot_id": int(config.telegram_bot_token.get_secret_value().split(":", 1)[0])
                % 2147483647
            }
            for name, lock in (("bot_running", 198401), ("worker_running", 198403)):
                free = await session.scalar(
                    text(f"SELECT pg_try_advisory_lock({lock}, :bot_id)"), params
                )
                report[name] = not free
                if free:
                    await session.execute(
                        text(f"SELECT pg_advisory_unlock({lock}, :bot_id)"), params
                    )
        if record:
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            report["snapshot_recorded"] = True
        elif path.exists():
            before = json.loads(path.read_text(encoding="utf-8"))
            report["settings_unchanged"] = all(
                before[k] == snapshot[k] for k in ("User", "Search", "Subscription", "intent")
            )
            report["history_preserved"] = set(before["history"]) <= set(snapshot["history"])
        print(json.dumps(report, indent=2))
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    asyncio.run(run(parser.parse_args().record))
