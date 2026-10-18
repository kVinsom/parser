"""Read-only operational snapshot; no tokens, filter text or personal chat contents."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT),
    *(str(ROOT.parent / name) for name in ("telegram-bot", "parser", "notifier")),
]
from sqlalchemy import text  # noqa: E402
from vpars_bot.db import database  # noqa: E402

from vpars_worker.runtime import settings  # noqa: E402


async def snapshot(output):
    config = settings()
    engine, sessions = database(config.database_url.get_secret_value())
    try:
        async with sessions() as session:
            queries = {
                "runs": "SELECT s.user_id, r.search_id, r.source_id, r.state, r.generation, r.last_error "
                "FROM bot_search_sources r JOIN bot_searches s ON s.id=r.search_id "
                "WHERE NOT s.archived ORDER BY r.source_id, r.search_id",
                "cache": "SELECT count(*) AS confirmed FROM vpars_monitor.sent_lots",
                "revision": "SELECT id FROM vpars_monitor.schema_revision",
                "jobs": "SELECT query->>'region_id' AS region, state, checks, last_error, "
                "last_checked, next_due, coverage FROM vpars_monitor.catalog_jobs "
                "WHERE state IN ('waiting','running') ORDER BY query->>'region_id'",
                "recent_deliveries": "SELECT state, last_error, count(*) AS count "
                "FROM vpars_monitor.deliveries WHERE created_at > now()-interval '10 minutes' "
                "GROUP BY state,last_error ORDER BY count(*) DESC",
                "pending": "SELECT count(*) AS count, min(created_at) AS oldest "
                "FROM vpars_monitor.deliveries WHERE state IN ('pending','sending')",
                "all_deliveries": "SELECT state,last_error,count(*) AS count "
                "FROM vpars_monitor.deliveries GROUP BY state,last_error ORDER BY count(*) DESC",
                "unique_observed": "SELECT count(DISTINCT item_id) AS source_ids, "
                "count(*) AS job_item_pairs FROM vpars_monitor.seen_items",
                "recent_sent": "SELECT sent_at, item->>'region_id' AS region, "
                "round(extract(epoch from sent_at-created_at)::numeric,3) AS queue_seconds, "
                "media_progress->>'publication_evidence' AS age_evidence "
                "FROM vpars_monitor.deliveries WHERE state='sent' ORDER BY sent_at DESC LIMIT 5",
            }
            report = {
                name: [dict(row) for row in (await session.execute(text(query))).mappings()]
                for name, query in queries.items()
            }
            if any(row["id"] >= 5 for row in report["revision"]):
                for name, query in {
                    "catchup": "SELECT id, query->>'region_id' AS region, page, coverage, "
                    "checkpoint, scan_state AS unfinished FROM vpars_monitor.catalog_jobs "
                    "WHERE state IN ('waiting','running')",
                    "collection_decisions": "SELECT decision->>'code' AS reason, count(*) AS count "
                    "FROM vpars_monitor.seen_items GROUP BY decision->>'code'",
                    "collection_stages": "SELECT count(*) AS observed_job_item_pairs, "
                    "count(*) FILTER (WHERE item IS NOT NULL) AS parsed_ever, "
                    "count(*) FILTER (WHERE decision->>'filter_match'='true') AS filter_match, "
                    "count(*) FILTER (WHERE decision->>'novel_eligible'='true') AS novel_eligible, "
                    "count(*) FILTER (WHERE decision->>'delivery_id' IS NOT NULL) AS linked_delivery, "
                    "count(*) FILTER (WHERE decision IS NULL) AS legacy_unknown_decision "
                    "FROM vpars_monitor.seen_items",
                    "page_audit": "SELECT count(*) AS stored_responses, "
                    "count(*) FILTER (WHERE result->>'completeness'='partial') AS partial "
                    ", count(*) FILTER (WHERE result->>'error' IS NOT NULL) AS failures "
                    "FROM vpars_monitor.catalog_pages",
                }.items():
                    report[name] = [
                        dict(row) for row in (await session.execute(text(query))).mappings()
                    ]
            bot_id = int(config.telegram_bot_token.get_secret_value().split(":", 1)[0]) % 2147483647
            for name, lock in (("bot_running", 198401), ("worker_running", 198403)):
                report[name] = bool(
                    await session.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                            "AND classid=:classid AND objid=:objid AND granted)"
                        ),
                        {"classid": lock, "objid": bot_id},
                    )
                )
        encoded = json.dumps(report, indent=2, default=str)
        if output:
            Path(output).write_text(encoded, encoding="utf-8")
        print(encoded)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    asyncio.run(snapshot(parser.parse_args().output))
