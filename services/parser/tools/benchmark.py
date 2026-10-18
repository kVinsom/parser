"""Repeatable local HTML and sequential live HTTP benchmarks with explicit failures."""

import argparse
import asyncio
import gzip
import hashlib
import importlib.metadata
import json
import math
import platform
import statistics
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

from tools.http_server import parser_server  # noqa: E402
from vpars_parser.errors import ParserError  # noqa: E402
from vpars_parser.models import ErrorResponse, Item, ParseRequest, ParseResult  # noqa: E402
from vpars_parser.service import normalize_page  # noqa: E402
from vpars_parser.settings import Settings  # noqa: E402
from vpars_parser.sources.vinted import VintedAdapter  # noqa: E402

REGIONS = ("UK", "FR", "IT", "DE")
ITEMS_JSON = TypeAdapter(list[Item])
MAX_BYTES = 32 * 1024 * 1024
LOCAL_FIELDS = ("elapsed_ms", "parse_ms", "normalization_ms", "serialization_ms")
LIVE_FIELDS = ("elapsed_ms", "access_ms", "parse_ms", "normalization_ms", "parser_total_ms")


@dataclass(frozen=True)
class BenchmarkConfig:
    mode: str = "offline"
    regions: tuple[str, ...] = REGIONS
    iterations: int = 50
    warmup: int = 3
    rounds: int = 3
    pause: float = 0.5
    capture_directory: Path | None = None
    browser_timeout: int = 60
    min_success_rate: float = 100
    max_local_p95_ms: float | None = None
    max_live_p95_ms: float | None = None

    def __post_init__(self):
        if self.mode not in {"offline", "live", "both"}:
            raise ValueError("mode must be offline, live or both")
        if not self.regions or len(set(self.regions)) != len(self.regions):
            raise ValueError("Select distinct supported regions")
        if any(r not in REGIONS for r in self.regions):
            raise ValueError("Supported regions: UK/GB, FR, IT, DE; PL is disabled")
        for name, low, high in (
            ("iterations", 1, 1000),
            ("warmup", 0, 20),
            ("rounds", 1, 5),
            ("browser_timeout", 1, 300),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be {low}..{high}")
        for name, low, high in (("pause", 0, 30), ("min_success_rate", 0, 100)):
            value = getattr(self, name)
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be finite and {low}..{high}")
        for value in (self.max_local_p95_ms, self.max_live_p95_ms):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("p95 thresholds must be finite and positive")
        if self.mode == "live" and self.max_local_p95_ms is not None:
            raise ValueError("Local p95 requires offline or both mode")
        if self.mode == "offline" and self.max_live_p95_ms is not None:
            raise ValueError("Live p95 requires live or both mode")


def stats(values: list[float]) -> dict:
    if not values:
        return {
            "samples": 0,
            "min_ms": None,
            "mean_ms": None,
            "median_ms": None,
            "p95_ms": None,
            "max_ms": None,
        }
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("Measurements must be finite and nonnegative")
    ordered = sorted(values)
    return {
        "samples": len(values),
        "min_ms": ordered[0],
        "mean_ms": statistics.fmean(values),
        "median_ms": statistics.median(values),
        "p95_ms": ordered[math.ceil(0.95 * len(values)) - 1],
        "max_ms": ordered[-1],
    }


def summarize(samples: list[dict], planned: int, fields: tuple[str, ...], wall_seconds: float):
    complete = [s for s in samples if s["status"] == "complete"]
    return {
        "planned": planned,
        "attempted": len(samples),
        "complete": len(complete),
        "partial": sum(s["status"] == "partial" for s in samples),
        "failed": sum(s["status"] == "error" for s in samples),
        "not_attempted": planned - len(samples),
        "success_rate_percent": 100 * len(complete) / planned if planned else 0,
        "wall_seconds": wall_seconds,
        "observed_complete_pages_per_minute": len(complete) * 60 / wall_seconds
        if wall_seconds > 0
        else None,
        "error_counts": dict(Counter(s["error_code"] for s in samples if s.get("error_code"))),
        "latency_complete_only": {
            field: stats([s[field] for s in complete if field in s]) for field in fields
        },
    }


def evaluate(section: dict, config: BenchmarkConfig, threshold: float | None) -> dict:
    failures = []
    if section.get("setup_error"):
        failures.append("setup_failed")
    summary = section["summary"]
    if summary["not_attempted"]:
        failures.append("incomplete_run")
    if not summary["complete"]:
        failures.append("no_complete_samples")
    if summary["success_rate_percent"] < config.min_success_rate:
        failures.append("success_rate_below_threshold")
    if threshold is not None:
        # Per-region gates prevent a fast region from hiding a slower one.
        for region in config.regions:
            value = section["by_region"][region]["latency_complete_only"]["elapsed_ms"]["p95_ms"]
            if value is None or value > threshold:
                failures.append(f"p95_threshold:{region}")
    return {"passed": not failures, "failures": failures}


def update_section(section: dict, config: BenchmarkConfig, live: bool, elapsed: float):
    count = config.rounds if live else config.iterations
    fields = LIVE_FIELDS if live else LOCAL_FIELDS
    section["summary"] = summarize(section["samples"], count * len(config.regions), fields, elapsed)
    section["by_region"] = {
        region: summarize(
            [s for s in section["samples"] if s["region"] == region], count, fields, 0
        )
        for region in config.regions
    }
    section["criteria"] = evaluate(
        section, config, config.max_live_p95_ms if live else config.max_local_p95_ms
    )


def write_report(folder: Path, report: dict):
    """Replace each report atomically; retain the previous usable file until replacement."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Parser benchmark",
        "",
        f"State: **{report['state']}**",
        "",
        f"Python: {report['environment']['python']}; {report['environment']['platform']}",
        "",
        "Latency: complete pages only; p95 = nearest rank ceil(0.95 × N).",
        "Missing/failed samples never become zero-millisecond successes.",
        "",
    ]
    for key in ("offline", "live"):
        if key not in report:
            continue
        section = report[key]
        lines.extend([f"## {key}", "", section["boundary"], ""])
        if section.get("setup_error"):
            lines.extend([f"Setup error: {section['setup_error']}", ""])
        if "summary" not in section:
            lines.append("Pending.\n")
            continue
        total = section["summary"]
        lines.extend(
            [
                f"Complete: {total['complete']}/{total['planned']}; partial: {total['partial']}; "
                f"failed: {total['failed']}; not attempted: {total['not_attempted']}.",
                "",
                "| Region | Complete/planned | Mean, ms | Median, ms | p95, ms | Max, ms |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for region, data in section["by_region"].items():
            latency = data["latency_complete_only"]["elapsed_ms"]
            numbers = [
                "—" if latency[k] is None else f"{latency[k]:.3f}"
                for k in ("mean_ms", "median_ms", "p95_ms", "max_ms")
            ]
            lines.append(
                f"| {region} | {data['complete']}/{data['planned']} | " + " | ".join(numbers) + " |"
            )
        lines.extend(["", f"Criteria: {section['criteria']}", ""])
    lines.extend(
        [
            "Stage timings and individual samples are in report.json.",
            "Live measurements are sequential observations, not maximum capacity or 24/7 evidence.",
            "",
        ]
    )
    for name, content in (
        ("report.json", json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"),
        ("report.md", "\n".join(lines)),
    ):
        temporary = folder / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(folder / name)


def new_folder(kind: str = "benchmark") -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / ".artifacts" / f"{kind}-{stamp}-{uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    return folder


def load_input(region: str, config: BenchmarkConfig):
    source = (
        config.capture_directory / f"{region}.html.gz"
        if config.capture_directory
        else ROOT / "tests" / "fixtures" / f"{region}.html"
    )
    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Capture exceeds the parser HTML limit")
    return raw.decode("utf-8"), {
        "file": source.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "html_bytes": len(raw),
        "kind": "full_saved_html" if config.capture_directory else "redacted_real_dom_fixture",
    }


def local_sample(html: str, region: str, iteration: int) -> dict:
    adapter = VintedAdapter()
    plan = adapter.build_query(ParseRequest(region_id=region), 60)
    fetched = datetime.now(UTC)
    started = time.perf_counter()
    try:
        items, _, warnings, skipped, seen, completeness, parse_ms, normal_ms, *_ = normalize_page(
            adapter, html, plan.query, plan, fetched
        )
        serialize_start = time.perf_counter()
        encoded = ITEMS_JSON.dump_json(items)
        serialize_ms = (time.perf_counter() - serialize_start) * 1000
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "region": region,
            "iteration": iteration,
            "status": completeness,
            "items": len(items),
            "cards_seen": seen,
            "cards_skipped": skipped,
            "json_bytes": len(encoded),
            "warning_codes": sorted({w.code for w in warnings}),
            "elapsed_ms": elapsed_ms,
            "parse_ms": parse_ms,
            "normalization_ms": normal_ms,
            "serialization_ms": serialize_ms,
        }
    except ParserError as exc:
        return {
            "region": region,
            "iteration": iteration,
            "status": "error",
            "error_code": exc.detail.code,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }


def run_offline(config: BenchmarkConfig, section: dict, checkpoint: Callable[[], None]):
    inputs = {}
    try:
        for region in config.regions:
            html, metadata = load_input(region, config)
            inputs[region] = html
            section["inputs"][region] = metadata
            for _ in range(config.warmup):
                warm = local_sample(html, region, 0)
                if warm["status"] != "complete":
                    section["setup_error"] = "WarmupDidNotProduceCompletePage"
                    return
    except (OSError, ValueError):
        section["setup_error"] = "CaptureUnavailableOrInvalid"
        return
    started = time.perf_counter()
    try:
        for iteration in range(1, config.iterations + 1):
            for region in config.regions:
                section["samples"].append(local_sample(inputs[region], region, iteration))
            update_section(section, config, False, time.perf_counter() - started)
            checkpoint()
    finally:
        update_section(section, config, False, time.perf_counter() - started)


async def http_sample(
    client: httpx.AsyncClient, region: str, iteration: int, config: BenchmarkConfig
):
    request = ParseRequest(
        region_id=region,
        timeout_seconds=config.browser_timeout,
        request_id=f"benchmark-{region}-{iteration}-{uuid4().hex[:8]}",
    )
    started = time.perf_counter()
    row = {"region": region, "iteration": iteration, "status": "error"}
    try:
        response = await client.post("/v1/catalog/parse", json=request.model_dump(mode="json"))
        row["http_status"] = response.status_code
        if response.status_code == 200:
            result = ParseResult.model_validate_json(response.content)
            if (
                result.region_id != region
                or result.source_id != "vinted"
                or not result.items
                or result.request_id != request.request_id
                or response.headers.get("x-request-id") != request.request_id
            ):
                raise ValueError("Unexpected parser response identity")
            row.update(
                status=result.completeness,
                items=len(result.items),
                cards_skipped=result.cards_skipped,
                html_bytes=result.html_bytes,
                access_ms=result.timings.access_ms,
                parse_ms=result.timings.parse_ms,
                normalization_ms=result.timings.normalization_ms,
                parser_total_ms=result.timings.total_ms,
                warning_codes=sorted({w.code for w in result.warnings}),
            )
        else:
            error = ErrorResponse.model_validate_json(response.content).error
            row.update(error_code=error.code, source_status=error.source_status)
    except httpx.TimeoutException:
        row["error_code"] = "BenchmarkHTTPTimeout"
    except httpx.TransportError:
        row["error_code"] = "BenchmarkHTTPUnavailable"
    except ValueError:
        row["error_code"] = "BenchmarkInvalidResponse"
    row["elapsed_ms"] = (time.perf_counter() - started) * 1000
    return row


async def run_live_samples(
    config: BenchmarkConfig,
    section: dict,
    fetch: Callable[[str, int], Awaitable[dict]],
    checkpoint: Callable[[], None],
):
    stopped_regions = set()
    started = time.perf_counter()
    attempted = 0
    try:
        for iteration in range(1, config.rounds + 1):
            for region in config.regions:
                if region in stopped_regions:
                    continue
                if attempted and config.pause:
                    await asyncio.sleep(config.pause)
                row = await fetch(region, iteration)
                attempted += 1
                section["samples"].append(row)
                update_section(section, config, True, time.perf_counter() - started)
                checkpoint()
                if row["status"] != "complete":
                    # No immediate retries. Access problems stop this region;
                    # uncertain transport/capacity failures stop the entire live run.
                    if (
                        row.get("error_code")
                        in {
                            "AccessBlocked",
                            "SourceRateLimited",
                            "SchemaChanged",
                            "UnexpectedContent",
                            "UnsupportedRegion",
                        }
                        or row["status"] == "partial"
                    ):
                        stopped_regions.add(region)
                    else:
                        return
    finally:
        update_section(section, config, True, time.perf_counter() - started)


async def run_live_http(url: str, config: BenchmarkConfig, section: dict, checkpoint):
    settings = Settings.from_env()
    deadline = (
        config.browser_timeout
        + settings.rpc_grace
        + settings.control_timeout
        + settings.parse_timeout
        + 10
    )
    async with httpx.AsyncClient(base_url=url, timeout=deadline, trust_env=False) as client:
        ready = await client.get("/health/ready")
        if ready.status_code != 200:
            section["setup_error"] = "GatewayNotReady"
            return
        await run_live_samples(
            config, section, lambda r, i: http_sample(client, r, i, config), checkpoint
        )


def run_benchmark(config: BenchmarkConfig, folder: Path) -> dict:
    report = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "state": "running",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("pydantic", "selectolax", "grpcio", "fastapi", "httpx")
            },
        },
        "config": {
            "mode": config.mode,
            "regions": list(config.regions),
            "iterations": config.iterations,
            "warmup": config.warmup,
            "live_rounds": config.rounds,
            "live_pause_seconds": config.pause,
            "browser_timeout_seconds": config.browser_timeout,
            "min_success_rate": config.min_success_rate,
            "max_local_p95_ms": config.max_local_p95_ms,
            "max_live_p95_ms": config.max_live_p95_ms,
        },
    }

    def checkpoint():
        write_report(folder, report)

    checkpoint()
    try:
        if config.mode in {"offline", "both"}:
            section = report["offline"] = {
                "samples": [],
                "inputs": {},
                "boundary": (
                    "Local parse + normalize + items JSON; disk/gzip/input decoding "
                    "and warmup excluded. No Vinted requests."
                ),
            }
            run_offline(config, section, checkpoint)
            if "summary" not in section:
                update_section(section, config, False, 0)
            checkpoint()
        if config.mode in {"live", "both"}:
            section = report["live"] = {
                "samples": [],
                "boundary": (
                    "HTTP client → temporary Uvicorn → parser → existing gRPC gateway → Vinted. "
                    "First catalog request included; no warmup requests/retries."
                ),
            }
            # A failed local baseline must not be followed by live requests in combined mode.
            if report.get("offline", {}).get("criteria", {}).get("passed") is False:
                section["setup_error"] = "OfflineBenchmarkFailed"
            else:
                try:
                    with parser_server(folder / "server.log", {"PARSER_MAX_INFLIGHT": "1"}) as url:
                        asyncio.run(run_live_http(url, config, section, checkpoint))
                except (OSError, RuntimeError, httpx.HTTPError):
                    section["setup_error"] = "LiveEnvironmentUnavailable"
            if "summary" not in section:
                update_section(section, config, True, 0)
            else:
                section["criteria"] = evaluate(section, config, config.max_live_p95_ms)
        report["state"] = (
            "passed"
            if all(report[k]["criteria"]["passed"] for k in ("offline", "live") if k in report)
            else "failed"
        )
    except KeyboardInterrupt:
        report["state"] = "interrupted"
    except Exception as exc:
        report["state"] = "error"
        report["error_type"] = type(exc).__name__  # Never persist raw exception text/HTML/secrets.
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        checkpoint()
    return report


def add_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--mode", choices=("offline", "live", "both"), default="offline")
    parser.add_argument("--regions", nargs="+", default=list(REGIONS))
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--capture-directory", type=Path)
    parser.add_argument("--browser-timeout", type=int, default=60)
    parser.add_argument("--min-success-rate", type=float, default=100)
    parser.add_argument("--max-local-p95-ms", type=float)
    parser.add_argument("--max-live-p95-ms", type=float)


def config_from_args(args) -> BenchmarkConfig:
    regions = tuple(dict.fromkeys("UK" if r.upper() == "GB" else r.upper() for r in args.regions))
    return BenchmarkConfig(
        mode=args.mode,
        regions=regions,
        iterations=args.iterations,
        warmup=args.warmup,
        rounds=args.rounds,
        pause=args.pause,
        capture_directory=args.capture_directory,
        browser_timeout=args.browser_timeout,
        min_success_rate=args.min_success_rate,
        max_local_p95_ms=args.max_local_p95_ms,
        max_live_p95_ms=args.max_live_p95_ms,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args()
    try:
        config = config_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    folder = new_folder()
    report = run_benchmark(config, folder)
    print(f"Benchmark {report['state']}: {folder / 'report.md'}")
    return 0 if report["state"] == "passed" else 130 if report["state"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
