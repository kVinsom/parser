import argparse
import json
from pathlib import Path

import httpx
import pytest

from tools import benchmark as bench
from vpars_parser.models import ParseRequest
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings


@pytest.mark.parametrize(
    "kwargs",
    [
        {"iterations": 0},
        {"iterations": 1001},
        {"rounds": 6},
        {"rounds": 0},
        {"warmup": -1},
        {"warmup": 21},
        {"regions": ("PL",)},
        {"regions": ()},
        {"pause": float("nan")},
        {"pause": -1},
        {"min_success_rate": 101},
        {"max_local_p95_ms": float("inf")},
        {"max_local_p95_ms": 0},
        {"mode": "offline", "max_live_p95_ms": 10},
        {"browser_timeout": 301},
    ],
)
def test_invalid_benchmark_configuration(kwargs):
    with pytest.raises(ValueError):
        bench.BenchmarkConfig(**kwargs)


def test_gb_alias_deduplicated_without_enabling_pl():
    parser = argparse.ArgumentParser()
    bench.add_arguments(parser)
    config = bench.config_from_args(parser.parse_args(["--regions", "GB", "UK", "FR"]))
    assert config.regions == ("UK", "FR")


def test_nearest_rank_p95_and_missing_measurements():
    assert bench.stats(list(range(1, 21)))["p95_ms"] == 19
    assert bench.stats([9])["median_ms"] == 9
    assert bench.stats([])["p95_ms"] is None
    with pytest.raises(ValueError):
        bench.stats([float("nan")])


def test_failed_partial_and_unattempted_requests_do_not_bias_latency():
    rows = [
        {"status": "complete", "elapsed_ms": 100},
        {"status": "partial", "elapsed_ms": 2},
        {"status": "error", "elapsed_ms": 1, "error_code": "AccessBlocked"},
    ]
    summary = bench.summarize(rows, 4, bench.LIVE_FIELDS, 2)
    assert (
        summary["complete"]
        == summary["failed"]
        == summary["partial"]
        == summary["not_attempted"]
        == 1
    )
    assert summary["success_rate_percent"] == 25
    assert summary["latency_complete_only"]["elapsed_ms"]["mean_ms"] == 100
    assert summary["latency_complete_only"]["access_ms"]["mean_ms"] is None


def test_local_benchmark_real_fixtures_and_reports(tmp_path):
    config = bench.BenchmarkConfig(regions=("UK", "FR"), iterations=2, warmup=1)
    report = bench.run_benchmark(config, tmp_path)
    assert report["state"] == "passed" and "live" not in report
    section = report["offline"]
    assert len(section["samples"]) == 4  # Warmup is excluded.
    assert section["by_region"]["UK"]["complete"] == 2
    assert section["samples"][0]["items"] == 8
    assert section["inputs"]["FR"]["kind"] == "redacted_real_dom_fixture"
    assert json.loads((tmp_path / "report.json").read_text())["state"] == "passed"
    assert "No Vinted requests" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_local_threshold_prevents_live_requests(tmp_path, monkeypatch):
    def never_start(*args, **kwargs):
        pytest.fail("A failed offline benchmark must not contact the live gateway")

    monkeypatch.setattr(bench, "parser_server", never_start)
    report = bench.run_benchmark(
        bench.BenchmarkConfig(
            mode="both",
            regions=("UK",),
            iterations=1,
            warmup=0,
            max_local_p95_ms=0.000001,
        ),
        tmp_path,
    )
    assert report["state"] == "failed"
    assert report["live"]["setup_error"] == "OfflineBenchmarkFailed"
    assert report["live"]["summary"]["not_attempted"] == 3


def test_missing_input_is_a_failed_report_without_exception_text(tmp_path):
    report = bench.run_benchmark(
        bench.BenchmarkConfig(
            capture_directory=Path("secret-path-that-does-not-exist"),
            iterations=1,
        ),
        tmp_path,
    )
    assert report["state"] == "failed"
    assert report["offline"]["summary"]["complete"] == 0
    assert "secret-path" not in (tmp_path / "report.json").read_text()


@pytest.mark.parametrize(
    "error,attempted",
    [
        ("AccessBlocked", 4),
        ("SourceRateLimited", 4),
        ("GatewayTimeout", 1),
        ("GatewayResourceExhausted", 1),
    ],
)
async def test_live_stops_region_or_entire_run_without_retries(error, attempted):
    config = bench.BenchmarkConfig(mode="live", regions=("UK", "FR"), rounds=3, pause=0)
    calls = []
    section = {"samples": []}

    async def fetch(region, iteration):
        calls.append((region, iteration))
        if region == "UK":
            return {"region": region, "status": "error", "error_code": error, "elapsed_ms": 1}
        return {"region": region, "status": "complete", "elapsed_ms": 100}

    await bench.run_live_samples(config, section, fetch, lambda: None)
    assert len(calls) == attempted
    assert calls.count(("UK", 1)) == 1 and ("UK", 2) not in calls
    assert section["criteria"]["passed"] is False
    assert "incomplete_run" in section["criteria"]["failures"]


def test_p95_gate_is_checked_per_region():
    config = bench.BenchmarkConfig(mode="live", regions=("UK", "FR"), rounds=1, max_live_p95_ms=50)
    section = {
        "samples": [
            {"region": "UK", "status": "complete", "elapsed_ms": 1},
            {"region": "FR", "status": "complete", "elapsed_ms": 100},
        ]
    }
    bench.update_section(section, config, True, 1)
    assert section["criteria"]["failures"] == ["p95_threshold:FR"]


async def test_http_benchmark_validates_response_and_records_real_fields(gateway):
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))

    def respond(request):
        request_id = json.loads(request.content)["request_id"]
        body = result.model_dump(mode="json")
        body["request_id"] = request_id
        return httpx.Response(200, json=body, headers={"x-request-id": request_id})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(respond)
    ) as client:
        sample = await bench.http_sample(client, "UK", 1, bench.BenchmarkConfig(mode="live"))
    assert sample["status"] == "complete" and sample["items"] == 8
    assert sample["access_ms"] == result.timings.access_ms and sample["elapsed_ms"] >= 0


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"secret": "untrusted invalid payload"}, "BenchmarkInvalidResponse"),
        (
            502,
            {"request_id": "r", "error": {"code": "AccessBlocked", "message": "secret"}},
            "AccessBlocked",
        ),
    ],
)
async def test_http_benchmark_error_data_is_redacted(status, body, expected):
    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
    ) as client:
        row = await bench.http_sample(client, "UK", 1, bench.BenchmarkConfig(mode="live"))
    assert row["status"] == "error" and row["error_code"] == expected
    assert "secret" not in json.dumps(row)
