import gzip
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

from direct_assets import MAX_WIRE_BYTES, DirectAssetFetcher, NoRedirect, fetch_asset
from static_cache import StaticAssetCache

URL = "https://marketplace-web-assets.vinted.com/_next/static/chunks/hash.js"
PUBLIC = {"cache-control": "public, max-age=3600, immutable",
          "content-type": "application/javascript"}


def request(origin="https://www.vinted.fr", **changes):
    return Mock(url=URL, method="GET", resource_type="script",
                headers={"origin": origin, "cookie": "private",
                         "user-agent": "browser"}, **changes)


class Reply(io.BytesIO):
    def __init__(self, data, headers=None, status=200, url=URL):
        super().__init__(data)
        self.headers = PUBLIC | (headers or {})
        self.status, self.url, self.reads = status, url, 0

    def geturl(self):
        return self.url

    def read1(self, size):
        self.reads += 1
        return super().read1(size)


class DirectAssetTests(unittest.TestCase):
    def fetch(self, reply, req=None, decoded_limit=1024):
        opener = Mock()
        opener.open.return_value = reply
        result = fetch_asset(req or request(), decoded_limit, opener=opener)
        return result, opener.open.call_args.args[0]

    def test_only_public_assets_and_no_redirects(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.test"))
        for url in ("https://www.vinted.fr/catalog", URL + "?token=private", URL.replace(".com/", ".com.evil/")):
            req = request()
            req.url = url
            opener = Mock()
            with self.assertRaises(ValueError):
                fetch_asset(req, 1024, opener=opener)
            opener.open.assert_not_called()

    def test_cookies_auth_and_proxy_environment_are_never_used(self):
        for origin in ("https://www.vinted.fr", "https://www.vinted.de",
                       "https://www.vinted.it", "https://www.vinted.co.uk"):
            with self.subTest(origin=origin):
                response, sent = self.fetch(Reply(b"window.ready=1"), request(origin))
                headers = {k.lower(): v for k, v in sent.header_items()}
                self.assertEqual(headers["origin"], origin)
                self.assertEqual(headers["accept-encoding"], "gzip")
                self.assertFalse({"cookie", "authorization", "proxy-authorization"} & headers.keys())
                self.assertEqual(response.data, b"window.ready=1")
        with patch("direct_assets.urllib.request.build_opener") as build:
            build.return_value.open.return_value = Reply(b"window.ready=1")
            fetch_asset(request(), 1024)
            self.assertEqual(build.call_args.args[0].proxies, {})

    def test_header_limit_rejects_before_reading_and_chunked_limit_closes_stream(self):
        reply = Reply(b"irrelevant", {"content-length": str(MAX_WIRE_BYTES + 1)})
        with self.assertRaisesRegex(ValueError, "transfer limit"):
            self.fetch(reply)
        self.assertEqual(reply.reads, 0)
        self.assertTrue(reply.closed)
        reply = Reply(b"x" * (MAX_WIRE_BYTES + 100000))
        with self.assertRaisesRegex(ValueError, "transfer limit"):
            self.fetch(reply, decoded_limit=MAX_WIRE_BYTES * 2)
        self.assertEqual(reply.reads, MAX_WIRE_BYTES // 65536 + 1)
        self.assertTrue(reply.closed)

    def test_gzip_is_bounded_and_missing_bytes_never_become_javascript(self):
        data = b"window.answer=42;"
        compressed = gzip.compress(data)
        response, _ = self.fetch(Reply(compressed, {"content-encoding": "gzip", "content-length": str(len(compressed))}))
        self.assertEqual(response.data, data)
        self.assertNotIn("content-encoding", response.headers)
        self.assertNotIn("content-length", response.headers)
        for raw, headers in ((compressed[:-2], {"content-encoding": "gzip"}),
                             (gzip.compress(b"x" * 10000), {"content-encoding": "gzip"}),
                             (data, {"content-length": str(len(data) + 1)}),
                             (data, {"content-encoding": "br"})):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.fetch(Reply(raw, headers))

    def test_private_redirect_error_and_html_responses_never_read_body(self):
        for headers, status, url in (({"cache-control": "private, immutable, max-age=3600"}, 200, URL),
                                      ({"set-cookie": "private"}, 200, URL),
                                      ({"content-type": "text/html"}, 200, URL),
                                      ({}, 403, URL), ({}, 200, "https://evil.test/")):
            reply = Reply(b"private", headers, status, url)
            with self.subTest(headers=headers, status=status), self.assertRaises(ValueError):
                self.fetch(reply)
            self.assertEqual(reply.reads, 0)


class DirectCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_miss_and_cached_hit_never_use_page_proxy_for_two_profiles(self):
        cache = StaticAssetCache(direct_fetcher=DirectAssetFetcher())
        page = SimpleNamespace(route=AsyncMock())
        await cache.install(page)
        handler = page.route.call_args.args[1]
        with patch("direct_assets.urllib.request.build_opener") as build:
            build.return_value.open.side_effect = lambda *args, **kwargs: Reply(b"window.ready=1")
            for origin in ("https://www.vinted.fr", "https://www.vinted.de"):
                for _ in range(2):
                    route = SimpleNamespace(request=request(origin), fetch=AsyncMock(),
                                            fulfill=AsyncMock(), abort=AsyncMock(), fallback=AsyncMock())
                    await handler(route)
                    route.fetch.assert_not_awaited()
                    route.fulfill.assert_awaited_once()
                    route.abort.assert_not_awaited()
            self.assertEqual(build.call_count, 2)
            self.assertEqual(cache.hits, 2)

    async def test_direct_failure_never_falls_back_to_paid_proxy(self):
        cache = StaticAssetCache(direct_fetcher=DirectAssetFetcher())
        page = SimpleNamespace(route=AsyncMock())
        await cache.install(page)
        route = SimpleNamespace(request=request(), fetch=AsyncMock(), fulfill=AsyncMock(),
                                abort=AsyncMock(), fallback=AsyncMock())
        with patch("direct_assets.urllib.request.build_opener") as build:
            error = HTTPError(URL, 403, "denied", {}, io.BytesIO(b"denied"))
            build.return_value.open.side_effect = error
            await page.route.call_args.args[1](route)
            await page.route.call_args.args[1](route)
            build.return_value.open.assert_called_once()
            self.assertTrue(error.closed)
        self.assertEqual(route.abort.await_count, 2)
        route.fetch.assert_not_awaited()
        route.fallback.assert_not_awaited()
        route.fulfill.assert_not_awaited()
