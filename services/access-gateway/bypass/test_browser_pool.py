"""Lifecycle and cleanup guarantees for reusable browsers with isolated contexts."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from browser_pool import SharedBrowser, bind_browser_lifespan


class BrowserPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_preserves_existing_state_and_closes_on_error(self):
        events = []
        @asynccontextmanager
        async def original(app):
            events.append("start")
            try:
                yield {"existing": True}
            finally:
                events.append("stop")
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=original))
        pool = SimpleNamespace(close=AsyncMock())
        bind_browser_lifespan(app, pool)
        with self.assertRaises(ValueError):
            async with app.router.lifespan_context(app) as state:
                self.assertEqual(state, {"existing": True})
                raise ValueError()
        self.assertEqual(events, ["start", "stop"])
        pool.close.assert_awaited_once()

    def setUp(self):
        self.contexts = []
        self.managers = []

        async def new_context():
            context = SimpleNamespace(close=AsyncMock(), cookies={},
                                      new_page=AsyncMock(return_value=SimpleNamespace(close=AsyncMock())))
            self.contexts.append(context)
            return context

        def factory():
            browser = SimpleNamespace(is_connected=Mock(return_value=True), new_context=AsyncMock(side_effect=new_context))
            manager = SimpleNamespace(__aenter__=AsyncMock(return_value=browser), __aexit__=AsyncMock())
            self.managers.append(manager)
            return manager

        self.factory = Mock(side_effect=factory)
        self.pool = SharedBrowser(self.factory, max_uses=2)
        self.addAsyncCleanup(self.pool.close)

    async def test_reuses_process_but_never_context_or_cookies(self):
        async with self.pool.context() as first:
            first.cookies["session"] = "private"
        async with self.pool.context() as second:
            self.assertIsNot(first, second)
            self.assertEqual(second.cookies, {})
        self.assertEqual(self.factory.call_count, 1)
        for context in self.contexts:
            context.close.assert_awaited_once()
        self.assertEqual(self.pool.active, 0)

    async def test_explicit_regional_context_reuses_only_within_its_generation(self):
        self.pool.max_uses = 4
        async with self.pool.context(key="DE") as first:
            first.cookies["anonymous"] = "DE"
            async with self.pool.context(key="DE") as concurrent:
                self.assertIs(first, concurrent)
        async with self.pool.context(key="FR") as other:
            self.assertIsNot(first, other)
            self.assertEqual(other.cookies, {})
        async with self.pool.context() as ordinary:
            self.assertIsNot(first, ordinary)
            self.assertEqual(ordinary.cookies, {})
        first.close.assert_not_awaited()
        async with self.pool.context(key="DE") as renewed:
            self.assertIsNot(first, renewed)
            self.assertEqual(renewed.cookies, {})
        self.managers[0].__aexit__.assert_awaited_once()

    async def test_rotates_after_use_limit_when_idle(self):
        for _ in range(3):
            async with self.pool.context():
                pass
        self.assertEqual(self.factory.call_count, 2)
        self.managers[0].__aexit__.assert_awaited_once()

    async def test_rotation_never_kills_an_active_context(self):
        async with self.pool.context():
            async with self.pool.context():
                async with self.pool.context():
                    self.assertEqual(self.factory.call_count, 2)
                    self.assertEqual(self.pool.active, 3)
                    self.managers[0].__aexit__.assert_not_awaited()
                self.managers[0].__aexit__.assert_not_awaited()
            self.managers[0].__aexit__.assert_not_awaited()
        self.managers[0].__aexit__.assert_awaited_once()
        async with self.pool.context():
            self.assertEqual(self.factory.call_count, 2)

    async def test_slow_retired_generation_cannot_spawn_unbounded_browsers(self):
        async with self.pool.context():
            for _ in range(10):
                async with self.pool.context():
                    pass
            self.assertEqual(self.factory.call_count, 2)
            self.managers[0].__aexit__.assert_not_awaited()
        self.managers[0].__aexit__.assert_awaited_once()
        async with self.pool.context():
            self.assertEqual(self.factory.call_count, 3)

    async def test_disconnected_browser_is_replaced_on_next_request(self):
        async with self.pool.context():
            self.pool.browser.is_connected.return_value = False
        async with self.pool.context():
            self.assertEqual(self.factory.call_count, 2)

    async def test_context_is_closed_after_error_and_cancellation(self):
        for error in (ValueError(), asyncio.CancelledError()):
            with self.assertRaises(type(error)):
                async with self.pool.context():
                    raise error
            self.assertEqual(self.pool.active, 0)
            self.contexts[-1].close.assert_awaited_once()

    async def test_context_creation_failure_does_not_leak_active_count(self):
        async with self.pool.context():
            self.pool.browser.new_context.side_effect = RuntimeError()
        with self.assertRaises(RuntimeError):
            async with self.pool.context():
                pass
        self.assertEqual(self.pool.active, 0)

    async def test_cancelled_tab_creation_preserves_warm_catalog_and_closes_late_tab(self):
        self.pool.max_uses = 50
        async with self.pool.page(key='FR') as session:
            context = session.context
            context.cookies['catalog'] = 'warm'
        entered, finish = asyncio.Event(), asyncio.Event()
        orphan = SimpleNamespace(close=AsyncMock())

        async def new_page():
            entered.set()
            await finish.wait()
            return orphan

        context.new_page.side_effect = new_page
        pending = asyncio.create_task(self.pool.page(key='FR').__aenter__())
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertLess(self.pool.current.uses, 50)
        context.new_page.side_effect = None
        async with self.pool.page(key='FR') as reused:
            self.assertIs(reused.context, context)
            self.assertEqual(reused.context.cookies, {'catalog':'warm'})
        finish.set()
        await asyncio.gather(*list(self.pool.cleanups))
        orphan.close.assert_awaited_once()
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.pool.active, 0)

    async def test_cancelled_isolated_context_creation_does_not_restart_catalogs(self):
        self.pool.max_uses = 50
        async with self.pool.page(key='DE') as session:
            catalog = session.context
        entered, finish = asyncio.Event(), asyncio.Event()
        orphan = SimpleNamespace(close=AsyncMock())

        async def new_context():
            entered.set()
            await finish.wait()
            return orphan

        self.pool.browser.new_context.side_effect = new_context
        pending = asyncio.create_task(self.pool.page().__aenter__())
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        async with self.pool.page(key='DE') as reused:
            self.assertIs(reused.context, catalog)
        finish.set()
        await asyncio.gather(*list(self.pool.cleanups))
        orphan.close.assert_awaited_once()
        self.assertEqual(self.factory.call_count, 1)

    async def test_cancelled_wait_for_context_lock_does_not_retire_browser(self):
        self.pool.max_uses = 50
        async with self.pool.page(key='UK') as session:
            catalog = session.context
        async with self.pool.current.context_lock:
            pending = asyncio.create_task(self.pool.page(key='IT').__aenter__())
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        async with self.pool.page(key='UK') as reused:
            self.assertIs(reused.context, catalog)
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.pool.active, 0)

    async def test_hung_context_cleanup_releases_generation_and_rotates(self):
        self.pool.cleanup_timeout = 0.01
        async with self.pool.context() as context:
            context.close.side_effect = asyncio.Event().wait
        self.assertEqual(self.pool.active, 0)
        async with self.pool.context():
            self.assertEqual(self.factory.call_count, 2)
        self.managers[0].__aexit__.assert_awaited_once()

    async def test_full_request_deadline_releases_a_retired_generation(self):
        entered = asyncio.Event()
        async def stuck_response():
            with self.assertRaises(TimeoutError):
                async with self.pool.context(timeout=0.05):
                    entered.set()
                    await asyncio.Event().wait()
        task = asyncio.create_task(stuck_response())
        await entered.wait()
        for _ in range(2):
            async with self.pool.context():
                pass
        self.assertEqual(len(self.managers), 2)
        self.managers[0].__aexit__.assert_not_awaited()
        await task
        self.managers[0].__aexit__.assert_awaited_once()
        self.assertEqual(self.pool.active, 0)
        self.assertEqual(self.pool.retired, [])

    async def test_launch_failure_cleans_up_and_next_request_can_recover(self):
        broken = SimpleNamespace(__aenter__=AsyncMock(side_effect=RuntimeError()), __aexit__=AsyncMock())
        factory = self.factory.side_effect
        self.factory.side_effect = [broken, factory()]
        with self.assertRaises(RuntimeError):
            async with self.pool.context():
                pass
        broken.__aexit__.assert_awaited_once()
        async with self.pool.context():
            self.assertEqual(self.pool.active, 1)

    async def test_shutdown_closes_once_and_rejects_new_work(self):
        async with self.pool.context():
            pass
        await self.pool.close()
        await self.pool.close()
        self.managers[0].__aexit__.assert_awaited_once()
        with self.assertRaises(RuntimeError):
            async with self.pool.context():
                pass

    async def test_concurrent_first_requests_only_launch_one_process(self):
        both_ready = asyncio.Event()
        count = 0

        async def request():
            nonlocal count
            async with self.pool.context():
                count += 1
                if count == 2:
                    both_ready.set()
                await asyncio.wait_for(both_ready.wait(), 2)

        await asyncio.gather(request(), request())
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(len(self.contexts), 2)

    async def test_cancelled_rotation_finishes_driver_and_display_cleanup(self):
        async with self.pool.context():
            pass
        self.pool.max_uses = 1
        closing, finish = asyncio.Event(), asyncio.Event()
        self.addCleanup(finish.set)
        released = []

        async def teardown(*args):
            closing.set()
            await finish.wait()
            released.extend(['driver', 'display'])

        self.managers[0].__aexit__.side_effect = teardown

        async def rotate():
            async with self.pool.context():
                pass

        task = asyncio.create_task(rotate())
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        finish.set()
        await self.pool.close()
        self.assertEqual(released, ['driver', 'display'])
        self.managers[0].__aexit__.assert_awaited_once()

    async def test_request_timeout_during_context_close_does_not_abandon_cleanup(self):
        closing, finish = asyncio.Event(), asyncio.Event()
        self.addCleanup(finish.set)
        closed = []

        async def close_context():
            closing.set()
            await finish.wait()
            closed.append(True)

        async def request():
            async with self.pool.context(timeout=0.03) as context:
                context.close.side_effect = close_context

        task = asyncio.create_task(request())
        await asyncio.wait_for(closing.wait(), 1)
        with self.assertRaises(TimeoutError):
            await task
        finish.set()
        await self.pool.close()
        self.assertEqual(closed, [True])
        self.assertEqual(self.pool.active, 0)

    async def test_retired_teardown_does_not_lock_current_generation(self):
        closing, finish = asyncio.Event(), asyncio.Event()
        self.addCleanup(finish.set)
        old = self.pool.context(key='FR')
        await old.__aenter__()
        self.pool.max_uses = 1
        async with self.pool.context(key='DE'):
            pass

        async def teardown(*args):
            closing.set()
            await finish.wait()

        self.managers[0].__aexit__.side_effect = teardown
        release = asyncio.create_task(old.__aexit__(None, None, None))
        await asyncio.wait_for(closing.wait(), 1)
        try:
            async with asyncio.timeout(0.1):
                async with self.pool.context(key='DE'):
                    self.assertEqual(self.factory.call_count, 2)
        finally:
            finish.set()
            await release

    async def test_page_timeout_finishes_closing_replacement_tab(self):
        closing, finish = asyncio.Event(), asyncio.Event()
        self.addCleanup(finish.set)
        replacement = SimpleNamespace(close=AsyncMock())

        async def close_page():
            closing.set()
            await finish.wait()

        replacement.close.side_effect = close_page

        async def request():
            async with asyncio.timeout(None) as deadline:
                async with self.pool.page(key="IT") as session:
                    # Test cancellation during cleanup, independently of how
                    # long resource acquisition takes on a busy test runner.
                    deadline.reschedule(asyncio.get_running_loop().time() + 0.03)
                    session.page = replacement

        pending = asyncio.create_task(request())
        await asyncio.wait_for(closing.wait(), 1)
        with self.assertRaises(TimeoutError):
            await pending
        self.assertEqual(self.pool.active, 1)
        finish.set()
        await self.pool.close()
        replacement.close.assert_awaited_once()
        self.assertEqual(self.pool.active, 0)

    async def test_bad_page_retires_its_original_generation_during_rotation(self):
        old = self.pool.page(key="UK")
        session = await old.__aenter__()
        old_generation = self.pool.current
        self.pool.max_uses = 1
        async with self.pool.page(key="DE"):
            pass
        current = self.pool.current
        current.uses = 0
        session.page.close.side_effect = RuntimeError("disconnected tab")
        await old.__aexit__(None, None, None)
        self.assertEqual(old_generation.uses, 1)
        self.assertEqual(current.uses, 0)
        self.assertTrue(old_generation.closed)

    async def test_cancelled_regional_creation_does_not_lock_other_generation(self):
        self.pool.max_uses = 3
        old = self.pool.context(key="FR")
        await old.__aenter__()
        started = asyncio.Event()

        async def slow_context():
            started.set()
            await asyncio.Event().wait()

        self.pool.browser.new_context.side_effect = slow_context

        async def request():
            with self.assertRaises(asyncio.CancelledError):
                async with self.pool.context(key="IT"):
                    pass

        pending = asyncio.create_task(request())
        await started.wait()
        self.pool.max_uses = 2
        try:
            async with asyncio.timeout(0.5):
                async with self.pool.context(key="DE"):
                    pass
        finally:
            pending.cancel()
            await pending
            await old.__aexit__(None, None, None)
        self.assertEqual(self.pool.active, 0)

    async def test_cancelled_launch_keeps_cleanup_without_extending_request_deadline(self):
        started, finish = asyncio.Event(), asyncio.Event()
        self.addCleanup(finish.set)

        async def enter():
            started.set()
            await asyncio.Event().wait()

        async def exit(*args):
            await finish.wait()

        broken = SimpleNamespace(__aenter__=AsyncMock(side_effect=enter),
                                 __aexit__=AsyncMock(side_effect=exit))
        self.factory.side_effect = [broken]
        async with asyncio.timeout(0.2):
            with self.assertRaises(TimeoutError):
                async with self.pool.context(timeout=0.02):
                    pass
        self.assertTrue(started.is_set())
        self.assertEqual(len(self.pool.retired), 1)
        self.assertFalse(self.pool.retired[0].closed)
        finish.set()
        await self.pool.close()
        broken.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
