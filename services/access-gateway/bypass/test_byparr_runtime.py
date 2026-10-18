"""The readiness shortcut must never mistake a shell or challenge for a catalog."""

import asyncio
import importlib.util
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from byparr_runtime import (
    BrowserSession,
    install_catalog_readiness,
    install_navigation_timing,
    install_response_metadata,
    item_data_ready,
)

CATALOG = '<title>Articles | Vinted</title><a href="/items/123-shoes">Shoes</a>'


def item_page(identity="123", *, gallery=True, description=True, reviews=True, publication=True):
    records = []
    if gallery:
        records.append({"item_id": identity, "photos": [{"url": "https://images1.vinted.net/1.jpg"}]})
    if description:
        records.append({"item_id": identity, "description": ""})
    if reviews:
        records.append({"item_id": identity, "seller_id": 10, "feedback_count": 0, "feedback_reputation": None})
    chunk = json.dumps([1, "1:" + json.dumps(records) + "\n"])
    return ('<title>Shoes | Vinted</title><img data-testid="item-photo-1">'
            f'<script>self.__next_f.push({chunk})</script>'
            + ('<div data-testid="item-attributes-upload_date">Just now</div>' if publication else ''))


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_tab_is_replaced_and_response_extraction_uses_new_page(self):
        self.page.close = AsyncMock()
        self.page.goto.side_effect = RuntimeError('loadURI: browsingContext is undefined')
        replacement = SimpleNamespace(url=self.request.url,
            goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            content=AsyncMock(return_value=CATALOG), wait_for_load_state=AsyncMock())
        async def new_page():
            self.page.close.assert_awaited_once()
            return replacement
        context = SimpleNamespace(new_page=AsyncMock(side_effect=new_page))
        dep = BrowserSession(self.page, context)
        self.endpoints.setup_routes = AsyncMock()
        result = await self.endpoints._navigate_and_solve(dep, self.request, self.timer)
        self.assertIs(dep.page, replacement)
        self.assertIs(dep.context, context)
        self.assertEqual(result[1], CATALOG)
        self.page.goto.assert_awaited_once()
        self.page.close.assert_awaited_once()
        replacement.goto.assert_awaited_once()
        self.endpoints.setup_routes.assert_awaited_once_with(self.request, dep)

    async def test_replacement_is_single_attempt_and_does_not_retry_refusals(self):
        self.endpoints.setup_routes = AsyncMock()
        for status in (403, 429):
            self.page.goto = AsyncMock(return_value=SimpleNamespace(status=status))
            context = SimpleNamespace(new_page=AsyncMock())
            dep = BrowserSession(self.page, context)
            result = await self.endpoints._navigate_and_solve(dep, self.request, self.timer)
            self.assertEqual(result[2].status, status)
            context.new_page.assert_not_awaited()
        broken = RuntimeError('loadURI: browsingContext is undefined')
        self.page.goto = AsyncMock(side_effect=broken)
        self.page.close = AsyncMock()
        replacement = SimpleNamespace(url=self.request.url, goto=AsyncMock(side_effect=broken))
        context = SimpleNamespace(new_page=AsyncMock(return_value=replacement))
        with self.assertRaises(RuntimeError):
            await self.endpoints._navigate_and_solve(BrowserSession(self.page, context),
                                                    self.request, self.timer)
        context.new_page.assert_awaited_once()
        replacement.goto.assert_awaited_once()

    async def test_tab_recovery_stays_inside_original_navigation_deadline(self):
        self.page.goto.side_effect = RuntimeError('loadURI: browsingContext is undefined')
        context = SimpleNamespace(new_page=AsyncMock(side_effect=asyncio.Event().wait))
        self.endpoints.remaining_ms.return_value = 1001
        install_navigation_timing(self.endpoints)
        with self.assertRaises(TimeoutError):
            await self.endpoints._navigate_and_solve(BrowserSession(self.page, context),
                                                    self.request, self.timer)
        context.new_page.assert_awaited_once()

    async def test_initial_blank_tab_race_retries_once_and_still_settles_catalog(self):
        for message in (
            'Page.goto: Navigation is interrupted by another navigation to "about:blank"',
            'Page.goto: Protocol error (Page.navigate): can\'t access property "loadURI", browsingContext is undefined',
        ):
            with self.subTest(message=message):
                self.page.goto.reset_mock()
                self.endpoints._wait_for_networkidle.reset_mock()
                self.page.goto.side_effect = [RuntimeError(message), SimpleNamespace(status=200)]
                result = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
                self.assertEqual(result[1], CATALOG)
                self.assertEqual(self.page.goto.await_count, 2)
                self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_initial_navigation_retry_is_bounded_by_attempt_and_remaining_time(self):
        message = 'loadURI: browsingContext is undefined'
        self.page.goto.side_effect = RuntimeError(message)
        with self.assertRaises(RuntimeError):
            await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.assertEqual(self.page.goto.await_count, 2)
        self.page.goto.reset_mock()
        self.endpoints.remaining_ms.return_value = 500
        with self.assertRaises(RuntimeError):
            await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.assertEqual(self.page.goto.await_count, 1)

    async def test_navigation_retry_never_retries_block_timeout_or_external_redirect(self):
        for message, url in (
            ('Access denied', self.request.url),
            ('Timeout 12000ms exceeded', self.request.url),
            ('Target page, context or browser has been closed', self.request.url),
            ('loadURI: browsingContext is undefined', 'https://example.org/challenge'),
        ):
            with self.subTest(message=message):
                self.page.url = url
                self.page.goto.reset_mock()
                self.page.goto.side_effect = RuntimeError(message)
                with self.assertRaises(RuntimeError):
                    await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
                self.assertEqual(self.page.goto.await_count, 1)

    @unittest.skipUnless(importlib.util.find_spec('fastapi'), 'Run also with the parser environment')
    async def test_dependency_deadline_returns_408_and_releases_context(self):
        import httpx
        from fastapi import Depends, FastAPI

        app = FastAPI()
        closed = asyncio.Event()

        async def dependency():
            try:
                async with asyncio.timeout(0.02):
                    yield None
            finally:
                closed.set()

        @app.get('/timeout', dependencies=[Depends(dependency)])
        async def timeout():
            await asyncio.Event().wait()

        install_response_metadata(app, reuse=True, enabled=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/timeout')
        self.assertEqual(response.status_code, 408)
        self.assertEqual(response.headers['X-Vinted-Browser-Lifecycle'], 'reused')
        self.assertTrue(closed.is_set())

    async def test_source_budget_also_cancels_browser_operations_without_native_timeout(self):
        cancelled = asyncio.Event()

        async def hung(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        endpoints = SimpleNamespace(_navigate_and_solve=hung, remaining_ms=lambda timer: 10)
        install_navigation_timing(endpoints)
        with self.assertRaises(TimeoutError):
            await endpoints._navigate_and_solve(None, None, None)
        self.assertTrue(cancelled.is_set())

    def setUp(self):
        self.original = AsyncMock(return_value=(False, "original", None))
        self.endpoints = SimpleNamespace(
            _navigate_and_solve=self.original, remaining_ms=Mock(return_value=10000),
            challenge_present=AsyncMock(return_value=False), solve_challenge=AsyncMock(),
            _wait_for_networkidle=AsyncMock())
        self.page = SimpleNamespace(url="https://www.vinted.fr/catalog",
                                    goto=AsyncMock(return_value=SimpleNamespace(status=200)),
                                    wait_for_load_state=AsyncMock(),
                                    content=AsyncMock(return_value=CATALOG))
        self.dep = SimpleNamespace(page=self.page)
        self.request = SimpleNamespace(url="https://www.vinted.fr/catalog")
        self.timer = object()
        install_catalog_readiness(self.endpoints)

    async def test_catalog_links_alone_require_settling_before_checkpoint_use(self):
        challenge, body, response = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.assertFalse(challenge)
        self.assertEqual(body, CATALOG)
        self.assertEqual(response.status, 200)
        self.endpoints._wait_for_networkidle.assert_awaited_once()
        self.page.goto.assert_awaited_once_with(self.request.url, wait_until="domcontentloaded", timeout=10000)

    async def test_streaming_catalog_returns_late_cards_and_pagination(self):
        settled = CATALOG + '<a href="/items/456-shoes">More</a><nav>next page</nav>'
        self.page.content.side_effect = [CATALOG, settled]
        result = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.assertEqual(result[1], settled)
        self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_shell_waits_and_returns_updated_html(self):
        self.page.content.side_effect = ['<title>Vinted</title>', CATALOG]
        result = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.assertEqual(result[1], CATALOG)
        self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_challenge_keeps_existing_solver_and_settling_path(self):
        self.endpoints.challenge_present.return_value = True
        result = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.assertEqual(result[:2], (True, None))
        self.endpoints.solve_challenge.assert_awaited_once_with(self.page, self.timer)
        self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_other_domains_use_the_original_handler(self):
        self.request.url = "https://google.com/"
        self.assertEqual(await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer),
                         (False, "original", None))
        self.page.goto.assert_not_awaited()
        self.original.assert_awaited_once_with(self.dep, self.request, self.timer)

    async def test_late_challenge_is_checked_after_incomplete_page_loads(self):
        self.page.content.return_value = '<title>Just a moment</title>'
        self.endpoints.challenge_present.side_effect = [False, True]
        result = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.assertEqual(result[:2], (True, None))
        self.page.wait_for_load_state.assert_awaited_once_with("load", timeout=10000)
        self.endpoints.solve_challenge.assert_awaited_once_with(self.page, self.timer)

    async def test_wrong_region_does_not_take_readiness_shortcut(self):
        self.page.url = "https://www.vinted.de/catalog"
        await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_http_200_datadome_page_keeps_settling_path(self):
        self.page.content.return_value = CATALOG + '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>'
        await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_complete_item_returns_without_waiting_for_background_requests(self):
        self.request.url = self.page.url = "https://www.vinted.fr/items/123-shoes"
        self.page.content.return_value = item_page()
        result = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.assertEqual(result[1], item_page())
        self.endpoints._wait_for_networkidle.assert_not_awaited()

    async def test_incomplete_item_data_keeps_settling_path(self):
        self.request.url = self.page.url = "https://www.vinted.fr/items/123"
        for options in ({"gallery": False}, {"description": False}, {"reviews": False},
                        {"publication": False}, {"identity": "124"}):
            with self.subTest(options=options):
                self.endpoints._wait_for_networkidle.reset_mock()
                self.page.content.side_effect = [item_page(**options), item_page(**options), item_page()]
                result = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
                self.assertEqual(result[1], item_page())
                self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_item_completed_during_load_keeps_same_proof_without_networkidle(self):
        for domain in ("fr", "it", "de", "co.uk"):
            for brand in ("Nike", "Gucci"):
                with self.subTest(domain=domain, brand=brand):
                    self.request.url = self.page.url = f"https://www.vinted.{domain}/items/123-shirt"
                    self.endpoints._wait_for_networkidle.reset_mock()
                    self.page.goto.reset_mock()
                    ready = item_page().replace("Shoes", brand)
                    self.page.content.side_effect = [item_page(description=False), ready]
                    result = await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
                    self.assertEqual(result[1], ready)
                    self.page.goto.assert_awaited_once()
                    self.endpoints._wait_for_networkidle.assert_not_awaited()

    async def test_post_load_redirect_and_challenge_cannot_use_the_shortcut(self):
        self.request.url = "https://www.vinted.fr/items/123"
        for final_url, body in (("https://www.vinted.fr/items/124", item_page()),
                                (self.request.url, item_page() + '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>')):
            with self.subTest(final_url=final_url):
                self.page.url = final_url
                self.endpoints._wait_for_networkidle.reset_mock()
                self.page.content.side_effect = [item_page(description=False), body, body]
                await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
                self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_wrong_item_redirect_does_not_take_shortcut(self):
        self.request.url = "https://www.vinted.fr/items/123"
        self.page.url = "https://www.vinted.fr/items/124"
        self.page.content.return_value = item_page()
        await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.endpoints._wait_for_networkidle.assert_awaited_once()

    async def test_item_challenge_keeps_settling_path(self):
        self.request.url = self.page.url = "https://www.vinted.fr/items/123"
        self.page.content.return_value = item_page() + '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>'
        await self.endpoints._navigate_and_solve(self.dep, self.request, self.timer)
        self.endpoints._wait_for_networkidle.assert_awaited_once()

    def test_item_requires_json_not_text_markers_or_recommended_items(self):
        self.assertFalse(item_data_ready('photos description feedback_count item_id=123', '123'))
        self.assertFalse(item_data_ready(item_page("124"), "123"))
        self.assertFalse(item_data_ready('<script>self.__next_f.push(invalid)</script>', '123'))


if __name__ == "__main__":
    unittest.main()
