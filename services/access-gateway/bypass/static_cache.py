"""Bounded cache of explicitly public, immutable Vinted JS/CSS assets.

Use the originating route's request context unless a direct asset fetcher is supplied.
Never cache catalog/item/API data or share browser cookies. The pinned Firefox
cannot expose Brotli response bodies; route.fetch decodes them before replay.
"""

import asyncio
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from urllib.parse import urlsplit
from weakref import WeakSet

ASSET_HOST = "marketplace-web-assets.vinted.com"
ASSET_ROUTE = "https://marketplace-web-assets.vinted.com/_next/static/**"
MIME_TYPES = {
    "script": {"application/javascript", "text/javascript"},
    "stylesheet": {"text/css"},
}


def asset_key(request):
    url = urlsplit(request.url)
    headers = request.headers
    if (request.method != "GET" or request.resource_type not in MIME_TYPES
            or url.scheme != "https" or url.hostname != ASSET_HOST
            or url.netloc != ASSET_HOST or url.query or url.fragment
            or not url.path.startswith("/_next/static/")
            or not url.path.endswith(".js" if request.resource_type == "script" else ".css")
            or any(name in headers for name in ("authorization", "range", "if-range"))):
        return None
    # Assets advertise Vary: Origin. Keep regional CORS variants separate.
    return request.url, request.resource_type, headers.get("origin", "")


def lifetime(headers, now):
    directives = {}
    for part in headers.get("cache-control", "").lower().split(","):
        name, _, value = part.strip().partition("=")
        directives[name] = value.strip('"')
    if (not {"public", "immutable"} <= directives.keys()
            or {"private", "no-store", "no-cache"} & directives.keys()
            or "set-cookie" in headers):
        return 0
    vary = {value.strip().lower() for value in headers.get("vary", "").split(",") if value.strip()}
    if not vary <= {"accept-encoding", "origin"}:
        return 0
    try:
        age = max(0, int(headers.get("age", "0")))
        if "date" in headers:
            age = max(age, now - parsedate_to_datetime(headers["date"]).timestamp())
        return max(0, min(86400, int(directives.get("s-maxage", directives.get("max-age", "0"))) - age))
    except (ValueError, TypeError, OverflowError):
        return 0


@dataclass
class Asset:
    body: bytes
    headers: dict
    expires: float


