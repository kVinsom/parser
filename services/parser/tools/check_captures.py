"""Replay full saved HTML locally. This is explicitly NOT a live Vinted test."""

import argparse
import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vpars_parser.models import ParseRequest  # noqa: E402
from vpars_parser.service import normalize_page  # noqa: E402
from vpars_parser.sources.vinted import VintedAdapter  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_directory", type=Path)
    args = parser.parse_args()
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "kind": "offline_full_capture_replay",
        "results": [],
    }
    for region in ("UK", "FR", "IT", "DE"):
        source = args.capture_directory / f"{region}.html.gz"
        raw = gzip.decompress(source.read_bytes()).decode("utf-8")
        adapter = VintedAdapter()
        plan = adapter.build_query(ParseRequest(region_id=region), 60)
        result = normalize_page(adapter, raw, plan.query, plan, datetime.now(UTC))
        items, pagination, warnings, skipped, seen, complete, parse_ms, normal_ms, *_ = result
        report["results"].append(
            {
                "region": region,
                "capture": source.parent.name,
                "items": len(items),
                "cards_seen": seen,
                "cards_skipped": skipped,
                "completeness": complete,
                "warning_codes": sorted({w.code for w in warnings}),
                "pagination": pagination.model_dump(),
                "parse_ms": parse_ms,
                "normalization_ms": normal_ms,
            }
        )
    folder = ROOT / ".artifacts"
    folder.mkdir(exist_ok=True)
    (folder / "offline-captures.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0 if all(r["completeness"] == "complete" for r in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
