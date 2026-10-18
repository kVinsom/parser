"""Reuse a browser; isolate ordinary requests and scope opted-in catalogs by region.

The caller provides the pinned Byparr browser factory; this module does not
change proxy, locale, browser preferences or navigation/challenge behavior.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class Generation:
    manager: object
    browser: object
    active: int = 0
    uses: int = 0
    closed: bool = False
    contexts: dict = field(default_factory=dict)
    context_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: object = None


def bind_browser_lifespan(app, pool):
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original(application) as state:
                yield state
        finally:
            await pool.close()

    app.router.lifespan_context = lifespan


class SharedBrowser:
    def __init__(self, factory, max_uses=50, cleanup_timeout=5):
        if type(max_uses) is not int or max_uses < 1:
            raise ValueError("max_uses must be a positive integer")
        self.factory = factory
        self.max_uses = max_uses
        self.cleanup_timeout = cleanup_timeout
        self.lock = asyncio.Lock()
        self.current = None
        self.retired = []
        self.closed = False
        self.cleanups = set()

    def _cleanup_task(self, coroutine):
        # Keep a strong reference after the HTTP request has timed out. Cancelling
        # __aexit__ skips Playwright's driver, Xvfb and process-reaper cleanup.
        task = asyncio.create_task(coroutine)
        self.cleanups.add(task)
        task.add_done_callback(self.cleanups.discard)
        return task

    @property
    def browser(self):
        return self.current.browser if self.current else None

    @property
    def active(self):
        return sum(g.active for g in [self.current, *self.retired] if g is not None)

    def _start_browser_close(self, generation):
        if generation.closing is None:
            generation.closing = self._cleanup_task(self._teardown(generation))
        return generation.closing

    async def _teardown(self, generation):
        try:
            await generation.manager.__aexit__(None, None, None)
        except Exception:
            logging.getLogger("uvicorn.error").exception("Browser teardown failed")
        finally:
            generation.closed = True
            # No await between checking/removing: retain the retiring generation
            # until teardown ends, keeping the two-browser limit during cleanup.
            if generation in self.retired:
                self.retired.remove(generation)

    async def _close_browser(self, generation):
        if generation is not None:
            await asyncio.shield(self._start_browser_close(generation))

    async def _acquire(self):
        while True:
            wait_for_close = None
            async with self.lock:
                if self.closed:
                    raise RuntimeError("Browser pool is shutting down")
                disconnected = self.browser is not None and not self.browser.is_connected()
                rotate = (self.current is not None
                          and self.current.uses >= self.max_uses and not self.retired)
                if self.current is not None and self.current.closing is not None:
                    wait_for_close = self.current
                elif disconnected or rotate:
                    logging.getLogger("uvicorn.error").info(
                        "Rotating browser uses=%s active=%s", self.current.uses, self.current.active)
                    if self.current.active:
                        if self.retired:
                            raise RuntimeError("Disconnected browser awaiting active cleanup")
                        self.retired.append(self.current)
                        self.current = None
                    else:
                        wait_for_close = self.current
                        self._start_browser_close(wait_for_close)
                if self.current is None and len(self.retired) >= 2:
                    # A cancelled launch can be cleaning up alongside an older
                    # active generation. Do not launch a third process tree.
                    wait_for_close = next(g for g in self.retired if g.closing is not None)
                if wait_for_close is None:
                    if self.current is None:
                        manager = self.factory()
                        try:
                            browser = await manager.__aenter__()
                        except BaseException as exc:
                            failed = Generation(manager, None)
                            self.retired.append(failed)
                            self._start_browser_close(failed)
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                            await self._close_browser(failed)
                            raise
                        self.current = Generation(manager, browser)
                    self.current.active += 1
                    self.current.uses += 1
                    return self.current
            # Old driver/display teardown must not hold the pool lock. A request
            # cancellation leaves the tracked teardown running to completion.
            await self._close_browser(wait_for_close)
            async with self.lock:
                if self.current is wait_for_close:
                    self.current = None

    async def _release(self, generation, context, key, page=None):
        try:
            target = context if key is None else page
            if target is not None:
                try:
                    await asyncio.wait_for(target.close(), timeout=self.cleanup_timeout)
                except Exception:
                    generation.uses = self.max_uses
                    logging.getLogger("uvicorn.error").warning(
                        "Context/page cleanup failed; retiring its browser after active requests")
        finally:
            async with self.lock:
                generation.active -= 1
                retired = generation in self.retired and generation.active == 0
            if retired:
                await self._close_browser(generation)

    @asynccontextmanager
    async def page(self, *, key=None):
        """Own a tab, including its replacement after an initial navigation race.

        The yielded holder lets the caller update .page; cleanup always closes
        the final tab in its original generation, even when rotation overlaps.
        """
        from types import SimpleNamespace
        holder = SimpleNamespace(page=None, context=None)
        async with self._context(key, holder) as context:
            holder.context = context
            holder.page = await self._new_resource(holder.generation, context.new_page)
            yield holder

    async def _new_resource(self, generation, create):
        # Playwright may create the remote resource after its Python caller is
        # cancelled. Keep ownership until it can be closed; retiring the entire
        # browser here also discarded every other region's catalog session.
        task = asyncio.create_task(create())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._cleanup_task(self._discard_created(generation, task))
            raise
        except Exception:
            generation.uses = self.max_uses
            raise

    async def _discard_created(self, generation, task):
        try:
            resource = await asyncio.wait_for(asyncio.shield(task), timeout=max(10, self.cleanup_timeout))
        except TimeoutError:
            generation.uses = self.max_uses
            # Retirement closes the driver even if resource creation is stuck.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            return
        except (Exception, asyncio.CancelledError):
            # An isolated parent context can already have closed this page.
            return
        try:
            await asyncio.wait_for(resource.close(), timeout=self.cleanup_timeout)
        except Exception:
            generation.uses = self.max_uses

    @asynccontextmanager
    async def context(self, *, timeout=None, key=None):
        if key is not None and key not in {"DE", "FR", "IT", "UK"}:
            raise ValueError("Unknown anonymous catalog session")
        # Cover acquisition, response extraction and dependency cleanup too;
        # browser-native navigation timers alone do not bound those operations.
        async with asyncio.timeout(timeout):
            async with self._context(key) as context:
                yield context

    @asynccontextmanager
    async def _context(self, key=None, holder=None):
        if key is not None and key not in {"DE", "FR", "IT", "UK"}:
            raise ValueError("Unknown anonymous catalog session")
        generation = await self._acquire()
        if holder is not None:
            holder.generation = generation
        context = None
        try:
            if key is None:
                context = await self._new_resource(generation, generation.browser.new_context)
            else:
                # Only the four anonymous, fixed-proxy regional catalogs opt in.
                # Browser generation rotation bounds session/resource lifetime.
                async with generation.context_lock:
                    context = generation.contexts.get(key)
                    if context is None:
                        context = await self._new_resource(generation, generation.browser.new_context)
                        generation.contexts[key] = context
            yield context
        finally:
            cleanup = self._cleanup_task(self._release(
                generation, context, key, holder.page if holder else None))
            await asyncio.shield(cleanup)

    async def close(self):
        async with self.lock:
            self.closed = True
            generations = [self.current, *self.retired]
        # Context cleanup may have outlived its timed-out request. Drain it before
        # tearing down the manager, and never cancel the manager halfway through.
        while self.cleanups:
            await asyncio.shield(asyncio.gather(*self.cleanups))
        for generation in generations:
            await self._close_browser(generation)