class StaticAssetCache:
    def __init__(self, max_bytes=64 * 1024 * 1024, max_asset_bytes=4 * 1024 * 1024,
                 max_entries=512, clock=time.monotonic, wall_clock=time.time, disk_dir=None,
                 coalesce_timeout=10.5, direct_fetcher=None):
        self.max_bytes = max_bytes
        self.max_asset_bytes = min(max_asset_bytes, max_bytes)
        self.max_entries = max_entries
        self.clock, self.wall_clock = clock, wall_clock
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.stats = Counter()
        self.served_requests = WeakSet()
        self.disk = None
        self.disk_lock = asyncio.Lock()
        self.pending = {}
        self.coalesce_timeout = coalesce_timeout
        self.direct_fetcher = direct_fetcher
        self.direct_failures = OrderedDict()
        if disk_dir:
            try:
                from disk_cache import DiskAssetStore
                self.disk = DiskAssetStore(disk_dir, max_entries=max_entries)
                for key, body, headers, ttl in self.disk.load(self.wall_clock(), self.max_asset_bytes):
                    if self.valid_saved(key, headers):
                        ttl = min(ttl, lifetime(headers, self.wall_clock()))
                        if ttl > 0:
                            self.put(key, body, headers, ttl)
                            self.stats["disk_loaded"] += 1
            except (OSError, ValueError, TypeError):
                self.stats["disk_load_error"] += 1
                self.disk = None

    @staticmethod
    def valid_saved(key, headers):
        if (len(key) != 3 or not all(isinstance(v, str) for v in key)
                or not isinstance(headers, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())):
            return False
        request = SimpleNamespace(url=key[0], resource_type=key[1], method="GET",
                                  headers={"origin": key[2]})
        return (asset_key(request) == key and
                headers.get("content-type", "").partition(";")[0].strip().lower()
                in MIME_TYPES[key[1]])

    def get(self, key):
        entry = self.entries.get(key)
        if entry is None:
            return None
        if entry.expires <= self.clock():
            self.bytes -= len(self.entries.pop(key).body)
            return None
        self.entries.move_to_end(key)
        return entry

    def put(self, key, body, headers, ttl):
        if not body or len(body) > self.max_asset_bytes or ttl <= 0:
            return
        old = self.entries.pop(key, None)
        if old:
            self.bytes -= len(old.body)
        while self.entries and (self.bytes + len(body) > self.max_bytes
                                or len(self.entries) >= self.max_entries):
            self.bytes -= len(self.entries.popitem(last=False)[1].body)
        # response.body() is decoded; replaying the original Content-Encoding or
        # compressed Content-Length corrupts JS/CSS. Keep CORS/content headers.
        clean = {k: v for k, v in headers.items()
                 if k not in ("content-encoding", "content-length", "transfer-encoding")}
        self.entries[key] = Asset(body, clean, self.clock() + ttl)
        self.bytes += len(body)

    async def capture(self, request, response, key):
        if response.status != 200 or response.url != request.url:
            self.stats["status_or_redirect"] += 1
            return
        headers = response.headers
        ttl = lifetime(headers, self.wall_clock())
        mime = headers.get("content-type", "").partition(";")[0].strip().lower()
        if (ttl <= 0 or mime not in MIME_TYPES[request.resource_type]
                or int(headers.get("content-length", "0")) > self.max_asset_bytes):
            self.stats["ineligible_response"] += 1
            return
        self.put(key, await response.body(), headers, ttl)
        self.stats["captured"] += 1
        entry = self.get(key)
        if entry is not None and self.disk is not None:
            try:
                async with self.disk_lock:
                    remaining = entry.expires - self.clock()
                    if remaining > 0:
                        await asyncio.to_thread(self.disk.save, key, entry.body, entry.headers,
                                                remaining, self.wall_clock())
            except OSError:
                self.stats["disk_write_error"] += 1

    async def install(self, page):
        async def serve(route, entry):
            self.served_requests.add(route.request)
            await route.fulfill(status=200, headers=entry.headers, body=entry.body)
            self.hits += 1

        async def route_asset(route):
            key = asset_key(route.request)
            if key is None:
                await route.fallback()
                return
            entry = self.get(key) if key else None
            if entry is not None:
                await serve(route, entry)
                return
            if self.direct_fetcher is not None and self.direct_failures.get(key, 0) > self.clock():
                self.stats["direct_cooldown"] += 1
                await route.abort()
                return
            # Elect one downloader per public asset/origin. A slow owner is not
            # permission to download the same file in parallel after just 100 ms.
            while True:
                pending = self.pending.get(key)
                if pending is None:
                    owner = asyncio.Event()
                    self.pending[key] = owner
                    break
                try:
                    await asyncio.wait_for(pending.wait(), timeout=self.coalesce_timeout)
                except TimeoutError:
                    self.stats["coalesce_timeout"] += 1
                    await route.abort()
                    return
                entry = self.get(key)
                if entry is not None:
                    self.stats["coalesced"] += 1
                    await serve(route, entry)
                    return
                if self.direct_fetcher is not None and self.direct_failures.get(key, 0) > self.clock():
                    self.stats["direct_cooldown"] += 1
                    await route.abort()
                    return
                # Private/failed/cancelled results cannot be shared. Elect a new
                # owner only after the previous request has actually ended.
            response = None
            try:
                # Replace this one static request, never duplicate it or follow
                # redirects outside the cache scope. Page deadlines still apply.
                self.stats["network_misses"] += 1
                if self.direct_fetcher is None:
                    response = await route.fetch(timeout=10000, max_redirects=0, max_retries=0)
                else:
                    self.stats["direct_network_misses"] += 1
                    response = await self.direct_fetcher(route.request, self.max_asset_bytes)
                try:
                    await self.capture(route.request, response, key)
                except Exception as exc:
                    self.stats["read_error_" + type(exc).__name__] += 1
                if self.direct_fetcher is None:
                    await route.fulfill(response=response)
                else:
                    self.served_requests.add(route.request)
                    await route.fulfill(status=response.status, headers=response.headers, body=await response.body())
            except Exception as exc:
                self.stats["route_error_" + type(exc).__name__] += 1
                if self.direct_fetcher is not None:
                    self.direct_failures[key] = self.clock() + 60
                    self.direct_failures.move_to_end(key)
                    while len(self.direct_failures) > self.max_entries:
                        self.direct_failures.popitem(last=False)
                # Do not retry the network request. A missing asset keeps normal
                # downstream content/completeness validation in force.
                try:
                    await route.abort()
                except Exception:
                    pass  # The page may already be closed by its deadline.
            finally:
                if owner is not None:
                    if self.pending.get(key) is owner:
                        self.pending.pop(key, None)
                    owner.set()
                if response is not None:
                    try:
                        await response.dispose()
                    except Exception:
                        pass

        await page.route(ASSET_ROUTE, route_asset)


def install_static_cache(endpoints, cache):
    original = endpoints.setup_routes

    async def setup(request, dep):
        await original(request, dep)
        # The newest, narrow route can fall back to ordinary media blocking.
        await cache.install(dep.page)

    endpoints.setup_routes = setup
