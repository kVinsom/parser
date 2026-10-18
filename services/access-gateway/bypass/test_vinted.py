"""Offline checks for routing, false successes, and partial regional batches."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import vinted as app
from artifacts import read_html, save_html
from vinted_regions import Region, get_region, select_regions


class RequestFailure(Exception):
    pass


class RequestTimeout(RequestFailure):
    pass


def mock_requests():
    module = MagicMock()
    module.exceptions = SimpleNamespace(Timeout=RequestTimeout, RequestException=RequestFailure)
    return module, module.Session.return_value.__enter__.return_value

CATALOG = '''<title>Articles | Vinted</title>
<script src="https://js.datadome.co/tags.js"></script>
<a href="/items/123-shoes">Shoes</a>
<a href="/items/123-shoes?tracking=secret">Shoes image</a>
<a href="https://www.vinted.fr/items/456-shirt">Shirt</a>'''


class RegionalRoutingTests(unittest.TestCase):
    def test_special_domains_alias_and_deduplication(self):
        regions = select_regions(["gb,FR", "UK", "fr", "it", "de"])
        self.assertEqual([r.code for r in regions], ["UK", "FR", "IT", "DE"])
        self.assertEqual(select_regions(["all"]), regions)
        self.assertEqual([r.catalog_url() for r in regions], [
            "https://www.vinted.co.uk/catalog", "https://www.vinted.fr/catalog",
            "https://www.vinted.it/catalog",
            "https://www.vinted.de/catalog",
        ])

    def test_invalid_region_fails_before_network(self):
        for codes in (["PL"], ["pl"], ["CA"], ["AT"], ["US"], ["AU"], ["all", "FR"], [",, "]):
            with self.subTest(codes=codes), self.assertRaises(ValueError):
                select_regions(codes)
        with self.assertRaises(ValueError):
            app.fetch_catalog(Region("FR", "Fake", "example.org"))
        with self.assertRaises(ValueError):
            app.fetch_catalog(Region("PL", "Poland", "vinted.pl"))

    def test_search_query_is_encoded_not_interpreted_as_a_url(self):
        self.assertEqual(get_region("DE").catalog_url("nike & adidas", 2),
                         "https://www.vinted.de/catalog?search_text=nike+%26+adidas&page=2")
        with self.assertRaises(ValueError):
            get_region("DE").catalog_url(page=0)

    def test_invalid_service_address_is_rejected(self):
        for endpoint in ("file:///tmp/x", "http://user:secret@localhost:8191",
                         "http://localhost:8191/v1", "http://localhost:invalid", "http://localhost?key=secret"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                app.validate_endpoint(endpoint)


class CatalogClassificationTests(unittest.TestCase):
    def test_redirect_cannot_silently_change_search_page_or_order(self):
        region = get_region("FR")
        requested = region.catalog_url("Amiri", 2, True)
        for final in (region.catalog_url("Stone Island", 2, True),
                      region.catalog_url("Amiri", 1, True),
                      region.catalog_url("Amiri", 2, False),
                      region.catalog_url()):
            with self.subTest(final=final):
                result = app.classify(app.CatalogResult("FR", requested), region, final, 200, CATALOG)
                self.assertEqual(result.outcome, "unexpected_redirect")
        result = app.classify(app.CatalogResult("FR", requested), region,
                              requested + "&tracking=private-token", 200, CATALOG)
        self.assertEqual(result.outcome, "page_loaded")
        self.assertNotIn("private-token", json.dumps(result.summary()))

    def classify(self, body=CATALOG, status=200, final_url="https://www.vinted.fr/catalog"):
        region = get_region("FR")
        return app.classify(app.CatalogResult("FR", region.catalog_url()), region, final_url, status, body)

    def test_items_are_unique_and_tracking_data_is_removed(self):
        result = self.classify()
        self.assertEqual((result.outcome, result.item_count, result.item_link_count), ("page_loaded", 2, 3))
        self.assertNotIn("secret", json.dumps(result.summary()))
        self.assertNotIn("body", result.summary())

    def test_every_market_uses_its_own_item_domain(self):
        for region in select_regions(["all"]):
            with self.subTest(region=region.code):
                body = CATALOG.replace("www.vinted.fr", "www." + region.domain)
                result = app.classify(app.CatalogResult(region.code, region.catalog_url()),
                                      region, region.catalog_url(), 200, body)
                self.assertEqual((result.outcome, result.item_count), ("page_loaded", 2))

    def test_observed_empty_catalog_marker_is_distinct_from_partial_or_blocked_page(self):
        empty = (Path(__file__).resolve().parents[2] / 'parser/tests/fixtures/FR_empty.html').read_text(encoding='utf-8')
        result = self.classify(empty)
        self.assertEqual((result.outcome, result.item_count), ('page_loaded', 0))
        for body, status, expected in (
            (empty, 403, 'blocked'),
            (empty, 429, 'rate_limited'),
            (empty + '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>', 200, 'blocked'),
            (empty + CATALOG, 200, 'unexpected_content'),
            (empty.replace('search-empty-state--body', 'loading'), 200, 'unexpected_content'),
            ('<title>Vinted</title><script>' + empty + '</script>', 200, 'unexpected_content'),
        ):
            with self.subTest(status=status, expected=expected):
                self.assertEqual(self.classify(body, status=status).outcome, expected)

    def test_http_200_challenge_is_not_success(self):
        for source in ("https://geo.captcha-delivery.com/captcha/?token=secret",
                       "//ct.captcha-delivery.com/c.js?token=secret"):
            with self.subTest(source=source):
                result = self.classify(CATALOG + f'<iframe src="{source}"></iframe>')
                self.assertEqual(result.outcome, "blocked")
                self.assertNotIn("secret", json.dumps(result.summary()))

    def test_wrong_region_and_non_catalog_redirects_are_not_success(self):
        for url in ("https://www.vinted.de/catalog", "https://www.vinted.fr/",
                    "https://www.vinted.fr/member/signup", "https://vinted.fr.evil.example/catalog",
                    "https://www.vinted.fr:444/catalog", "https://user:secret@www.vinted.fr/catalog"):
            with self.subTest(url=url):
                self.assertEqual(self.classify(final_url=url).outcome, "unexpected_redirect")

    def test_vinted_title_or_navigation_alone_is_insufficient(self):
        for body in ('<title>Vinted</title><a href="/catalog">Catalog</a>',
                     '<title>Vinted</title><a href="https://example.org/items/123">Item</a>',
                     '<title>Vinted</title><a href="https://www.vinted.de/items/123">Item</a>',
                     '<title>Vinted</title><a href="/items/not-a-product">Item</a>'):
            with self.subTest(body=body):
                self.assertEqual(self.classify(body).outcome, "unexpected_content")

    def test_unsuccessful_http_status_overrides_catalog(self):
        for status, expected in ((403, "blocked"), (429, "rate_limited"), (503, "upstream_error")):
            with self.subTest(status=status):
                self.assertEqual(self.classify(status=status).outcome, expected)


class ByparrIntegrationTests(unittest.TestCase):
    def test_http_timeouts_are_distinct_from_service_errors_and_never_retried(self):
        for status, expected in ((408, 'timeout'), (504, 'timeout'), (500, 'service_error'), (502, 'service_error')):
            with self.subTest(status=status):
                module, session = mock_requests()
                session.post.return_value.status_code = status
                with patch('curl_cffi.requests', module):
                    result = app.fetch_catalog('FR')
                self.assertEqual(result.outcome, expected)
                self.assertEqual(result.byparr_http_status, status)
                self.assertEqual(session.post.call_count, 1)

    def test_routes_region_and_does_not_persist_cookies(self):
        module, session = mock_requests()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {
            "status": "ok", "solution": {"url": "https://www.vinted.fr/catalog?search_text=nike", "status": 200,
                "response": CATALOG, "cookies": [{"name": "session", "value": "secret-cookie"}]}}
        with patch("curl_cffi.requests", module):
            result = app.fetch_catalog("FR", timeout=10, search_text="nike")
        self.assertEqual(result.outcome, "page_loaded")
        self.assertEqual(session.post.call_count, 1)
        args = session.post.call_args.kwargs
        self.assertEqual(args["json"]["url"], "https://www.vinted.fr/catalog?search_text=nike")
        self.assertEqual(args["json"]["maxTimeout"], 10000)
        self.assertTrue(args["json"]["blockMedia"])
        self.assertTrue(result.block_media)
        self.assertFalse(args["allow_redirects"])
        self.assertEqual(result.cookie_count, 1)
        self.assertNotIn("secret-cookie", json.dumps(result.summary()))

    def test_malformed_solutions_are_not_success_and_do_not_leak_errors(self):
        for payload in ([], {"status": "error", "message": "secret-error"},
                        {"status": "ok", "solution": {"status": True, "url": "https://www.vinted.fr/catalog",
                                                     "response": CATALOG}},
                        {"status": "ok", "solution": {"status": 200, "url": "https://www.vinted.fr/catalog",
                                                     "response": {}, "cookies": []}}):
            with self.subTest(payload=payload):
                module, session = mock_requests()
                session.post.return_value.status_code = 200
                session.post.return_value.json.return_value = payload
                with patch("curl_cffi.requests", module):
                    result = app.fetch_catalog("FR")
                self.assertEqual(result.outcome, "service_error")
                self.assertNotIn("secret-error", json.dumps(result.summary()))

    def test_timeout_does_not_retry_and_hides_raw_exception(self):
        module, session = mock_requests()
        session.post.side_effect = module.exceptions.Timeout("secret-url")
        with patch("curl_cffi.requests", module):
            result = app.fetch_catalog("UK")
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(session.post.call_count, 1)
        self.assertNotIn("secret-url", json.dumps(result.summary()))

    def test_media_can_be_loaded_for_comparison(self):
        module, session = mock_requests()
        session.post.return_value.status_code = 503
        with patch("curl_cffi.requests", module):
            result = app.fetch_catalog("FR", block_media=False)
        self.assertFalse(session.post.call_args.kwargs["json"]["blockMedia"])
        self.assertFalse(result.block_media)


class BatchReportingTests(unittest.TestCase):
    def setUp(self):
        (app.ROOT / ".tmp").mkdir(exist_ok=True)

    def test_compressed_snapshot_preserves_unicode_and_line_endings(self):
        body = '<title>Речі | Vinted</title>\r\n' + CATALOG * 50
        with tempfile.TemporaryDirectory(dir=app.ROOT / ".tmp") as directory:
            path = Path(directory) / "FR.html.gz"
            save_html(path, body)
            self.assertEqual(read_html(path), body)
            self.assertLess(path.stat().st_size, len(body.encode("utf-8")))

    def test_failed_region_does_not_stop_batch_and_exit_code_is_nonzero(self):
        rows = [app.CatalogResult("FR", get_region("FR").catalog_url(), "blocked"),
                app.CatalogResult("UK", get_region("UK").catalog_url(), "page_loaded", item_count=10)]
        with tempfile.TemporaryDirectory(dir=app.ROOT / ".tmp") as directory:
            with patch.object(app, "ROOT", Path(directory)), patch.object(app, "fetch_catalog", side_effect=rows) as fetch:
                with contextlib.redirect_stdout(io.StringIO()):
                    status = app.main(["--regions", "FR", "UK"])
                self.assertEqual(fetch.call_count, 2)
            report_file = next(Path(directory).glob(".artifacts/*/report.json"))
            report = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertEqual(status, 2)
            self.assertEqual(report["completed"], 2)
            self.assertEqual(report["counts"], {"blocked": 1, "page_loaded": 1})
            self.assertFalse(report["all_accessible"])

    def test_interrupted_batch_keeps_completed_results(self):
        row = app.CatalogResult("FR", get_region("FR").catalog_url(), "page_loaded", item_count=10)
        with tempfile.TemporaryDirectory(dir=app.ROOT / ".tmp") as directory:
            with patch.object(app, "ROOT", Path(directory)), patch.object(app, "fetch_catalog", side_effect=[row, KeyboardInterrupt]):
                with contextlib.redirect_stdout(io.StringIO()):
                    status = app.main(["--regions", "FR", "UK"])
            report = json.loads(next(Path(directory).glob(".artifacts/*/report.json")).read_text(encoding="utf-8"))
            self.assertEqual(status, 130)
            self.assertTrue(report["interrupted"])
            self.assertEqual(report["completed"], 1)
            self.assertFalse(report["all_accessible"])


if __name__ == "__main__":
    unittest.main()
