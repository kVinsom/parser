import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from static_cache import (
    ASSET_ROUTE,
    StaticAssetCache,
    asset_key,
    install_static_cache,
    lifetime,
)

URL = "https://marketplace-web-assets.vinted.com/_next/static/chunks/hash.js"
HEADERS = {"cache-control": "public, max-age=31536000, immutable",
           "content-type": "application/javascript", "vary": "Origin, Accept-Encoding",
           "access-control-allow-origin": "https://www.vinted.fr",
           "content-encoding": "br", "content-length": "12"}


def request(url=URL, kind="script", origin="https://www.vinted.fr", **changes):
    result = Mock(url=url, resource_type=kind, method="GET", headers={"origin": origin})
    for name, value in changes.items():
        setattr(result, name, value)
    return result


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.cache = StaticAssetCache(clock=lambda: self.now)

    def test_dynamic_pages_api_images_other_hosts_and_authorized_requests_are_excluded(self):
        for url, kind in (
            ("https://www.vinted.fr/catalog?search_text=Nike", "document"),
            ("https://www.vinted.de/items/123", "document"),
            ("https://www.vinted.it/api/v2/items", "xhr"),
            ("https://images1.vinted.net/photo.jpg", "image"),
            (URL.replace(".com/", ".com.evil/"), "script"),
            (URL.replace("https://", "http://"), "script"),
            (URL + "?session=secret", "script"),
            (URL.replace("/_next/static/", "/api/"), "script"),
        ):
            self.assertIsNone(asset_key(request(url, kind)))
        for changes in ({"method": "POST"}, {"headers": {"authorization": "private"}},
                        {"headers": {"range": "bytes=0-10"}}):
            self.assertIsNone(asset_key(request(**changes)))

    def test_regions_and_asset_versions_have_separate_keys(self):
        keys = [asset_key(request(origin=f"https://www.vinted.{region}"))
                for region in ("fr", "de", "it", "co.uk")]
        self.assertEqual(len(set(keys)), 4)
        self.assertNotEqual(keys[0], asset_key(request(URL.replace("hash.js", "newhash.js"))))

    def test_cache_permissions_and_freshness_are_required(self):
        self.assertEqual(lifetime(HEADERS, 0), 86400)
        for change in (
            {"cache-control": "public, max-age=20"},
            {"cache-control": "private, immutable, max-age=20"},
            {"cache-control": "public, no-store, immutable, max-age=20"},
            {"cache-control": "public, no-cache, immutable, max-age=20"},
            {"cache-control": "public, immutable, max-age=broken"},
            {"set-cookie": "private"}, {"vary": "Cookie"}, {"vary": "*"},
            {"age": "99999999"}, {"date": "broken"},
        ):
            self.assertEqual(lifetime(HEADERS | change, 0), 0)
        self.assertEqual(lifetime(HEADERS | {"age": "200000"}, 0), 86400)
        self.assertEqual(lifetime(HEADERS | {
            "cache-control": "public, immutable, max-age=100", "age": "70"}, 0), 30)
        self.assertEqual(lifetime(HEADERS | {
            "cache-control": "public, immutable, max-age=100",
            "date": "Thu, 01 Jan 1970 00:00:00 GMT"}, 80), 20)

    def test_expiration_lru_memory_and_entry_bounds(self):
        cache = StaticAssetCache(max_bytes=6, max_asset_bytes=4, max_entries=2, clock=lambda: self.now)
        cache.put("a", b"aaa", HEADERS, 20)
        cache.put("b", b"bbb", HEADERS, 20)
        cache.get("a")
        cache.put("c", b"ccc", HEADERS, 20)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.bytes, 6)
        cache.put("large", b"12345", HEADERS, 20)
        self.assertIsNone(cache.get("large"))
        self.now = 21
        self.assertIsNone(cache.get("a"))
        self.assertIsNone(cache.get("c"))
        self.assertEqual(cache.bytes, 0)

    async def test_browser_learned_decoded_response_is_replayed_without_network(self):
        req = request()
        response = SimpleNamespace(status=200, url=URL, headers=HEADERS,
                                   body=AsyncMock(return_value=b"window.answer=42"))
        await self.cache.capture(req, response, asset_key(req))
        page = SimpleNamespace(on=Mock(), route=AsyncMock())
        await self.cache.install(page)
        pattern, handler = page.route.call_args.args
        self.assertEqual(pattern, ASSET_ROUTE)
        route = SimpleNamespace(request=req, fulfill=AsyncMock(), fallback=AsyncMock())
        await handler(route)
        route.fallback.assert_not_awaited()
        delivered = route.fulfill.call_args.kwargs
        self.assertEqual(delivered["body"], b"window.answer=42")
        self.assertEqual(delivered["headers"]["access-control-allow-origin"], "https://www.vinted.fr")
        self.assertNotIn("content-encoding", delivered["headers"])
        self.assertNotIn("content-length", delivered["headers"])
        self.assertEqual(self.cache.hits, 1)
        # Hits have no browser response capture, so cannot refresh their own TTL.
        self.assertIn(req, self.cache.served_requests)
        self.now = 86401
        route.fetch = AsyncMock(return_value=SimpleNamespace(
            status=403, url=URL, headers={}, dispose=AsyncMock()))
        await handler(route)
        route.fetch.assert_awaited_once_with(timeout=10000, max_redirects=0, max_retries=0)
        route.fallback.assert_not_awaited()

    async def test_refusals_redirects_html_and_failed_body_reads_are_never_cached(self):
        for status, url, headers, error in (
            (429, URL, HEADERS, None), (403, URL, HEADERS, None),
            (200, "https://example.org/challenge", HEADERS, None),
            (200, URL, HEADERS | {"content-type": "text/html"}, None),
            (200, URL, HEADERS, RuntimeError("page closed")),
        ):
            req = request()
            response = SimpleNamespace(status=status, url=url, headers=headers,
                                       body=AsyncMock(return_value=b"body", side_effect=error))
            if error:
                with self.assertRaises(RuntimeError):
                    await self.cache.capture(req, response, asset_key(req))
            else:
                await self.cache.capture(req, response, asset_key(req))
            self.assertFalse(self.cache.entries)

    async def test_out_of_scope_requests_keep_original_media_blocking(self):
        page = SimpleNamespace(on=Mock(), route=AsyncMock())
        original = AsyncMock()
        endpoints = SimpleNamespace(setup_routes=original)
        install_static_cache(endpoints, self.cache)
        dep = SimpleNamespace(page=page)
        await endpoints.setup_routes("request", dep)
        original.assert_awaited_once_with("request", dep)
        handler = page.route.call_args.args[1]
        route = SimpleNamespace(request=request(kind="image"), fallback=AsyncMock(), fulfill=AsyncMock())
        await handler(route)
        route.fallback.assert_awaited_once()
        route.fulfill.assert_not_awaited()

    async def test_miss_fetches_once_then_disposes_response_and_subsequent_hit_is_local(self):
        page = SimpleNamespace(route=AsyncMock())
        await self.cache.install(page)
        handler = page.route.call_args.args[1]
        response = SimpleNamespace(status=200, url=URL, headers=HEADERS,
                                   body=AsyncMock(return_value=b"javascript"), dispose=AsyncMock())
        route = SimpleNamespace(request=request(), fetch=AsyncMock(return_value=response),
                                fulfill=AsyncMock(), abort=AsyncMock())
        await handler(route)
        await handler(route)
        route.fetch.assert_awaited_once_with(timeout=10000, max_redirects=0, max_retries=0)
        response.dispose.assert_awaited_once()
        self.assertEqual(route.fulfill.await_count, 2)
        route.abort.assert_not_awaited()
        self.assertEqual(self.cache.hits, 1)

    async def test_network_failure_never_retries_and_corrupt_body_does_not_fail_response(self):
        page = SimpleNamespace(route=AsyncMock())
        await self.cache.install(page)
        handler = page.route.call_args.args[1]
        route = SimpleNamespace(request=request(), fetch=AsyncMock(side_effect=TimeoutError()),
                                fulfill=AsyncMock(), abort=AsyncMock())
        await handler(route)
        route.fetch.assert_awaited_once()
        route.abort.assert_awaited_once()
        self.assertFalse(self.cache.entries)
        response = SimpleNamespace(status=200, url=URL, headers=HEADERS,
                                   body=AsyncMock(side_effect=RuntimeError()), dispose=AsyncMock())
        route.fetch = AsyncMock(return_value=response)
        await handler(route)
        route.fulfill.assert_awaited_once_with(response=response)
        response.dispose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
