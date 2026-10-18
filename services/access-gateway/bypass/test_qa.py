"""Benchmark arithmetic, stopping rules and automation exit codes, without network."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import qa
import vinted
from vinted_regions import get_region, select_regions


def attempt(code, outcome="page_loaded", seconds=2):
    return vinted.CatalogResult(code, get_region(code).catalog_url(), outcome=outcome,
                               item_count=95 if outcome == "page_loaded" else 0, elapsed_seconds=seconds)


class MetricsTests(unittest.TestCase):
    def test_nearest_rank_and_empty_sample(self):
        self.assertEqual(qa.distribution(list(range(1, 21)))["p95"], 19)
        self.assertEqual(qa.distribution([7, 3, 2])["p95"], 7)
        self.assertIsNone(qa.distribution([])["median"])

    def test_failure_latency_does_not_pollute_success_latency(self):
        summary = qa.summarize([attempt("UK", seconds=2).summary(),
                                attempt("UK", seconds=4).summary(),
                                attempt("UK", "timeout", 70).summary()], 5)
        self.assertEqual(summary["success_percent"], 66.67)
        self.assertEqual(summary["latency_seconds"]["p95"], 70)
        self.assertEqual(summary["successful_latency_seconds"]["p95"], 4)
        self.assertEqual(summary["successful_latency_seconds"]["median"], 3)
        self.assertEqual((summary["planned"], summary["attempted"]), (5, 3))

    def test_no_attempts_are_not_a_success(self):
        summary = qa.summarize([], 5)
        self.assertIsNone(summary["success_percent"])
        checks = qa.quality_checks({"summary": summary, "status": "incomplete", "parser": []}, 0, 20)
        self.assertTrue(all(not check["passed"] for check in checks))

    def test_latency_threshold_and_incomplete_run_fail(self):
        benchmark = {"summary": qa.summarize([attempt("UK", seconds=11).summary()], 1),
                     "status": "completed", "parser": [{"consistent": True}]}
        self.assertFalse(qa.quality_checks(benchmark, 100, 10)[-1]["passed"])
        self.assertTrue(all(check["passed"] for check in qa.quality_checks(benchmark, 100, 12)))
        benchmark["status"] = "interrupted"
        self.assertFalse(qa.quality_checks(benchmark, 100, 12)[0]["passed"])


class AutomationTests(unittest.TestCase):
    def setUp(self):
        (qa.ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=qa.ROOT / ".tmp")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def run_benchmark(self, regions, fetch, update=lambda value: None):
        with contextlib.redirect_stdout(io.StringIO()):
            return qa.run_benchmark(regions, rounds=2, timeout=10, pause=0, endpoint="http://localhost:8191",
                                    folder=self.folder, update=update, fetch=fetch, sleep=lambda seconds: None)

    def test_each_round_uses_only_the_four_enabled_regions(self):
        fetch = Mock(side_effect=lambda region, **kwargs: attempt(region.code))
        result = self.run_benchmark(select_regions(["all"]), fetch)
        self.assertEqual([call.args[0].code for call in fetch.call_args_list],
                         ["UK", "FR", "IT", "DE"] * 2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["successful"], 8)

    def test_blocked_region_is_not_retried_but_other_region_continues(self):
        fetch = Mock(side_effect=[attempt("FR", "blocked"), attempt("UK"), attempt("UK")])
        result = self.run_benchmark(select_regions(["FR", "UK"]), fetch)
        self.assertEqual([call.args[0].code for call in fetch.call_args_list], ["FR", "UK", "UK"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["summary"]["planned"], 4)
        self.assertEqual(result["stopped_regions"], {"FR": "blocked"})

    def test_service_failure_stops_the_benchmark(self):
        fetch = Mock(return_value=attempt("UK", "service_error"))
        result = self.run_benchmark(select_regions(["all"]), fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["status"], "stopped_service_error")

    def test_interrupt_preserves_partial_observations(self):
        checkpoints = []
        fetch = Mock(side_effect=[attempt("UK"), KeyboardInterrupt])
        with self.assertRaises(KeyboardInterrupt):
            self.run_benchmark(select_regions(["UK", "FR"]), fetch,
                               lambda value: checkpoints.append(copy.deepcopy(value)))
        self.assertEqual(checkpoints[-1]["status"], "interrupted")
        self.assertEqual(checkpoints[-1]["summary"]["attempted"], 1)

    def test_failed_unit_suite_prevents_live_requests_and_returns_failure(self):
        tests = {"status": "failed", "total": 1, "failures": 1, "errors": 0, "skipped": 0, "elapsed_seconds": 0.1}
        with patch.object(qa, "ROOT", self.folder), patch.object(qa, "run_tests", return_value=tests), \
                patch.object(qa, "run_benchmark") as benchmark, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(qa.main([]), 1)
        benchmark.assert_not_called()
        report = json.loads(next(self.folder.glob(".artifacts/*/report.json")).read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertIn("finished_at", report)

    def test_offline_mode_does_not_call_the_browser_service(self):
        tests = {"status": "passed", "total": 1, "failures": 0, "errors": 0, "skipped": 0, "elapsed_seconds": 0.1}
        with patch.object(qa, "ROOT", self.folder), patch.object(qa, "run_tests", return_value=tests), \
                patch.object(qa, "run_benchmark") as benchmark, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(qa.main(["--offline"]), 0)
        benchmark.assert_not_called()

    def test_invalid_benchmark_arguments_are_rejected(self):
        for arguments in (["--rounds", "0"], ["--rounds", "11"], ["--regions", "AU"],
                          ["--pause", "nan"], ["--max-p95", "inf"], ["--min-success-rate", "101"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    qa.parse_args(arguments)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
