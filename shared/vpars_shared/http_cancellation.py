"""Cancel HTTP work when its caller disconnects, with one bounded receive queue."""

import asyncio


class CancelOnDisconnect:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages = asyncio.Queue(maxsize=1)
        disconnected = asyncio.Event()

        async def pump():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    return
                await messages.put(message)

        async def replay():
            if disconnected.is_set():
                return {"type": "http.disconnect"}
            return await messages.get()

        application = asyncio.create_task(self.app(scope, replay, send))
        receiver = asyncio.create_task(pump())
        lost = asyncio.create_task(disconnected.wait())
        tasks = (application, receiver, lost)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                await receiver  # Surface receive failures instead of hiding them.
            if application in done:
                return await application
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
