"""Read-only latency snapshot with no search text, chat IDs or credentials."""

import argparse
import asyncio
import json
import math
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), *(str(ROOT.parent / s) for s in ("telegram-bot", "parser", "notifier"))]
from sqlalchemy import select, text  # noqa: E402
from vpars_bot.db import database  # noqa: E402

from vpars_worker.catchup import scope_key  # noqa: E402
from vpars_worker.db import Job  # noqa: E402
from vpars_worker.runtime import settings  # noqa: E402


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "p50_ms": round(values[math.ceil(len(values) * 0.5) - 1], 1),
        "p95_ms": round(values[math.ceil(len(values) * 0.95) - 1], 1),
        "max_ms": round(values[-1], 1),
    }


async def measure(output, minutes):
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=minutes)
    # Logs use the machine's local timezone; convert before filtering.
    values = defaultdict(list)
    failures = defaultdict(int)
    for line in (ROOT / ".artifacts/monitor-stderr.log").read_text(encoding="utf-8").splitlines():
        try:
            stamp = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").astimezone(UTC)
        except ValueError:
            continue
        if stamp < cutoff:
            continue
        for field in ("gap_ms", "region_gap_ms", "head_gap_ms", "queue_to_sent_ms", "transport_ms"):
            if match := re.search(r"\b" + field + r"=([\d.]+)", line):
                value = float(match[1])
                values[field].append(value)
                if field == "queue_to_sent_ms" and stamp - timedelta(milliseconds=value) >= cutoff:
                    values["new_cohort_queue_ms"].append(value)
        if match := re.search(r"shared_jobs=(\d+) cache=(True|False)", line):
            values["shared_jobs"].append(int(match[1]))
            values["cache_hits"].append(int(match[2] == "True"))
        if match := re.search(r"Checked region=.*elapsed_ms=([\d.]+)", line):
            values["catalog_ms"].append(float(match[1]))
        if match := re.search(r"Lot details loaded.*elapsed_ms=([\d.]+)", line):
            values["details_ms"].append(float(match[1]))
        if match := re.search(r"Catalog check failed region=\w+ \((\w+),", line):
            failures[match[1]] += 1
    report = {
        "at": now.isoformat(),
        "window_minutes": minutes,
        "timings": {name: distribution(v) for name, v in values.items() if name not in {"shared_jobs", "cache_hits"}},
        "shared_job_observations": sum(values["shared_jobs"]),
        "cache_hits": sum(values["cache_hits"]),
        "catalog_failures": dict(failures),
    }
    engine, sessions = database(settings().database_url.get_secret_value())
    try:
        async with sessions() as session:
            # Attempts can be frequent while responses fail. Measure successful
            # complete inventories separately, deduplicating shared local filters.
            gaps = await session.scalars(text("""
                WITH responses AS (
                    SELECT scope, result->>'request_id' AS request_id,
                           min(observed_at) AS checked
                    FROM vpars_monitor.catalog_pages
                    WHERE page=1 AND result->>'completeness'='complete'
                      AND result->>'identities_complete'='true'
                      AND observed_at >= CAST(:cutoff AS timestamptz) - interval '5 minutes'
                    GROUP BY scope, result->>'request_id'
                ), gaps AS (
                    SELECT checked, extract(epoch FROM checked - lag(checked)
                        OVER (PARTITION BY scope ORDER BY checked))*1000 AS gap
                    FROM responses
                ) SELECT gap FROM gaps WHERE checked>=:cutoff AND gap IS NOT NULL
            """), {"cutoff": cutoff})
            report["timings"]["successful_head_gap_ms"] = distribution([float(g) for g in gaps])
            jobs = list(
                await session.scalars(select(Job).where(Job.state.in_(["waiting", "running"])))
            )
            groups = defaultdict(list)
            for job in jobs:
                groups[scope_key(job.query)].append(job)
            report["upstream_queries"] = [
                {
                    "region": group[0].query["region_id"],
                    "jobs": len(group),
                    "has_search_text": bool(group[0].query.get("search_text")),
                    "pages": [job.page for job in group],
                    "local_filter_variants": len(
                        {json.dumps(job.query.get("filters"), sort_keys=True) for job in group}
                    ),
                }
                for group in groups.values()
            ]
            for name, query in {
                "queue": "SELECT state, count(*), min(created_at) AS oldest FROM vpars_monitor.deliveries "
                "WHERE state IN ('pending','sending') GROUP BY state",
                "recent_delivery_states": "SELECT state, last_error, count(*) FROM vpars_monitor.deliveries "
                "WHERE created_at >= :cutoff GROUP BY state, last_error ORDER BY count(*) DESC",
                "recent_deadlines": "SELECT count(*) FILTER (WHERE state='sent' AND sent_at-created_at <= interval '60 seconds') AS sent_within_minute, "
                "count(*) FILTER (WHERE state='sent' AND sent_at-created_at > interval '60 seconds') AS sent_after_minute, "
                "count(*) FILTER (WHERE state IN ('pending','sending') AND now()-created_at > interval '60 seconds') AS overdue_pending, "
                "count(DISTINCT (user_id,item->>'source_id',item->>'source_item_id')) FILTER (WHERE state IN ('pending','sending')) AS pending_unique_recipient_lots "
                "FROM vpars_monitor.deliveries WHERE created_at >= :cutoff",
            }.items():
                report[name] = [
                    dict(r)
                    for r in (await session.execute(text(query), {"cutoff": cutoff})).mappings()
                ]
    finally:
        await engine.dispose()
    encoded = json.dumps(report, indent=2, default=str)
    Path(output).write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--minutes", type=float, default=10)
    args = parser.parse_args()
    asyncio.run(measure(args.output, args.minutes))
