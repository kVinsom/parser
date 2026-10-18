import json
from datetime import UTC, datetime

import pytest
from conftest import first_card, html
from selectolax.parser import HTMLParser

from tools import compare_latency
from tools.benchmark import BenchmarkConfig
from tools.compare_latency import compare, comparison_stats, measure, reduction
from vpars_parser.models import ParseRequest
from vpars_parser.service import normalize_page
from vpars_parser.sources import vinted


@pytest.mark.parametrize("region", ["UK", "FR", "IT", "DE"])
def test_backend_change_preserves_complete_fixture_result(monkeypatch, region):
    adapter = vinted.VintedAdapter()
    plan = adapter.build_query(ParseRequest(region_id=region), 60)
    lexbor = measure(adapter, html(region), plan)
    monkeypatch.setattr(vinted, "LexborHTMLParser", HTMLParser)
    modest = measure(adapter, html(region), plan)
    assert lexbor["result_sha256"] == modest["result_sha256"]
    assert lexbor["items"] == modest["items"]


@pytest.mark.parametrize("container", ["script", "style", "comment"])
def test_hidden_markup_is_not_interpreted_as_catalog_cards(container):
    card = first_card()
    hidden = card.replace("10001", "99999")
    hidden = (
        "<!--" + hidden + "-->"
        if container == "comment"
        else f"<{container}>" + hidden + f"</{container}>"
    )
    adapter = vinted.VintedAdapter()
    plan = adapter.build_query(ParseRequest(region_id="UK"), 60)
    result = normalize_page(adapter, hidden + card, plan.query, plan, datetime.now(UTC))
    assert [item.source_item_id for item in result[0]] == ["10001"]
    assert result[4] == 1 and result[5] == "complete"


def test_latency_reduction_preserves_regression_and_missing_data():
    assert reduction(20, 15) == 25
    assert reduction(20, 25) == -25
    assert reduction(0, 0) is None
    empty = comparison_stats([])["elapsed_ms"]
    assert empty["before"]["mean_ms"] is None
    assert empty["mean_reduction_percent"] is None


def test_comparison_alternates_versions_and_excludes_warmup():
    config = BenchmarkConfig(regions=("UK",), iterations=2, warmup=1)
    report = {"inputs": {}, "pairs": []}
    compare(config, vinted.VintedAdapter(), vinted.VintedAdapter(), report)
    assert len(report["pairs"]) == 2
    assert report["pairs"][0]["order"] == ["before", "after"]
    assert report["pairs"][1]["order"] == ["after", "before"]
    assert report["summary"]["elapsed_ms"]["before"]["samples"] == 2
    assert report["inputs"]["UK"]["sha256"]


def test_comparison_rejects_changed_output_even_with_same_item_count():
    class Changed(vinted.VintedAdapter):
        def normalize_item(self, record, region, fetched_at):
            item = super().normalize_item(record, region, fetched_at)
            item.title = "Changed title"
            return item

    report = {"inputs": {}, "pairs": []}
    with pytest.raises(ValueError, match="results differ"):
        compare(
            BenchmarkConfig(regions=("UK",), iterations=1, warmup=0),
            vinted.VintedAdapter(),
            Changed(),
            report,
        )
    assert report["pairs"] == []


def test_comparison_rejects_partial_instead_of_timing_as_success():
    adapter = vinted.VintedAdapter()
    plan = adapter.build_query(ParseRequest(region_id="UK"), 60)
    with pytest.raises(ValueError, match="requires complete"):
        measure(adapter, first_card() + '<a href="/items/9999-changed">x</a>', plan)


def test_comparison_cli_loads_snapshot_and_writes_report(monkeypatch, tmp_path):
    monkeypatch.setattr(compare_latency, "new_folder", lambda kind: tmp_path)
    code = compare_latency.main(
        ["--baseline-adapter", vinted.__file__, "--iterations", "1", "--warmup", "0"]
    )
    report = json.loads((tmp_path / "report.json").read_text())
    assert code == 0 and report["state"] == "passed"
    assert len(report["pairs"]) == 4
    assert report["environment"]["packages"]["selectolax"]
    assert report["source_sha256"]["before"] == report["source_sha256"]["after"]
    assert (tmp_path / "report.md").exists()


def test_comparison_cli_records_failure_without_raw_exception(monkeypatch, tmp_path):
    def failed(*args):
        raise RuntimeError("private raw HTML")

    monkeypatch.setattr(compare_latency, "new_folder", lambda kind: tmp_path)
    monkeypatch.setattr(compare_latency, "compare", failed)
    assert compare_latency.main(["--baseline-adapter", vinted.__file__]) == 1
    raw = (tmp_path / "report.json").read_text()
    assert "private raw HTML" not in raw
    assert json.loads(raw)["state"] == "failed"
