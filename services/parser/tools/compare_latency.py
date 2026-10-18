"""Offline AB/BA comparison of a trusted, saved adapter and the current adapter."""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.benchmark import (  # noqa: E402
    ITEMS_JSON,
    LOCAL_FIELDS,
    BenchmarkConfig,
    load_input,
    new_folder,
    stats,
)
from vpars_parser.models import ParseRequest  # noqa: E402
from vpars_parser.service import normalize_page  # noqa: E402
from vpars_parser.sources.vinted import VintedAdapter  # noqa: E402

FETCHED_AT = datetime(2026, 9, 22, tzinfo=UTC)


def reduction(before: float, after: float) -> float | None:
    """Positive means less latency; preserve regressions as negative percentages."""
    return (before - after) / before * 100 if before > 0 else None


def load_baseline(path: Path):
    # Only an explicitly selected, trusted local Python snapshot may be loaded.
    name = "vpars_parser.sources._latency_baseline"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Invalid adapter snapshot")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolves the owning module during import.
    spec.loader.exec_module(module)
    return module.VintedAdapter()


def measure(adapter, html: str, plan):
    started = time.perf_counter()
    result = normalize_page(adapter, html, plan.query, plan, FETCHED_AT)
    items, pagination, warnings, skipped, seen, completeness, parse_ms, normal_ms, *_ = result
    serialize_start = time.perf_counter()
    encoded = ITEMS_JSON.dump_json(items)
    serialize_ms = (time.perf_counter() - serialize_start) * 1000
    elapsed_ms = (time.perf_counter() - started) * 1000
    if completeness != "complete":
        raise ValueError("Comparison requires complete catalog results")
    # Identity checks are outside the measured boundary. Fixed fetched_at makes
    # every field comparable; reports contain hashes, never raw catalog data.
    fingerprint = hashlib.sha256(encoded)
    fingerprint.update(pagination.model_dump_json().encode())
    for warning in warnings:
        fingerprint.update(warning.model_dump_json().encode())
    fingerprint.update(json.dumps([skipped, seen, completeness]).encode())
    return {
        "elapsed_ms": elapsed_ms,
        "parse_ms": parse_ms,
        "normalization_ms": normal_ms,
        "serialization_ms": serialize_ms,
        "items": len(items),
        "result_sha256": fingerprint.hexdigest(),
    }


def comparison_stats(pairs: list[dict]) -> dict:
    result = {}
    for field in LOCAL_FIELDS:
        before = stats([p["before"][field] for p in pairs])
        after = stats([p["after"][field] for p in pairs])
        result[field] = {
            "before": before,
            "after": after,
            "mean_reduction_percent": reduction(before["mean_ms"], after["mean_ms"])
            if pairs
            else None,
            "p95_reduction_percent": reduction(before["p95_ms"], after["p95_ms"])
            if pairs
            else None,
        }
    return result


def compare(config: BenchmarkConfig, baseline, candidate, report: dict):
    inputs = {}
    plans = {}
    for region in config.regions:
        inputs[region], report["inputs"][region] = load_input(region, config)
        request = ParseRequest(region_id=region)
        plans[region] = candidate.build_query(request, 60)
        if baseline.build_query(request, 60) != plans[region]:
            raise ValueError("Adapter query contracts differ")
    for iteration in range(-config.warmup, config.iterations):
        for region in config.regions:
            # Reverse the order each round to reduce cache/temperature/order bias.
            order = ("before", "after") if iteration % 2 == 0 else ("after", "before")
            adapters = {"before": baseline, "after": candidate}
            pair = {"region": region, "iteration": iteration + 1, "order": list(order)}
            for label in order:
                pair[label] = measure(adapters[label], inputs[region], plans[region])
            if pair["before"]["result_sha256"] != pair["after"]["result_sha256"]:
                raise ValueError("Adapter results differ")
            if iteration >= 0:
                report["pairs"].append(pair)
    report["summary"] = comparison_stats(report["pairs"])
    report["by_region"] = {
        region: comparison_stats([p for p in report["pairs"] if p["region"] == region])
        for region in config.regions
    }


def write_comparison(folder: Path, report: dict):
    lines = [
        "# Parser latency comparison",
        "",
        f"State: **{report['state']}**",
        "",
        "Offline parse + normalize + items JSON, same saved HTML, alternating AB/BA.",
        "Every pair must have identical items, pagination, warnings and completeness.",
        "Warmup, disk/gzip/input decoding and equivalence checks are outside timings.",
        "Positive reduction = (before - after) / before × 100; negative = regression.",
        "No Vinted requests; this percentage is not the live HTTP speedup.",
        "",
        "| Region | Pairs | Mean before, ms | Mean after, ms | Reduction |",
        "|---|---:|---:|---:|---:|",
    ]
    rows = {**report.get("by_region", {}), "ALL": report.get("summary", {})}
    for region, values in rows.items():
        if "elapsed_ms" not in values:
            continue
        row = values["elapsed_ms"]
        lines.append(
            f"| {region} | {row['before']['samples']} | {row['before']['mean_ms']:.3f} | "
            f"{row['after']['mean_ms']:.3f} | {row['mean_reduction_percent']:.2f}% |"
        )
    if report.get("error"):
        lines.extend(["", f"Error: {report['error']}"])
    for filename, content in (
        ("report.md", "\n".join(lines) + "\n"),
        ("report.json", json.dumps(report, indent=2, allow_nan=False) + "\n"),
    ):
        temporary = folder / (filename + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(folder / filename)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-adapter", type=Path, required=True)
    parser.add_argument("--capture-directory", type=Path)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        config = BenchmarkConfig(
            capture_directory=args.capture_directory,
            iterations=args.iterations,
            warmup=args.warmup,
        )
        baseline = load_baseline(args.baseline_adapter)
    except (OSError, ValueError, ImportError, AttributeError):
        parser.error("Invalid comparison configuration or baseline adapter snapshot")
    folder = new_folder("latency-comparison")
    report = {
        "state": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": {
                name: importlib.metadata.version(name) for name in ("pydantic", "selectolax")
            },
        },
        "iterations_per_region": config.iterations,
        "warmup_per_region": config.warmup,
        "source_sha256": {
            "before": hashlib.sha256(args.baseline_adapter.read_bytes()).hexdigest(),
            "after": hashlib.sha256(
                (ROOT / "vpars_parser/sources/vinted.py").read_bytes()
            ).hexdigest(),
            "shared_service": hashlib.sha256(
                (ROOT / "vpars_parser/service.py").read_bytes()
            ).hexdigest(),
        },
        "inputs": {},
        "pairs": [],
    }
    write_comparison(folder, report)
    code = 1
    try:
        compare(config, baseline, VintedAdapter(), report)
        report["state"] = "passed"
        code = 0
    except Exception as exc:
        report.update(state="failed", error=type(exc).__name__)
    except KeyboardInterrupt:
        report["state"] = "interrupted"
        code = 130
    finally:
        write_comparison(folder, report)
    print(f"Comparison {report['state']}: {folder / 'report.md'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
