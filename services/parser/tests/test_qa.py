import json
import subprocess

import pytest

from tools import qa
from tools.benchmark import BenchmarkConfig


@pytest.mark.parametrize(
    "checks",
    [
        {
            "passed": False,
            "steps": [{"name": "tests", "exit_code": 1, "elapsed_seconds": 0, "log": "tests.log"}],
        },
        {
            "passed": False,
            "steps": [],
            "tests": {"tests": 5, "failed": 0, "errors": 0, "skipped": 1},
        },
    ],
)
def test_failed_or_skipped_tests_block_all_benchmarks(tmp_path, monkeypatch, checks):
    monkeypatch.setattr(qa, "run_checks", lambda folder: checks)

    def forbidden(*args):
        pytest.fail("Benchmarks must not start after failed checks")

    monkeypatch.setattr(qa, "run_benchmark", forbidden)
    report = qa.run_qa(BenchmarkConfig(mode="live"), tmp_path)
    assert report["state"] == "failed" and "benchmark_state" not in report
    assert json.loads((tmp_path / "report.json").read_text())["state"] == "failed"


def test_checks_stop_after_first_failed_command(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(qa.subprocess, "run", run)
    report = qa.run_checks(tmp_path)
    assert len(calls) == 1 and not report["passed"]
    assert report["steps"][0]["name"] == "contracts"


def test_junit_counts_each_case_once(tmp_path):
    path = tmp_path / "tests.xml"
    path.write_text(
        "<testsuites><testsuite><testcase/><testcase><failure/></testcase>"
        "<testcase><skipped/></testcase><testcase><error/></testcase></testsuite></testsuites>"
    )
    assert qa.junit_counts(path) == {"tests": 4, "failed": 1, "errors": 1, "skipped": 1}


def test_qa_preserves_benchmark_failure_exit_state(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "run_checks", lambda _: {"passed": True, "steps": []})
    monkeypatch.setattr(qa, "run_benchmark", lambda *args: {"state": "failed"})
    report = qa.run_qa(BenchmarkConfig(), tmp_path)
    assert report["state"] == report["benchmark_state"] == "failed"


def test_interruption_writes_non_successful_report(tmp_path, monkeypatch):
    def interrupt(folder):
        raise KeyboardInterrupt

    monkeypatch.setattr(qa, "run_checks", interrupt)
    assert qa.run_qa(BenchmarkConfig(), tmp_path)["state"] == "interrupted"
    assert json.loads((tmp_path / "report.json").read_text())["state"] == "interrupted"
