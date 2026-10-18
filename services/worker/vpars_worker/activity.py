"""Keep source I/O alive only while an eligible user has requested search."""

import asyncio
import logging

from .parser import ParseFailure

logger = logging.getLogger("vpars.monitor")


class SourceActivity:
    def __init__(self, has_searches, *, poll_interval=1):
        self.has_searches = has_searches
        self.poll_interval = poll_interval
        self.active = None
        self.unavailable = False
        self.lock = asyncio.Lock()
        self.requests = set()

    async def _refresh(self):
        # Serialize snapshots and registration so an old lookup cannot restart
        # traffic after a newer Stop, or miss an operation about to start.
        try:
            async with asyncio.timeout(2):
                active = await self.has_searches()
        except Exception:  # A failed demand lookup cannot authorize paid traffic.
            active = False
            if not self.unavailable:
                logger.warning("Search activity unavailable; source traffic paused")
            self.unavailable = True
        else:
            self.unavailable = False
        if not active:
            for task in self.requests:
                if not task.done() and not task.cancelling():
                    task.cancel()
        if active != self.active:
            logger.info(
                "Source traffic %s: %s",
                "resumed" if active else "paused",
                "eligible searches available" if active else "no eligible active searches",
            )
        self.active = active
        return active

    async def refresh(self):
        async with self.lock:
            return await self._refresh()

    async def run(self, operation):
        async with self.lock:
            if not await self._refresh():
                raise ParseFailure("SearchIdle", retry_after=1, deferred=True)
            task = asyncio.create_task(operation())
            self.requests.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            # A last-user Stop cancels only source I/O. Its caller can settle its
            # job/queue lease normally, without treating this as a failed fetch.
            if asyncio.current_task().cancelling():
                raise
            raise ParseFailure("SearchIdle", retry_after=1, deferred=True) from None
        finally:
            self.requests.discard(task)

    async def watch(self):
        while True:
            await self.refresh()
            await asyncio.sleep(self.poll_interval)
