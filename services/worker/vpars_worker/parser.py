import asyncio
import json
import logging
import math
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

from aiohttp import ClientSession, ClientTimeout
from vpars_parser.models import (
    Capabilities,
    ItemDetails,
    ItemDetailsRequest,
    ParseRequest,
    ParseResult,
)

from .freshness import relative_snapshot_ttl
from .matching import validate_result
from .source_rate import SourcePacer

logger = logging.getLogger("vpars.source_rate")
DETAIL_TIMEOUT_SECONDS = 20


class ParseFailure(Exception):
    def __init__(self, code, fields=(), retry_after=None, *, deferred=False):
        self.code = code if str(code).isalnum() else "ParserFailure"
        allowed = {
            "models",
            "sizes",
            "brands",
            "conditions",
            "colors",
            "categories",
            "price.currency",
        }
        self.fields = [field for field in fields if field in allowed]
        self.retry_after = (
            min(retry_after, 86400) if type(retry_after) is int and retry_after > 0 else None
        )
        # These responses reject work before checking the source. Keep that
        # distinction in both catalog and detail callers, including after retries.
        capacity_wait = self.code in {"ParserBusy", "GatewayResourceExhausted"}
        budget_wait = self.code in {"ProxyBudgetWait", "ProxyBudgetExceeded", "ProxyBudgetNotStarted", "ProxyBudgetUnavailable"}
        self.deferred = deferred or capacity_wait or budget_wait
        if capacity_wait and self.retry_after is None:
            self.retry_after = 2
        if budget_wait and self.retry_after is None:
            self.retry_after = 30
        if self.code == "ProxyBudgetWait":
            # Estimates can clear as another request settles. Recheck locally;
            # the shared gateway still authorizes every paid source operation.
            self.retry_after = min(2, self.retry_after)
        super().__init__(self.code)


