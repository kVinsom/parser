import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from traffic_policy import install_traffic_policy, should_skip


def request(url, resource_type="script", method="GET", headers=None):
    return SimpleNamespace(url=url, resource_type=resource_type, method=method, headers=headers or {})


class TrafficPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_same_policy_for_every_market_and_every_search(self):
        for market in ("de", "fr", "it", "co.uk"):
            self.assertTrue(should_skip(request(f"https://www.vinted.{market}/gtg/tag")))
            for brand in ("Nike", "Adidas", "Stone+Island", "Amiri"):
                self.assertFalse(should_skip(request(
                    f"https://www.vinted.{market}/catalog?search_text={brand}", "document")))
                self.assertFalse(should_skip(request(
                    f"https://www.vinted.{market}/api/v2/catalog/items?search_text={brand}", "fetch")))

    def test_only_confirmed_analytics_are_blocked(self):
        for url in ("https://metrics.vinted.lt/events", "https://www.google-analytics.com/collect",
                    "https://www.googletagmanager.com/gtm.js"):
            self.assertTrue(should_skip(request(url)))
        for url in ("https://metrics.vinted.lt.evil/events", "https://www.vinted.de/gtg-application.js",
                    "https://www.vinted.de:444/gtg/tags", "https://other.example/gtg/tags",
                    "https://static-assets.vinted.com/datadome/5.9.4/tags.js",
                    "https://geo.captcha-delivery.com/captcha/",
                    "https://marketplace-web-assets.vinted.com/_next/static/chunks/file.js",
                    "https://cdn.cookielaw.org/consent/data.json", "https://www.vinted.fr/api/v2/items/123"):
            self.assertFalse(should_skip(request(url)))
        self.assertFalse(should_skip(request("https://www.vinted.de:bad/gtg/tag")))
        self.assertFalse(should_skip(request("https://[broken/gtg/tag")))
        self.assertFalse(should_skip(request("https://www.vinted.de/gtg/test", "document")))

    def test_prefetch_does_not_block_regular_data_fetches_or_posts(self):
        for headers in ({"next-router-prefetch": "1"}, {"purpose": "prefetch"},
                        {"sec-purpose": "prefetch;prerender"}):
            url = "https://www.vinted.de/items/123"
            self.assertTrue(should_skip(request(url, "fetch", headers=headers)))
            self.assertFalse(should_skip(request(url, "document", headers=headers)))
            self.assertFalse(should_skip(request(url, "fetch", method="POST", headers=headers)))
        for headers in ({}, {"next-router-prefetch": "0"}, {"purpose": "navigate"}):
            self.assertFalse(should_skip(request("https://www.vinted.de/catalog", "fetch", headers=headers)))

    def test_only_unneeded_interface_endpoints_and_media_are_skipped(self):
        for market in ("fr", "de", "it", "co.uk"):
            for path in ("/api/v2/banners", "/api/v2/info_banners/catalog", "/api/v2/promoted_closets/"):
                url = f"https://www.vinted.{market}{path}?page=1"
                self.assertTrue(should_skip(request(url, "fetch")))
                self.assertFalse(should_skip(request(url, "document")))
                self.assertFalse(should_skip(request(url, "fetch", method="POST")))
            for path in ("/api/v2/items/123", "/api/v2/catalog/items", "/api/v2/banners-required"):
                self.assertFalse(should_skip(request(f"https://www.vinted.{market}{path}", "fetch")))
            for kind in ("image", "media", "font"):
                self.assertTrue(should_skip(request(f"https://www.vinted.{market}/large-resource", kind)))

    async def test_all_targets_get_the_guard_and_safe_routes_fall_back(self):
        endpoints = SimpleNamespace(setup_routes=AsyncMock())
        original = endpoints.setup_routes
        install_traffic_policy(endpoints)
        dep = SimpleNamespace(page=SimpleNamespace(route=AsyncMock()))
        await endpoints.setup_routes(request("https://example.org/"), dep)
        dep.page.route.assert_awaited_once()
        await endpoints.setup_routes(request("https://www.vinted.fr/catalog"), dep)
        handler = dep.page.route.call_args.args[1]
        route = SimpleNamespace(request=request("https://www.vinted.fr/api/v2/items", "fetch"),
                                abort=AsyncMock(), fallback=AsyncMock())
        await handler(route)
        route.fallback.assert_awaited_once()
        route.abort.assert_not_awaited()
        route.request = request("https://www.vinted.fr/gtg/tag")
        await handler(route)
        route.abort.assert_awaited_once()
        self.assertEqual(original.await_count, 2)

    async def test_service_bypass_cannot_be_used_by_pages_even_with_analytics_disabled(self):
        for skip_analytics in (True, False):
            endpoints = SimpleNamespace(setup_routes=AsyncMock())
            install_traffic_policy(endpoints, skip_analytics=skip_analytics)
            dep = SimpleNamespace(page=SimpleNamespace(route=AsyncMock()))
            await endpoints.setup_routes(request("https://www.vinted.fr/catalog"), dep)
            handler = dep.page.route.call_args.args[1]
            for url in ("https://firefox.settings.services.mozilla.com/v1",
                        "http://ocsp.digicert.com/check", "https://outside.example/file"):
                route = SimpleNamespace(request=request(url, "document"),
                                        abort=AsyncMock(), fallback=AsyncMock())
                await handler(route)
                route.abort.assert_awaited_once()
                route.fallback.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
