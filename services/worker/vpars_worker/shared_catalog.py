"""A single regional catalog traversal with bounded, isolated subscriber fan-out."""

import asyncio
import logging
from copy import deepcopy
from datetime import timedelta
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters, UserError, euro_price
from vpars_bot.search_control import WorkToken
from vpars_parser.errors import ParserError
from vpars_parser.models import ParseRequest
from vpars_parser.sources.vinted_query import build_query

from .catchup import advance, begin, scope_key
from .db import Job, RegionalCatalog, RegionalIdentity
from .matching import validate_result
from .monitor import Monitor, Store, aware
from .parser import ParseFailure
from .scheduling import choose, head_timeout

logger = logging.getLogger("vpars.shared_catalog")
CURSOR_FIELDS = ("page", "baseline_ready", "baseline_pages", "checkpoint", "scan_state",
                 "scheduling", "coverage", "checks", "failures", "head_streak")


def regional_query(region):
    return {"source_id": "vinted", "region_id": "UK" if region == "GB" else region,
            "search_text": "", "filters": {}, "newest_first": True, "timeout_seconds": 45}


def cursor_object(feed, region):
    values = {"page": 1, "baseline_ready": False, "baseline_pages": [], "checkpoint": None,
              "scan_state": None, "scheduling": None, "coverage": None, "checks": 0,
              "failures": 0, "head_streak": 0, **deepcopy(feed.cursor or {})}
    return SimpleNamespace(**values, id=feed.id, query_key=feed.id, query=regional_query(region),
                           next_due=aware(feed.next_due))


def save_cursor(feed, cursor):
    feed.cursor = {name: getattr(cursor, name) for name in CURSOR_FIELDS}


def upgrade_job(job):
    if (job.scheduling or {}).get("shared_catalog_version") == 1:
        return
    old = {key: deepcopy(getattr(job, key)) for key in
           ("query", "page", "checkpoint", "scan_state", "scheduling", "coverage")}
    job.query = {**job.query, "search_text": "", "newest_first": True}
    job.query_key = scope_key(job.query)
    job.page, job.checkpoint, job.scan_state = 1, None, None
    job.scheduling = {"shared_catalog_version": 1, "previous_catalog": old}
    # Existing discovery eligibility, SeenItem timestamps and SentLot survive.
    job.coverage = "shared_feed" if job.baseline_ready else "baseline_pending"


class SharedStore(Store):
    async def refresh_queries(self):
        async with self.sessions.begin() as session:
            for job in await session.scalars(select(Job).where(Job.state == "waiting").with_for_update()):
                upgrade_job(job)

    async def recover_executions(self):
        await super().recover_executions()
        # The runtime singleton is acquired before recovery. Re-fetch an
        # interrupted page; recipient transactions and dedup make replay safe.
        async with self.sessions.begin() as session:
            for feed in await session.scalars(select(RegionalCatalog).with_for_update()):
                if feed.lease_id:
                    feed.lease_id = feed.lease_until = None
                    feed.next_due = utcnow()

    async def claim(self, region=None):
        if region is None:
            for candidate in ("FR", "IT", "DE", "UK"):
                jobs = await self.claim(candidate)
                if jobs:
                    return jobs
            return []
        region = "UK" if region == "GB" else region
        query = regional_query(region)
        key = scope_key(query)
        now = utcnow()
        async with self.sessions.begin() as session:
            await session.execute(insert(RegionalCatalog).values(id=key, cursor={}, next_due=now)
                                  .on_conflict_do_nothing(index_elements=[RegionalCatalog.id]))
            feed = await session.scalar(select(RegionalCatalog).where(RegionalCatalog.id == key)
                                        .with_for_update())
            if aware(feed.next_due) > now or (feed.lease_until and aware(feed.lease_until) > now):
                return []
            jobs = list(await session.scalars(select(Job).where(
                Job.query["region_id"].as_string().in_([region, "GB"] if region == "UK" else [region]),
                Job.state.in_(["waiting", "running"]),
                or_(Job.lease_until.is_(None), Job.lease_until <= now),
            ).order_by(Job.id).with_for_update(skip_locked=True)))
            ready = []
            for job in jobs:
                current = await self.current(session, WorkToken(**job.token))
                if current is None:
                    job.state = "cancelled"
                    job.lease_id = job.lease_until = None
                    continue
                upgrade_job(job)
                saved = Filters.model_validate(current[1].filters)
                try:
                    if euro_price(saved):
                        price = self.registry.for_source(saved, job.token["source_id"]).price
                        job.query = {**job.query, "filters": {**job.query["filters"],
                                    "price": price.model_dump(mode="json")}}
                    build_query(ParseRequest(**job.query), 45)
                except (UserError, ParserError) as exc:
                    job.last_error = "UnsupportedFilter" if isinstance(exc, ParserError) else "ExchangeRateUnavailable"
                    job.next_due = now + timedelta(seconds=30)
                    current[2].last_error = job.last_error
                    continue
                ready.append(job)
            if not ready:
                return []
            cursor = cursor_object(feed, region)
            if not choose([cursor], now, cursor.head_streak):
                return []
            feed.lease_id, feed.lease_until = str(uuid4()), now + timedelta(seconds=120)
            # The cursor advances only after fan-out; its selected page is saved
            # for crash recovery, independently of any subscriber's Start/Stop.
            save_cursor(feed, cursor)
            timeout = head_timeout([cursor]) if cursor.page == 1 else 24
            for job in ready:
                job.page = cursor.page
                job.state = "running"
                job.lease_id, job.lease_until = str(uuid4()), feed.lease_until
                job._feed_id, job._feed_lease = feed.id, feed.lease_id
                job._feed_timeout = timeout
            return ready

    async def finish(self, claimed, result, *, interval, max_pages):
        return await super().finish(claimed, result, interval=interval, max_pages=max_pages, shared=True)

    async def complete_feed(self, jobs, result, *, interval, max_pages, failure=None):
        first = jobs[0]
        now = utcnow()
        async with self.sessions.begin() as session:
            feed = await session.scalar(select(RegionalCatalog).where(RegionalCatalog.id == first._feed_id)
                                        .with_for_update())
            if feed.lease_id != first._feed_lease:
                return
            if not feed.lease_until or aware(feed.lease_until) <= now:
                feed.lease_id = feed.lease_until = None
                feed.next_due = now
                return
            cursor = cursor_object(feed, first.query["region_id"])
            if failure is not None:
                deferred = isinstance(failure, ParseFailure) and failure.deferred
                cursor.failures += int(not deferred)
                delay = getattr(failure, "retry_after", None) or min(60, 4 * 2 ** min(cursor.failures, 4))
                if cursor.page == 1 and getattr(failure, "code", None) == "GatewayTimeout":
                    cursor.scheduling = {**(cursor.scheduling or {}), "head_timeouts": min(2,cursor.failures)}
                if not deferred and cursor.page > 1:
                    from .scheduling import deep_failed
                    deep_failed(cursor, now)
                # Preserve source blocking and budget pauses from the shared result.
                deadlines = list(await session.scalars(select(Job.next_due).where(
                    Job.id.in_([job.id for job in jobs]))))
                feed.next_due = max([now + timedelta(seconds=delay)] + [aware(d) for d in deadlines])
            else:
                ids = result.observed_item_ids or [item.source_item_id for item in result.items]
                known = set(await session.scalars(select(RegionalIdentity.item_id).where(
                    RegionalIdentity.catalog_id == feed.id, RegionalIdentity.item_id.in_(ids)))) if ids else set()
                new = set(ids) - known
                if new:
                    await session.execute(insert(RegionalIdentity),
                        [{"catalog_id": feed.id, "item_id": identity} for identity in sorted(new)])
                state = begin(cursor, now)
                page = cursor.page
                advance(cursor, state, result, new, now, max_pages)
                cursor.checks += 1
                cursor.failures = 0
                cursor.head_streak = cursor.head_streak + 1 if page == 1 else 0
                cursor.scheduling = {**(cursor.scheduling or {}),
                    "head_attempt_at" if page == 1 else "deep_attempt_at": now.isoformat()}
                started = aware(feed.lease_until) - timedelta(seconds=120)
                feed.next_due = max(now, started + timedelta(seconds=interval))
            save_cursor(feed, cursor)
            feed.lease_id = feed.lease_until = None


