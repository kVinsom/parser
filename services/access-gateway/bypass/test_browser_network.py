import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from browser_network import (
    BACKGROUND_DOWNLOAD_HOSTS,
    COMPONENT_PREFS,
    DIRECT_SERVICE_DOMAINS,
    install_browser_network_policy,
    protected_factory,
    source_route,
    source_url,
)
from traffic_policy import should_skip


class BrowserNetworkTests(unittest.TestCase):
    def test_every_proxy_protocol_keeps_credentials_and_routes_only_exact_hosts_locally(
        self,
    ):
        for scheme in ("http", "https", "socks5"):
            with self.subTest(scheme=scheme):
                original = {
                    "server": scheme + "://proxy.example:10000",
                    "username": "user",
                    "password": "test",
                }
                snapshot = dict(original)
                factory = Mock(return_value=object())
                protected_factory(factory)(headless=True, proxy=original)
                options = factory.call_args.kwargs
                self.assertEqual(original, snapshot)
                self.assertEqual(
                    {key: options["proxy"][key] for key in original}, original
                )
                self.assertEqual(
                    set(options["proxy"]["bypass"].split(",")),
                    {*BACKGROUND_DOWNLOAD_HOSTS, *DIRECT_SERVICE_DOMAINS},
                )
                self.assertEqual(
                    set(options["extra_prefs"]["network.dns.localDomains"].split(",")),
                    set(BACKGROUND_DOWNLOAD_HOSTS),
                )
                self.assertEqual(
                    set(options["extra_prefs"]["network.proxy.no_proxies_on"].split(",")),
                    {*BACKGROUND_DOWNLOAD_HOSTS, *DIRECT_SERVICE_DOMAINS},
                )
                self.assertNotIn("ignore_https_errors", options)

    def test_existing_preferences_and_bypass_survive_without_mutating_the_caller(self):
        proxy = {
            "server": "http://proxy.example:10000",
            "bypass": "localhost,example.local",
        }
        prefs = {
            "custom.pref": 42,
            "network.dns.localDomains": "local.test",
            "media.eme.enabled": True,
        }
        factory = Mock()
        protected_factory(factory)(proxy=proxy, extra_prefs=prefs)
        result = factory.call_args.kwargs
        self.assertEqual(result["extra_prefs"]["custom.pref"], 42)
        self.assertTrue(
            result["proxy"]["bypass"].startswith("localhost,example.local,")
        )
        self.assertTrue(
            result["extra_prefs"]["network.dns.localDomains"].startswith("local.test,")
        )
        self.assertEqual(proxy["bypass"], "localhost,example.local")
        self.assertTrue(prefs["media.eme.enabled"])
        self.assertFalse(result["extra_prefs"]["media.eme.enabled"])

    def test_direct_mode_and_per_request_factory_have_the_same_component_policy(self):
        utils = SimpleNamespace(InvisiblePlaywright=Mock())
        original = utils.InvisiblePlaywright
        utils.InvisiblePlaywright = protected_factory(utils.InvisiblePlaywright)
        utils.InvisiblePlaywright(proxy=None)
        self.assertIsNone(original.call_args.kwargs["proxy"])
        for key, value in COMPONENT_PREFS.items():
            self.assertEqual(original.call_args.kwargs["extra_prefs"][key], value)

    def test_page_requests_to_the_two_download_hosts_are_blocked_for_every_resource_type(
        self,
    ):
        for host in BACKGROUND_DOWNLOAD_HOSTS:
            for kind in ("document", "script", "fetch", "xhr", "image", "other"):
                self.assertTrue(
                    should_skip(
                        SimpleNamespace(
                            url="https://" + host + "/download", resource_type=kind
                        )
                    )
                )
        for market in ("fr", "it", "de", "co.uk"):
            for brand in ("Nike", "Gucci", "Stone+Island"):
                self.assertFalse(
                    should_skip(
                        SimpleNamespace(
                            url=f"https://www.vinted.{market}/catalog?search_text={brand}",
                            resource_type="document",
                            headers={},
                        )
                    )
                )
        for host in (
            "edgedl.me.gvt1.com.example",
            "firefox-settings-attachments.cdn.mozilla.net.example",
        ):
            self.assertFalse(
                should_skip(
                    SimpleNamespace(
                        url="https://" + host + "/download", resource_type="document"
                    )
                )
            )


class BrowserRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_filter_blocks_other_sites_and_services_for_all_page_requests(self):
        for url in (
            "https://firefox.settings.services.mozilla.com/v1",
            "https://ocsp.digicert.com/check", "https://outside.example/file",
            "https://metrics.vinted.lt/events", "http://www.vinted.fr/",
            "https://images1.vinted.net/photo.webp", "https://images2.vinted.net/photo.webp",
            "https://marketplace-web-assets.vinted.com/_next/static/chunks/app.js",
            "https://www.vinted.fr:444/", "https://www.vinted.fr:bad/",
            "https://user:secret@www.vinted.fr/", "https://www.vinted.fr.evil/",
        ):
            with self.subTest(url=url):
                route = SimpleNamespace(request=SimpleNamespace(url=url),
                                        abort=AsyncMock(), fallback=AsyncMock())
                await source_route(route)
                route.abort.assert_awaited_once()
                route.fallback.assert_not_awaited()
        for host in ("www.vinted.fr", "www.vinted.it", "www.vinted.de", "www.vinted.co.uk",
                     "geo.captcha-delivery.com", "cdn.cookielaw.org"):
            for brand in ("Nike", "Gucci"):
                url = f"https://{host}/catalog?search_text={brand}"
                route = SimpleNamespace(request=SimpleNamespace(url=url),
                                        abort=AsyncMock(), fallback=AsyncMock())
                await source_route(route)
                route.fallback.assert_awaited_once()
                route.abort.assert_not_awaited()
        for value in ("about:blank", "data:text/plain,test", "blob:https://www.vinted.fr/1"):
            self.assertTrue(source_url(value))

    async def test_service_geo_is_direct_and_every_new_context_is_filtered(self):
        api = SimpleNamespace(prepare_session_geo=Mock(return_value="geo"))
        core = SimpleNamespace(resolve_session_locale=Mock(return_value="fr-FR"))
        original_geo, original_locale = api.prepare_session_geo, core.resolve_session_locale
        context = SimpleNamespace(route=AsyncMock())
        instance = SimpleNamespace(new_context=AsyncMock(return_value=context))
        original_context = instance.new_context

        @asynccontextmanager
        async def factory(**kwargs):
            yield instance

        utils = SimpleNamespace(InvisiblePlaywright=factory)
        modules = {"invisible_core": core, "invisible_playwright": SimpleNamespace(async_api=api),
                   "invisible_playwright.async_api": api}
        with patch.dict("sys.modules", modules):
            policy = install_browser_network_policy(utils)
            try:
                paid = {"server": "http://paid.example:10000", "username": "u", "password": "p"}
                async with utils.InvisiblePlaywright(proxy=paid) as browser:
                    self.assertEqual(api.prepare_session_geo("UTC", paid), "geo")
                    self.assertEqual(core.resolve_session_locale("1.2.3.4", paid), "fr-FR")
                    original_geo.assert_called_once_with("UTC", None)
                    original_locale.assert_called_once_with(None, None)
                    for _ in range(2):
                        await browser.new_context(service_workers="allow")
                    self.assertEqual(original_context.await_count, 2)
                    self.assertEqual(context.route.await_count, 2)
                    original_context.assert_awaited_with(service_workers="block")
                    context.route.assert_awaited_with("**/*", source_route)
                self.assertFalse(policy.configs)
            finally:
                policy.close()
                policy.restore_geo()
            self.assertIs(api.prepare_session_geo, original_geo)
            self.assertIs(core.resolve_session_locale, original_locale)


if __name__ == "__main__":
    unittest.main()
