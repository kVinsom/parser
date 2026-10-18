import asyncio
import unittest
from unittest.mock import AsyncMock

from vpars_shared.http_cancellation import CancelOnDisconnect


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_application_and_finishes_cleanup_once(self):
        incoming = asyncio.Queue()
        started, stopped = asyncio.Event(), asyncio.Event()

        async def application(scope, receive, send):
            self.assertEqual((await receive())["body"], b"request")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                stopped.set()

        await incoming.put({"type": "http.request", "body": b"request", "more_body": False})
        send = AsyncMock()
        task = asyncio.create_task(CancelOnDisconnect(application)(
            {"type": "http"}, incoming.get, send))
        await started.wait()
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 1)
        self.assertTrue(stopped.is_set())
        send.assert_not_awaited()

    async def test_streamed_body_and_normal_response_keep_their_order(self):
        incoming = asyncio.Queue()
        for body, more in ((b"first", True), (b"second", False)):
            await incoming.put({"type": "http.request", "body": body, "more_body": more})

        async def application(scope, receive, send):
            self.assertEqual((await receive())["body"], b"first")
            self.assertEqual((await receive())["body"], b"second")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        send = AsyncMock()
        await asyncio.wait_for(CancelOnDisconnect(application)(
            {"type": "http"}, incoming.get, send), 1)
        self.assertEqual(send.await_count, 2)

    async def test_application_error_is_preserved_and_receiver_is_cleaned_up(self):
        async def application(scope, receive, send):
            raise ValueError("controlled failure")

        with self.assertRaisesRegex(ValueError, "controlled failure"):
            await CancelOnDisconnect(application)(
                {"type": "http"}, asyncio.Queue().get, AsyncMock())

    async def test_lifespan_and_websocket_are_untouched(self):
        for kind in ("lifespan", "websocket"):
            app = AsyncMock(return_value="handled")
            self.assertEqual(await CancelOnDisconnect(app)(
                {"type": kind}, AsyncMock(), AsyncMock()), "handled")
            app.assert_awaited_once()