class SharedMonitor(Monitor):
    async def once(self, region=None, *, ingest=True, started_event=None):
        if ingest:
            await self.store.ingest()
        jobs = await self.store.claim(region)
        if not jobs:
            if started_event:
                started_event.set()
            return False
        self.last_request_started = perf_counter()
        if started_event:
            started_event.set()
        request = ParseRequest(**{**regional_query(jobs[0].query["region_id"]),
                                  "page": jobs[0].page, "timeout_seconds": jobs[0]._feed_timeout})
        try:
            result = await self.parser.parse(request)
            validate_result(request, result)
        except Exception as exc:
            code = exc.code if isinstance(exc, ParseFailure) else type(exc).__name__
            for job in jobs:
                await self.store.fail(job, code, retry_after=getattr(exc,"retry_after",None),
                                      deferred=getattr(exc,"deferred",False))
            await self.store.complete_feed(jobs,None,interval=self.interval,max_pages=self.max_pages,failure=exc)
            logger.warning("Shared catalog failed region=%s code=%s",request.region_id,code)
            return True

        # DB work overlaps across users; same-user transactions stay in order
        # because delivery dedup and personal controls share that user's lock.
        groups = {}
        for job in jobs:
            groups.setdefault(job.token["user_id"], []).append(job)
        lanes = asyncio.Semaphore(4)
        errors = []
        async def fanout(group):
            async with lanes:
                for job in group:
                    try:
                        local = ParseRequest(**job.query, page=request.page, request_id=result.request_id)
                        plan = build_query(local, request.timeout_seconds)
                        filtered = result.model_copy(update={"residual_filters":plan.residual,
                            "applied_filters":plan.applied,
                            "filter_logic":"AND_text_OR_sizes_OR_conditions" if local.filters.keywords_all
                            else "AND_between_fields_OR_within_field"})
                        validate_result(local, filtered)
                        await self.store.finish(job, filtered, interval=self.interval, max_pages=self.max_pages)
                    except Exception as exc:
                        errors.append(exc)
                        await self.store.fail(job, type(exc).__name__)
                        logger.error("Shared catalog subscriber failed type=%s",type(exc).__name__)
        await asyncio.gather(*(fanout(group) for group in groups.values()))
        await self.store.complete_feed(jobs,result,interval=self.interval,max_pages=self.max_pages,
                                       failure=errors[0] if errors else None)
        logger.info("Shared catalog region=%s page=%s filters=%s recipients=%s cards=%s fanout_errors=%s",
                    request.region_id,request.page,len(jobs),len(groups),result.cards_seen,len(errors))
        return True
