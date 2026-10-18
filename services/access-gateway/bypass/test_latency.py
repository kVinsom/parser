"""Timing availability, nested durations and diagnostic isolation."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from byparr_runtime import (
    REQUEST_TIMINGS,
    install_navigation_timing,
    navigation_timings,
)
from latency import measured_row, replace_report, summaries
from test_vinted import CATALOG, mock_requests
from vinted import fetch_catalog
from vinted_rpc import access_pb2 as pb


class TimingTests(unittest.TestCase):
    def test_temporarily_open_report_is_retried_without_repeating_measurement(self):
        temporary = Mock()
        temporary.replace.side_effect = [PermissionError(), PermissionError(), None]
        with patch("latency.time.sleep") as sleep:
            replace_report(temporary, "report.json")
        self.assertEqual(temporary.replace.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        temporary.replace.side_effect = PermissionError()
        with patch("latency.time.sleep"), self.assertRaises(PermissionError):
            replace_report(temporary, "report.json")

    def test_nested_stages_are_subtracted_per_request(self):
        response = pb.FetchCatalogResponse(outcome=pb.PAGE_LOADED, timings_ms={
            "byparr_http_ms": 4000, "fetch_total_ms": 4080})
        row = measured_row(response, 4200)
        self.assertEqual(row["timings_ms"]["gateway_overhead_ms"], 200)
        self.assertEqual(row["timings_ms"]["grpc_transport_ms"], 120)
        self.assertFalse(row["complete_timings"])
        stats = summaries([row])
        self.assertIsNone(stats["browser_ttfb_ms"]["mean"])
        self.assertEqual(stats["browser_ttfb_ms"]["samples"], 0)

    def test_failed_requests_do_not_enter_success_latency(self):
        row = measured_row(pb.FetchCatalogResponse(outcome=pb.BLOCKED), 9000)
        self.assertIsNone(summaries([row])["rpc_total_ms"]["mean"])

    def test_navigation_timing_excludes_connect_and_distinguishes_missing(self):
        self.assertEqual(navigation_timings({"requestStart": 100, "responseStart": 450, "responseEnd": 700}),
                         {"browser_document_request_ms": 600, "browser_ttfb_ms": 350})
        for entry in (None, {}, {"requestStart": 0, "responseStart": 0, "responseEnd": 0},
                      {"requestStart": 2, "responseStart": 1, "responseEnd": 4},
                      {"requestStart": 1, "responseStart": float("nan"), "responseEnd": 4}):
            self.assertEqual(navigation_timings(entry), {})

    def test_invalid_diagnostic_headers_cannot_change_catalog_result(self):
        module, session = mock_requests()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {
            "status": "ok", "solution": {"url": "https://www.vinted.fr/catalog", "status": 200,
                                         "response": CATALOG, "cookies": []}}
        for header in ("broken-json", '{"browser_ttfb_ms":NaN,"browser_navigation_ms":-1}', "[]"):
            session.post.return_value.headers = {"X-Vinted-Timings": header}
            with patch("curl_cffi.requests", module):
                result = fetch_catalog("FR", measure_timing=True)
            self.assertEqual(result.outcome, "page_loaded")
            self.assertNotIn("browser_ttfb_ms", result.timings_ms)
            self.assertIn("byparr_http_ms", result.timings_ms)
            self.assertIn("catalog_validation_ms", result.timings_ms)


class BrowserTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_diagnostics_do_not_change_result_or_read_page_by_default(self):
        result = (False, CATALOG, object())
        endpoint = SimpleNamespace(_navigate_and_solve=AsyncMock(return_value=result),
                                   remaining_ms=lambda timer: 10000)
        page = SimpleNamespace(url="https://www.vinted.fr/catalog", evaluate=AsyncMock(side_effect=RuntimeError))
        request = SimpleNamespace(url=page.url)
        install_navigation_timing(endpoint)
        self.assertIs(await endpoint._navigate_and_solve(SimpleNamespace(page=page), request, None), result)
        page.evaluate.assert_not_awaited()
        timings = {}
        token = REQUEST_TIMINGS.set(timings)
        try:
            self.assertIs(await endpoint._navigate_and_solve(SimpleNamespace(page=page), request, None), result)
        finally:
            REQUEST_TIMINGS.reset(token)
        self.assertIn("browser_navigation_ms", timings)
        self.assertNotIn("browser_ttfb_ms", timings)


if __name__ == "__main__":
    unittest.main()