class ParserClient:
    def __init__(self, url="http://127.0.0.1:8002", *, state_path=None, activity=None):
        self.url = url.rstrip("/")
        self.activity = activity
        self.http = ClientSession(timeout=ClientTimeout(total=80))
        self.capabilities = None
        self.expires = 0
        # Separate bounded lanes: a slow gallery must not stop catalog polling.
        self.catalog_access = asyncio.Semaphore(4)
        self.detail_access = asyncio.Semaphore(2)
        self.cooldowns = {}
        self.budget_pauses = {}
        self.pacer = SourcePacer()
        self.state_path = Path(state_path) if state_path is not None else None
        self.detail_cache = OrderedDict()
        self.detail_tasks = {}
        self.detail_waiters = {}
        self.restore_cooldowns()

    def restore_cooldowns(self):
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("regions"), dict)
            ):
                raise ValueError("Invalid source cooldown state")
            pacing = data.get("pacing", {})
            if not isinstance(pacing, dict):
                raise ValueError("Invalid source pacing state")
            for region, saved in pacing.items():
                if region not in {"UK", "FR", "IT", "DE"}:
                    continue
                interval = saved["interval"]
                if type(interval) not in {int, float} or not 1 <= interval <= 16:
                    raise ValueError("Invalid source interval")
                remaining = (
                    datetime.fromisoformat(saved["recover_after"]) - datetime.now(UTC)
                ).total_seconds()
                self.pacer.regions[region] = interval
                self.pacer.recover_after[region] = monotonic() + remaining
            for region, saved in data["regions"].items():
                if region not in {"UK", "FR", "IT", "DE"}:
                    continue
                until = datetime.fromisoformat(saved["until"])
                remaining = (until - datetime.now(UTC)).total_seconds()
                if saved["code"] not in {"AccessBlocked", "SourceRateLimited"}:
                    continue
                if remaining > 0:
                    self.cooldowns[region] = (saved["code"], monotonic() + remaining)
                    if region not in pacing:
                        self.pacer.limited(region)
            recovered = False
            for region in tuple(self.pacer.regions):
                # An explicit Retry-After always takes precedence, even when
                # the learned pacing checkpoint is much older than that pause.
                if self.cooldowns.get(region, (None, 0))[1] <= monotonic():
                    recovered = self.pacer.recover(region) or recovered
            if recovered:
                self.persist_cooldowns()
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            # A damaged checkpoint is not permission to resume a blocked source.
            for region in ("UK", "FR", "IT", "DE"):
                self.cooldowns[region] = ("SourceRateLimited", monotonic() + 120)
                self.pacer.limited(region)
            logger.warning("Source cooldown state unavailable; using a conservative pause")

    def persist_cooldowns(self):
        if self.state_path is None:
            return
        now = monotonic()
        saved = {
            region: {
                "code": code,
                "until": (datetime.now(UTC) + timedelta(seconds=until - now)).isoformat(),
            }
            for region, (code, until) in self.cooldowns.items()
            if until > now and code in {"AccessBlocked", "SourceRateLimited"}
        }
        pacing = {
            region: {
                "interval": interval,
                "recover_after": (
                    datetime.now(UTC)
                    + timedelta(seconds=self.pacer.recover_after[region] - now)
                ).isoformat(),
            }
            for region, interval in self.pacer.regions.items()
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"version": 1, "regions": saved, "pacing": pacing}), encoding="utf-8"
            )
            temporary.replace(self.state_path)
        except OSError:
            logger.error("Could not persist source cooldown; in-memory pause remains active")

    def check_cooldown(self, region, kind="catalog"):
        code, until = self.cooldowns.get("UK" if region == "GB" else region, (None, 0))
        if until > monotonic():
            raise ParseFailure(code, retry_after=math.ceil(until - monotonic()), deferred=True)
        until = self.budget_pauses.get(kind, 0)
        if until > monotonic():
            raise ParseFailure("ProxyBudgetWait", retry_after=math.ceil(until-monotonic()))

    def remember_failure(self, region, failure, kind="catalog"):
        if failure.code == "ProxyBudgetWait":
            self.budget_pauses[kind] = max(self.budget_pauses.get(kind, 0), monotonic() + failure.retry_after)
            return
        if failure.deferred:
            return
        if failure.code in {"AccessBlocked", "SourceRateLimited"} or failure.retry_after:
            region = "UK" if region == "GB" else region
            now = monotonic()
            until = now + (failure.retry_after or 120)
            if (
                failure.code in {"AccessBlocked", "SourceRateLimited"}
                and self.cooldowns.get(region, (None, 0))[1] <= now
            ):
                self.pacer.limited(region)
                logger.warning(
                    "Source pause region=%s code=%s seconds=%s next_interval=%s",
                    region,
                    failure.code,
                    failure.retry_after or 120,
                    self.pacer.regions[region],
                )
            # Concurrent catalog/detail responses may arrive out of order. A
            # shorter refusal cannot erase a still-active, longer Retry-After.
            if until > self.cooldowns.get(region, (None, 0))[1]:
                self.cooldowns[region] = (failure.code, until)
                self.persist_cooldowns()

    async def parse(self, request: ParseRequest):
        if self.activity is not None:
            return await self.activity.run(lambda: self._paced_parse(request))
        return await self._paced_parse(request)

    async def _paced_parse(self, request: ParseRequest):
        region = "UK" if request.region_id == "GB" else request.region_id
        async with self._source_slot(self.catalog_access, region, "catalog", request.request_id):
            try:
                result = await self._parse(request)
                if self.pacer.succeeded(region):
                    self.persist_cooldowns()
                return result
            except ParseFailure as exc:
                self.remember_failure(request.region_id, exc)
                raise

    @asynccontextmanager
    async def _source_slot(self, lane, region, kind, request_id):
        started = monotonic()
        acquired = paced = None
        try:
            async with lane:
                acquired = monotonic()
                if await self.pacer.acquire(region, lambda value: self.check_cooldown(value, kind)):
                    self.persist_cooldowns()
                paced = monotonic()
                yield
        finally:
            ended = monotonic()
            logger.info(
                "Source phases kind=%s region=%s request_id=%s queue_ms=%.1f pacing_ms=%.1f work_ms=%.1f total_ms=%.1f",
                kind, region, request_id, ((acquired if acquired is not None else ended) - started) * 1000,
                ((paced or ended) - acquired) * 1000 if acquired is not None else 0,
                (ended - paced) * 1000 if paced is not None else 0, (ended - started) * 1000,
            )

    async def _parse(self, request: ParseRequest):
        try:
            if self.capabilities is None or monotonic() >= self.expires:
                async with self.http.get(
                    self.url + "/v1/sources/vinted/capabilities", timeout=ClientTimeout(total=8)
                ) as response:
                    if response.status != 200:
                        raise ParseFailure("CapabilitiesUnavailable")
                    self.capabilities = Capabilities.model_validate(await response.json())
                    self.expires = monotonic() + 300
            caps = self.capabilities
            if request.newest_first and not caps.newest_first:
                raise ParseFailure("NewestSortUnsupported")
            region = caps.aliases.get(request.region_id, request.region_id)
            if request.source_id != caps.source_id or region not in {r.code for r in caps.regions}:
                raise ParseFailure("UnsupportedRegion")
            for field in type(request.filters).model_fields:
                if getattr(request.filters, field) and (
                    field not in caps.filters or caps.filters[field].mode != "residual"
                ):
                    raise ParseFailure("UnsupportedFilter")
            async with self.http.post(
                self.url + "/v1/catalog/parse", json=request.model_dump(mode="json")
            ) as response:
                payload = await response.json()
                if response.status != 200:
                    detail = payload.get("error", {})
                    raise ParseFailure(
                        detail.get("code", "ParserFailure"),
                        detail.get("fields", []),
                        retry_after=detail.get("retry_after"),
                    )
                result = ParseResult.model_validate(payload)
            validate_result(request, result)
            return result
        except ParseFailure:
            raise
        except Exception as exc:
            raise ParseFailure(type(exc).__name__) from None

    async def close(self):
        self.persist_cooldowns()
        tasks = list(self.detail_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.http.close()

    async def refresh_details(self, item):
        """Retry incomplete age evidence against the public HTML, bypassing the cache."""
        return await self.details(item, force_html=True)

    async def details(self, item, *, force_html=False):
        key = (item.source_id, item.region_id, item.source_item_id, force_html)
        cached = self.detail_cache.get(key)
        if cached and cached[0] > monotonic():
            self.detail_cache.move_to_end(key)
            return cached[1]
        task = self.detail_tasks.get(key)
        if task is None:

            def operation():
                return self._details(item, force_html=force_html)

            task = asyncio.create_task(
                self.activity.run(operation) if self.activity is not None else operation()
            )
            self.detail_tasks[key] = task

            def finished(done):
                if self.detail_tasks.get(key) is done:
                    self.detail_tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        self.detail_waiters[task] = self.detail_waiters.get(task, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            remaining = self.detail_waiters[task] - 1
            if remaining:
                self.detail_waiters[task] = remaining
            else:
                self.detail_waiters.pop(task, None)
                if not task.done():
                    # A timed-out recipient must not occupy a browser lane for
                    # another 80 seconds. Keep a shared fetch only while another
                    # recipient still needs it; no publication proof is renewed.
                    if self.detail_tasks.get(key) is task:
                        self.detail_tasks.pop(key, None)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def _details(self, item, *, force_html=False):
        request = ItemDetailsRequest(
            source_id=item.source_id,
            region_id=item.region_id,
            source_item_id=item.source_item_id,
            timeout_seconds=DETAIL_TIMEOUT_SECONDS,
            force_html=force_html,
        )
        try:
            region = "UK" if item.region_id == "GB" else item.region_id
            async with self._source_slot(self.detail_access, region, "details", request.request_id):
                async with self.http.post(
                    self.url + "/v1/items/details", json=request.model_dump(mode="json")
                ) as response:
                    data = await response.json()
                    if response.status != 200:
                        error = data.get("error", {})
                        failure = ParseFailure(
                            error.get("code", "ParserFailure"), retry_after=error.get("retry_after")
                        )
                        # Only an actual response starts a new cooldown. Reusing
                        # a local refusal for queued lots must not keep extending
                        # its deadline by rounded-up remaining seconds.
                        self.remember_failure(item.region_id, failure, "details")
                        raise failure
                    result = ItemDetails.model_validate(data)
            if (result.source_id, result.region_id, result.source_item_id) != (
                item.source_id,
                item.region_id,
                item.source_item_id,
            ):
                raise ParseFailure("ItemIdentityMismatch")
            if self.pacer.succeeded(region):
                self.persist_cooldowns()
            # Reuse simultaneous requests without extending the age snapshot's TTL.
            age = (datetime.now(UTC) - result.fetched_at).total_seconds()
            ttl = relative_snapshot_ttl(result.publication_evidence)
            if 0 <= age < ttl and result.publication_evidence != "unknown":
                key = (item.source_id, item.region_id, item.source_item_id, False)
                self.detail_cache[key] = (monotonic() + ttl - age, result)
                self.detail_cache.move_to_end(key)
                if force_html:
                    # A fresh HTML result can satisfy another HTML retry. API
                    # results never satisfy force_html, and fetched_at never moves.
                    html_key = (*key[:-1], True)
                    self.detail_cache[html_key] = self.detail_cache[key]
                    self.detail_cache.move_to_end(html_key)
                while len(self.detail_cache) > 128:
                    self.detail_cache.popitem(last=False)
            return result
        except ParseFailure:
            raise
        except Exception as exc:
            raise ParseFailure(type(exc).__name__) from None
