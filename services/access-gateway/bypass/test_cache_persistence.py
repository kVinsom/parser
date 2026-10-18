import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from disk_cache import DiskAssetStore
from static_cache import StaticAssetCache, asset_key
from test_static_cache import HEADERS, URL, request


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_preserves_body_cors_and_absolute_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = StaticAssetCache(disk_dir=directory, clock=lambda: 0, wall_clock=lambda: 100)
            req = request()
            headers = HEADERS | {"cache-control": "public, immutable, max-age=60"}
            await cache.capture(req, SimpleNamespace(status=200, url=URL, headers=headers,
                                body=AsyncMock(return_value=b"asset")), asset_key(req))
            restored = StaticAssetCache(disk_dir=directory, clock=lambda: 5, wall_clock=lambda: 125)
            entry = restored.get(asset_key(req))
            self.assertEqual(entry.body, b"asset")
            self.assertEqual(entry.expires, 40)
            self.assertEqual(entry.headers["access-control-allow-origin"], headers["access-control-allow-origin"])
            self.assertEqual(restored.stats["disk_loaded"], 1)
            expired = StaticAssetCache(disk_dir=directory, wall_clock=lambda: 161)
            self.assertFalse(expired.entries)
            self.assertFalse(list(Path(directory).glob("*.json")))

    async def test_corrupt_private_and_wrong_scope_disk_entries_are_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            disk = DiskAssetStore(directory)
            key = asset_key(request())
            for headers, body in ((HEADERS | {"set-cookie": "private"}, b"private"),
                                  (HEADERS | {"cache-control": "private,max-age=100"}, b"private")):
                disk.save(key, body, headers, 60, 100)
                self.assertFalse(StaticAssetCache(disk_dir=directory, wall_clock=lambda: 101).entries)
            other = ("https://www.vinted.fr/catalog", "script", "")
            disk.save(other, b"catalog", HEADERS, 60, 100)
            self.assertFalse(StaticAssetCache(disk_dir=directory, wall_clock=lambda: 101).entries)
            disk.save(key, b"correct", HEADERS, 60, 100)
            path = Path(directory) / disk.name(key)
            data = json.loads(path.read_text())
            data["sha256"] = "wrong"
            path.write_text(json.dumps(data))
            self.assertFalse(StaticAssetCache(disk_dir=directory, wall_clock=lambda: 101).entries)

    def test_disk_limits_and_unavailable_disk_do_not_break_memory_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            disk = DiskAssetStore(directory, max_entries=2, max_bytes=10000)
            for i in range(4):
                disk.save((str(i),), b"asset", HEADERS, 60, 100)
            self.assertEqual(len(disk.files()), 2)
            self.assertFalse(list(Path(directory).glob("*.tmp")))
            occupied = Path(directory) / "file"
            occupied.write_text("existing")
            cache = StaticAssetCache(disk_dir=occupied)
            self.assertIsNone(cache.disk)
            cache.put("key", b"ok", HEADERS, 60)
            self.assertEqual(cache.get("key").body, b"ok")

    async def handler(self, cache):
        page = SimpleNamespace(route=AsyncMock())
        await cache.install(page)
        return page.route.call_args.args[1]

    def route(self, *, headers=HEADERS):
        response = SimpleNamespace(status=200, url=URL, headers=headers,
                                   body=AsyncMock(return_value=b"asset"), dispose=AsyncMock())
        return SimpleNamespace(request=request(), fetch=AsyncMock(return_value=response),
                               fulfill=AsyncMock(), abort=AsyncMock())

    async def test_simultaneous_public_resource_has_one_download(self):
        cache = StaticAssetCache()
        handler = await self.handler(cache)
        routes = [self.route() for _ in range(4)]
        original = routes[0].fetch.return_value
        started = asyncio.Event()
        async def fetch(**kwargs):
            started.set()
            await asyncio.sleep(0.15)  # Exceeds the old 100-ms duplicate-download threshold.
            return original
        routes[0].fetch.side_effect = fetch
        first = asyncio.create_task(handler(routes[0]))
        await started.wait()
        await asyncio.gather(first, *(handler(route) for route in routes[1:]))
        self.assertEqual(sum(r.fetch.await_count for r in routes), 1)
        self.assertEqual(cache.stats["coalesced"], 3)
        self.assertFalse(cache.pending)

    async def test_private_response_is_not_shared_and_cancelled_owner_releases_waiters(self):
        for cancelled in (False, True):
            cache = StaticAssetCache()
            handler = await self.handler(cache)
            first, second = self.route(headers=HEADERS | {"set-cookie": "session"}), self.route()
            started, finish = asyncio.Event(), asyncio.Event()
            response = first.fetch.return_value
            async def fetch(*, started=started, finish=finish, response=response, **kwargs):
                started.set()
                await finish.wait()
                return response
            first.fetch.side_effect = fetch
            owner = asyncio.create_task(handler(first))
            await started.wait()
            follower = asyncio.create_task(handler(second))
            await asyncio.sleep(0)
            if cancelled:
                owner.cancel()
            else:
                finish.set()
            await asyncio.gather(owner, follower, return_exceptions=True)
            second.fetch.assert_awaited_once()
            self.assertFalse(cache.pending)

    async def test_slow_owner_shares_download_but_other_origins_stay_independent(self):
        cache = StaticAssetCache()
        handler = await self.handler(cache)
        first, second, other = self.route(), self.route(), self.route()
        started = asyncio.Event()
        async def slow(**kwargs):
            started.set()
            await asyncio.Event().wait()
        first.fetch.side_effect = slow
        owner = asyncio.create_task(handler(first))
        await started.wait()
        other.request = request(origin="https://www.vinted.de")
        await asyncio.wait_for(handler(other), timeout=0.05)
        follower = asyncio.create_task(handler(second))
        await asyncio.sleep(0.15)
        second.fetch.assert_not_awaited()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        await asyncio.wait_for(follower, timeout=0.3)
        second.fetch.assert_awaited_once()
        self.assertEqual(cache.stats["coalesce_timeout"], 0)

    async def test_stuck_owner_does_not_authorize_parallel_download(self):
        cache = StaticAssetCache(coalesce_timeout=0.02)
        handler = await self.handler(cache)
        first, second = self.route(), self.route()
        started = asyncio.Event()

        async def slow(**kwargs):
            started.set()
            await asyncio.Event().wait()

        first.fetch.side_effect = slow
        owner = asyncio.create_task(handler(first))
        try:
            await started.wait()
            await asyncio.wait_for(handler(second), timeout=0.2)
            second.abort.assert_awaited_once()
            second.fetch.assert_not_awaited()
            self.assertEqual(cache.stats["coalesce_timeout"], 1)
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        self.assertFalse(cache.pending)
