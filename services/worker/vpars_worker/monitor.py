import asyncio
import hashlib
import json
import logging
from datetime import UTC, timedelta
from time import perf_counter
from uuid import uuid4

from sqlalchemy import or_, select, update
from vpars_bot.application import locked_user
from vpars_bot.db import ControlCommand, Search, SearchRun, User, utcnow
from vpars_bot.domain import Filters, UserError, euro_price
from vpars_bot.entitlements import access_error, reconcile_user
from vpars_bot.formatting import filter_name
from vpars_bot.search_control import PersistentSearchControl, WorkToken
from vpars_parser.errors import ParserError
from vpars_parser.models import ParseRequest
from vpars_parser.sources.vinted_query import build_query

from .catchup import advance, begin, scope_key
from .db import CatalogPage, Delivery, Job, Receipt, SeenItem, SentLot
from .freshness import freshness_error, publication_age_error
from .matching import excluded_match, mismatch_reason, validate_result
from .message_cache import message_key
from .page_cache import PageCache
from .parser import ParseFailure
from .query import source_text
from .scheduling import choose, deep_failed, head_timeout

logger = logging.getLogger("vpars.monitor")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Store:
    def __init__(self, sessions, registry):
        self.sessions, self.registry = sessions, registry
        self.control = PersistentSearchControl(sessions, registry)
        self.head_streak = {}

    async def has_active_searches(self):
        """Read current intent, including Start not yet ingested into a job."""
        async with self.sessions() as session:
            candidates = await session.stream(
                select(User, Search, SearchRun)
                .join(Search, Search.user_id == User.id)
                .join(SearchRun, SearchRun.search_id == Search.id)
                .where(
                    Search.enabled.is_(True),
                    Search.archived.is_(False),
                    SearchRun.state.in_(["requested", "active"]),
                )
            )
            try:
                async for user, search, run in candidates:
                    source = self.registry.sources.get(run.source_id)
                    if source is None or source.provider != "vinted":
                        continue
                    token = WorkToken(
                        user.id,
                        search.id,
                        run.source_id,
                        user.delivery_epoch,
                        run.generation,
                        search.version,
                    )
                    if self.control._current(user, search, run, token) and not await access_error(
                        session, user, search, self.registry, run.source_id
                    ):
                        return True
            finally:
                await candidates.close()
        return False

    async def current(self, session, token):
        user = await session.get(User, token.user_id)
        search = await session.get(Search, token.search_id)
        run = await session.get(SearchRun, (token.search_id, token.source_id))
        valid = self.control._current(user, search, run, token)
        if valid and await access_error(session, user, search, self.registry, token.source_id):
            await reconcile_user(session, user, self.registry)
            valid = False
        return (user, search, run) if valid else None

    async def recover_executions(self):
        # Call only after acquiring the singleton monitor lock. Old results retain
        # different lease IDs and cannot commit after the new process takes over.
        async with self.sessions.begin() as session:
            for job in await session.scalars(
                select(Job).where(Job.state == "running").with_for_update()
            ):
                job.state = "waiting"
                job.lease_id = job.lease_until = None
                job.next_due = utcnow()
            for delivery in await session.scalars(
                select(Delivery).where(Delivery.state == "sending").with_for_update()
            ):
                uncertain = (delivery.media_progress or {}).get("transport_started", True)
                delivery.state = "unknown" if uncertain else "pending"
                delivery.uncertain += int(uncertain)
                delivery.last_error = "InterruptedSend" if uncertain else "InterruptedPreparation"
                delivery.lease_id = delivery.lease_until = None
                delivery.next_due = utcnow()

    async def refresh_queries(self):
        """Upgrade derived queries, leaving saved filters and generation tokens intact."""
        async with self.sessions.begin() as session:
            for job in await session.scalars(
                select(Job).where(Job.state == "waiting").with_for_update()
            ):
                if job.baseline_ready and not job.checkpoint and not job.scan_state:
                    # v4 had no proven frontier. Recover from the head without
                    # silently baselining previously unseen IDs after upgrade.
                    job.page = 1
                    job.coverage = "legacy_frontier_pending"
                query = {
                    **job.query,
                    "search_text": source_text(job.query["filters"]),
                    "newest_first": True,
                }
                if query != job.query:
                    job.query = query
                    job.page = 1
                    job.next_due = utcnow()
                    job.failures = 0
                    job.last_error = None
                    job.baseline_ready = False
                    job.baseline_pages = []
                    job.checkpoint = job.scan_state = None
                    job.scheduling = None
                    await session.execute(
                        update(SeenItem).where(SeenItem.job_id == job.id).values(eligible=False)
                    )
                # Local filters share source work without sharing checkpoints or eligibility.
                job.query_key = scope_key(query)

    async def ingest(self):
        async with self.sessions.begin() as session:
            commands = list(
                await session.scalars(
                    select(ControlCommand)
                    .outerjoin(Receipt, ControlCommand.id == Receipt.id)
                    .where(Receipt.id.is_(None))
                    .order_by(ControlCommand.created_at, ControlCommand.id)
                    .limit(100)
                    .with_for_update(of=ControlCommand, skip_locked=True)
                )
            )
            for command in commands:
                session.add(Receipt(id=command.id))
                if command.kind != "start" or command.payload.get("simulation"):
                    continue
                token = WorkToken(**command.payload["token"])
                if not await self.current(session, token):
                    continue
                filters = Filters.model_validate(command.payload["filters"])
                source = self.registry.sources[token.source_id]
                regions = filters.sources[token.source_id]
                if source.provider != "vinted" or len(regions) != 1:
                    raise ValueError("Monitoring currently requires a single Vinted region")
                query = {
                    "source_id": source.provider,
                    "region_id": regions[0],
                    # Every original filter still requires verification on returned items.
                    "search_text": source_text(filters.model_dump()),
                    "filters": {
                        key: value
                        for key, value in filters.model_dump(mode="json").items()
                        if key
                        in (
                            "models",
                            "keywords",
                            "keywords_all",
                            "brands",
                            "sizes",
                            "conditions",
                            "colors",
                            "categories",
                            "price",
                        )
                    },
                    "timeout_seconds": 45,
                    "newest_first": True,
                }
                ParseRequest(**query)  # Reject invalid jobs before persisting them.
                session.add(
                    Job(
                        id=command.id,
                        token=command.payload["token"],
                        query=query,
                        query_key=scope_key(query),
                    )
                )
            return len(commands)

    async def claim(self, region=None):
        now = utcnow()
        due = (
            Job.next_due <= now,
            Job.state.in_(["waiting", "running"]),
            or_(Job.lease_until.is_(None), Job.lease_until <= now),
        )
        if region is not None:
            due += (Job.query["region_id"].as_string() == region,)
        async with self.sessions.begin() as session:
            jobs = list(
                await session.scalars(
                    select(Job)
                    .where(*due)
                    .order_by(Job.next_due, Job.id)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            )
            ready = []
            for job in jobs:
                current = await self.current(session, WorkToken(**job.token))
                if not current:
                    job.state = "cancelled"
                    job.lease_id = job.lease_until = None
                    continue
                # Re-derive only price from the saved EUR bounds at every poll,
                # including jobs that were started before currency conversion.
                # Catalog scope/checkpoints, generations and history stay intact.
                saved = Filters.model_validate(current[1].filters)
                if euro_price(saved):
                    try:
                        price = self.registry.for_source(saved, job.token["source_id"]).price
                    except UserError:
                        job.state = "waiting"
                        job.lease_id = job.lease_until = None
                        job.last_error = "ExchangeRateUnavailable"
                        job.next_due = now + timedelta(seconds=30)
                        current[2].last_error = "ExchangeRateUnavailable"
                        continue
                    job.query = {
                        **job.query,
                        "filters": {**job.query["filters"], "price": price.model_dump(mode="json")},
                    }
                ready.append(job)
            ready = choose(ready, now, self.head_streak.get(region, 0))
            if ready:
                head = ready[0].page == 1
                self.head_streak[region] = self.head_streak.get(region, 0) + 1 if head else 0
            for job in ready:
                job.scheduling = {
                    **(job.scheduling or {}),
                    "head_attempt_at" if job.page == 1 else "deep_attempt_at": now.isoformat(),
                }
                job.state = "running"
                job.lease_id = str(uuid4())
                job.lease_until = now + timedelta(seconds=120)
            return ready

    async def finish(self, claimed, result, *, interval, max_pages, shared=False):
        token = WorkToken(**claimed.token)
        async with self.sessions.begin() as session:
            await locked_user(session, token.user_id)
            job = await session.scalar(select(Job).where(Job.id == claimed.id).with_for_update())
            if (
                job.lease_id != claimed.lease_id
                or not job.lease_until
                or aware(job.lease_until) <= utcnow()
            ):
                return False
            current = await self.current(session, token)
            if current is None:
                job.state = "cancelled"
                job.lease_id = job.lease_until = None
                return False
            _, search, run = current
            run.state = "active"
            run.last_error = None
            run.active_until = utcnow() + timedelta(seconds=min(300, interval * 3 + 90))
            found = []
            observed_at = utcnow()
            command = await session.get(ControlCommand, job.id)
            started_at = aware(command.created_at)
            # Invalid attributes do not make an existing ID new on the next pass.
            # Require a complete identity inventory, even if some cards cannot match.
            catalog_ids = result.observed_item_ids or [i.source_item_id for i in result.items]
            catalog_positions = {identity: index for index, identity in enumerate(catalog_ids)}
            new_ids = set()
            was_baselined = job.baseline_ready
            legacy_ids = []
            if not shared and was_baselined and not job.checkpoint and not job.scan_state:
                legacy_ids = list(
                    await session.scalars(select(SeenItem.item_id).where(SeenItem.job_id == job.id))
                )
            scan = None if shared else begin(job, observed_at, legacy_ids)
            if scan is not None and scan["scope"] != scope_key(job.query):
                raise ValueError("Catalog checkpoint query mismatch")
            if catalog_ids:
                known = {
                    seen.item_id: seen
                    for seen in await session.scalars(
                        select(SeenItem).where(
                            SeenItem.job_id == job.id,
                            SeenItem.item_id.in_(catalog_ids),
                        )
                    )
                }
                new_ids = set(catalog_ids) - known.keys()
                for identity in new_ids:
                    seen = SeenItem(
                        job_id=job.id,
                        item_id=identity,
                        first_seen_at=observed_at,
                        # A checkpoint bounds traversal, not publication age.
                        # Promoted/moved items can put a new lot below old anchors.
                        eligible=was_baselined,
                    )
                    session.add(seen)
                    known[identity] = seen
                parsed_ids = {item.source_item_id for item in result.items}
                for identity in catalog_ids:
                    known[identity].last_seen_at = observed_at
                    if (known[identity].decision or {}).get("code") == "BeyondPreviousFrontier":
                        # Reconsider legacy rank-only exclusions without refreshing
                        # first_seen_at or bypassing the real publication-age policy.
                        known[identity].eligible = was_baselined
                    if identity not in parsed_ids:
                        known[identity].decision = {
                            "code": "CardNotParsed",
                            "parsed": False,
                            "novel_eligible": known[identity].eligible,
                            "filter_match": None,
                        }
                for item in result.items:
                    # Discovery comes from this worker, never from source metadata.
                    seen = known.get(item.source_item_id)
                    if seen is None:
                        continue
                    item = item.model_copy(update={"first_seen_at": aware(seen.first_seen_at)})
                    seen.item = item.model_dump(mode="json")
                    filter_reason = mismatch_reason(item, result.residual_filters)
                    excluded = excluded_match(
                        item, command.payload["filters"].get("excluded_keywords", [])
                    )
                    reason = None
                    if not was_baselined or not seen.eligible:
                        reason = {"code": "InitialBaseline"}
                    elif (observed_at - aware(seen.first_seen_at)).total_seconds() > 60:
                        reason = {"code": "DiscoveryWindowExpired"}
                    else:
                        reason = filter_reason
                        if not reason and excluded:
                            reason = {"code": "ExcludedKeyword"}
                        error = freshness_error(item, observed_at, started_at)
                        if not reason and error:
                            reason = {"code": error}
                        if item.published_at:
                            error = publication_age_error(item, observed_at, {})
                            if not reason and error:
                                reason = {"code": error}
                    seen.decision = {
                        "parsed": True,
                        "filter_match": not filter_reason and not excluded,
                        "novel_eligible": seen.eligible,
                        **(reason or {"code": "DeliveryEligible"}),
                    }
                    if not reason:
                        found.append(item)
            for item in found:
                seen = known[item.source_item_id]
                if await session.get(SentLot, message_key(token.user_id, item.model_dump())):
                    seen.decision = {**seen.decision, "code": "AlreadySent"}
                    continue
                key = digest(
                    [token.user_id, item.source_id, item.source_namespace, item.source_item_id]
                )
                delivery = await session.get(Delivery, key, with_for_update=True)
                candidate = {
                    "token": claimed.token,
                    "job_id": job.id,
                    "discovery_mode": "after_baseline",
                    "search_name": filter_name(search.filters),
                }
                discovery_order = {
                    "catalog_page": job.page,
                    "catalog_batch_at": observed_at.isoformat(),
                    "catalog_position": catalog_positions[item.source_item_id],
                }
                if delivery is None:
                    session.add(
                        Delivery(
                            id=key,
                            user_id=token.user_id,
                            item=item.model_dump(mode="json"),
                            candidates=[candidate],
                            media_progress=discovery_order,
                        )
                    )
                    seen.decision = {**seen.decision, "code": "DeliveryCreated", "delivery_id": key}
                elif (
                    delivery.last_error != "ClearedBeforeNewSearch"
                    and delivery.state not in {"sent", "failed", "expired", "duplicate"}
                    and not (delivery.state == "cancelled" and delivery.message_id is not None)
                ):
                    new_candidate = candidate not in delivery.candidates
                    remaining = [
                        c
                        for c in delivery.candidates
                        if (c["token"]["search_id"], c["token"]["source_id"])
                        != (token.search_id, token.source_id)
                    ]
                    delivery.candidates = remaining + [candidate]
                    if delivery.state == "cancelled":
                        delivery.state = "pending"
                        delivery.next_due = utcnow()
                        delivery.item = item.model_dump(mode="json")
                        delivery.media_progress = discovery_order
                        delivery.text_only = False
                    elif (
                        delivery.state == "excluded"
                        and new_candidate
                        and delivery.message_id is None
                    ):
                        # Exclusions belong to a search, not the whole lot. Recheck
                        # a newly matching search using the original age/description
                        # and prepared gallery; repeated observations alone do not retry.
                        delivery.state = "pending"
                        delivery.next_due = utcnow()
                        delivery.last_error = None
                    elif (
                        delivery.state == "pending"
                        and new_candidate
                        and (delivery.media_progress or {}).get("detail_error")
                        == delivery.last_error
                        and delivery.last_error is not None
                        and not (delivery.media_progress or {}).get("transport_started")
                    ):
                        # A new alternative may no longer need the failing
                        # description request. Re-evaluate now; the parser still
                        # enforces source pauses if details remain mandatory.
                        # Telegram retry deadlines and item age stay unchanged.
                        delivery.next_due = min(aware(delivery.next_due), utcnow())
                if delivery is not None:
                    seen.decision = {
                        **seen.decision,
                        "code": "DeliveryExists",
                        "delivery_id": key,
                        "state": delivery.state,
                    }
            session.add(
                CatalogPage(
                    id=str(uuid4()),
                    job_id=job.id,
                    scope=scope_key(job.query),
                    page=job.page,
                    observed_at=observed_at,
                    result=result.model_dump(mode="json"),
                    new_ids=sorted(new_ids),
                    decisions={identity: known[identity].decision for identity in catalog_ids},
                )
            )
            job.state = "waiting"
            job.lease_id = job.lease_until = None
            job.last_checked = utcnow()
            job.checks += 1
            job.items_checked += len(result.items)
            job.matches += len(found)
            job.failures = 0
            job.last_error = None
            if job.page == 1:
                job.scheduling = {**(job.scheduling or {}), "head_timeouts": 0}
            if job.page > 1:
                if result.completeness == "complete" and result.identities_complete:
                    job.scheduling = {
                        **(job.scheduling or {}),
                        "deep_failures": 0,
                        "deep_due_at": None,
                    }
                else:
                    deep_failed(job, observed_at)
            if shared:
                # A new subscriber establishes its own quiet baseline. Joining a
                # running feed does not silently inherit somebody else's history.
                if not job.baseline_ready and result.completeness == "complete" and result.identities_complete:
                    pages = set(job.baseline_pages or [])
                    if job.page == 1:
                        pages = {1}
                    elif job.page == 2 and 1 in pages:
                        pages.add(2)
                    job.baseline_pages = sorted(pages)
                    if 2 in pages or (job.page == 1 and result.pagination.has_more == "false"):
                        job.baseline_ready = True
                job.coverage = "shared_feed" if job.baseline_ready else "baseline_pending"
            else:
                advance(job, scan, result, new_ids, observed_at, max_pages)
            # Interval is measured from request start, not an extra pause after response.
            started = aware(claimed.lease_until) - timedelta(seconds=120)
            job.next_due = max(utcnow(), started + timedelta(seconds=interval))
            return True

    async def fail(self, claimed, code, fields=(), retry_after=None, *, deferred=False):
        token = WorkToken(**claimed.token)
        async with self.sessions.begin() as session:
            await locked_user(session, token.user_id)
            job = await session.scalar(select(Job).where(Job.id == claimed.id).with_for_update())
            if job.lease_id != claimed.lease_id:
                return
            current = await self.current(session, token)
            job.lease_id = job.lease_until = None
            job.state = "waiting" if current else "cancelled"
            job.failures += int(not deferred)
            if job.page == 1 and code == "GatewayTimeout" and not deferred:
                previous_budget = head_timeout([job])
                job.scheduling = {
                    **(job.scheduling or {}),
                    "head_timeouts": min(2, (job.scheduling or {}).get("head_timeouts", 0) + 1),
                    # Keep the learned deadline through successful requests. One
                    # success must not immediately reinstate the deadline that
                    # just failed; probe the fast default again after five minutes.
                    "head_budget_seconds": 20 if previous_budget < 20 else 24,
                    "head_budget_until": (utcnow() + timedelta(minutes=5)).isoformat(),
                }
            job.last_error = (code + (":" + ",".join(fields) if fields else ""))[:64]
            if job.scan_state and not deferred:
                job.scan_state = {
                    **job.scan_state,
                    "requests": job.scan_state["requests"] + 1,
                    "failed_requests": job.scan_state.get("failed_requests", 0) + 1,
                }
            session.add(
                CatalogPage(
                    id=str(uuid4()),
                    job_id=job.id,
                    scope=scope_key(job.query),
                    page=job.page,
                    observed_at=utcnow(),
                    new_ids=[],
                    decisions={},
                    result={
                        "error": job.last_error,
                        "retry_after": retry_after,
                        "deferred": deferred,
                    },
                )
            )
            # Transient failures recover within the freshness window. Explicit
            # source blocking retains a conservative cooldown, never rapid retries.
            delay = min(60, 4 * 2 ** min(job.failures - 1, 4))
            if not job.baseline_ready and code in {
                "UnexpectedContent",
                "GatewayTimeout",
                "GatewayServiceError",
            }:
                # A catalog that has never established its baseline must not
                # consume a fresh-head slot every minute indefinitely. Keep
                # initial retries quick and persist the longer recovery pause.
                delay = min(300, 4 * 2 ** min(job.failures - 1, 7))
            if code in {"ParserBusy", "GatewayResourceExhausted"}:
                delay = 2
            if code in {"AccessBlocked", "SourceRateLimited"}:
                delay = min(600, 120 * 2 ** min(job.failures - 1, 3))
            if retry_after is not None:
                delay = max(delay, retry_after)
            if deferred:
                # No upstream request happened. Reusing a regional cooldown is
                # not another failure and must not turn its remaining seconds
                # into a fresh 2–10 minute exponential backoff for each filter.
                delay = retry_after or 120
            job.next_due = utcnow() + timedelta(seconds=delay)
            if (
                not deferred
                and job.page > 1
                and code == "UnexpectedContent"
                and retry_after is None
            ):
                # An unconfirmed end remains pending, but cannot consume the head budget.
                deep_failed(job, utcnow())
                job.next_due = utcnow()
            # An error/unknown end is not proof of coverage. Keep the exact
            # cursor and all unfinished work, including across process restarts.
            if not deferred:
                job.coverage = "catchup_error" if job.baseline_ready else "baseline_error"
            if (
                not deferred
                and job.failures >= 2
                and job.page > 1
                and job.scan_state
                and job.scan_state.get("step") == "forward"
                and code != "UnexpectedContent"
            ):
                # A vanished anchor or permanently unconfirmed deep page must
                # not starve discovery at the head. The failed offset remains
                # durable and is retried after a head probe and overlap.
                job.scan_state = {
                    **job.scan_state,
                    "step": "head_probe",
                    "resume_page": job.page,
                }
                job.page = 1
            if current:
                # A failed request does not invalidate previously collected lots.
                # Keep the last successful poll's lease without extending it;
                # initial requests stay unacknowledged and all retry delays remain.
                current[2].last_error = job.last_error


class Monitor:
    def __init__(self, store, parser, *, interval=4, max_pages=3):
        self.store, self.parser = store, parser
        self.interval, self.max_pages = interval, max_pages
        self.last_started = {}
        self.last_request_started = None
        self.last_head_started = {}
        self.page_cache = PageCache()

    async def once(self, region=None, *, ingest=True, started_event=None):
        if ingest:
            await self.store.ingest()
        streaks = getattr(self.store, "head_streak", {})
        previous_streak = streaks.get(region, 0)
        jobs = await self.store.claim(region)
        claimed_streak = streaks.get(region, 0)
        if not jobs:
            if started_event:
                started_event.set()
            return False
        request = ParseRequest(**jobs[0].query, page=jobs[0].page)
        cached = self.page_cache.get(jobs, utcnow()) if request.page > 1 else None
        variants = {digest(job.query.get("filters", {})) for job in jobs}
        plans = {}
        if len(variants) > 1 or cached is not None:
            valid = []
            for job in jobs:
                local = ParseRequest(**job.query, page=job.page, request_id=request.request_id)
                try:
                    plans[job.id] = build_query(local, local.timeout_seconds or 45)
                except ParserError as exc:
                    await self.store.fail(job, exc.detail.code, exc.detail.fields)
                else:
                    valid.append(job)
            jobs = valid
            if not jobs:
                if started_event:
                    started_event.set()
                return False
            # One complete inventory; each saved filter gets its own verified predicate.
            request = ParseRequest(**{**jobs[0].query, "filters": {}}, page=jobs[0].page)
        if request.page == 1:
            # Fast default; adapt only the query that actually timed out.
            # Other regional lanes and their staggered start spacing continue.
            request = request.model_copy(
                update={"timeout_seconds": head_timeout(jobs, request.timeout_seconds or 45)}
            )
        else:
            # Retain the durable continuation, but do not occupy its region for
            # a 45-second deep fetch while the next fresh head is waiting.
            request = request.model_copy(
                update={"timeout_seconds": min(24, request.timeout_seconds or 45)}
            )
        started = perf_counter()
        if cached is None:
            previous = self.last_started.get(request.region_id)
            self.last_started[request.region_id] = started
            head_gap = None
            if request.page == 1:
                scope = scope_key(request.model_dump())
                if scope in self.last_head_started:
                    head_gap = round((started - self.last_head_started[scope]) * 1000, 1)
                self.last_head_started[scope] = started
            logger.info(
                "Checking region=%s page=%s gap_ms=%s region_gap_ms=%s head_gap_ms=%s",
                request.region_id,
                request.page,
                round((started - self.last_request_started) * 1000, 1)
                if self.last_request_started
                else None,
                round((started - previous) * 1000, 1) if previous else None,
                head_gap,
            )
            self.last_request_started = started
        if started_event:
            started_event.set()
        try:
            result = cached or await self.parser.parse(request)
            if cached is None:
                self.page_cache.put(request, result, jobs, utcnow())
        except Exception as exc:
            if isinstance(exc, ParseFailure) and exc.deferred and streaks.get(region, 0) == claimed_streak:
                # A local refusal did not perform a head poll and must not buy
                # a deep-page slot ahead of the next real inventory request.
                streaks[region] = previous_streak
            code = exc.code if isinstance(exc, ParseFailure) else type(exc).__name__
            fields = exc.fields if isinstance(exc, ParseFailure) else ()
            for job in jobs:
                await self.store.fail(
                    job,
                    code,
                    fields,
                    retry_after=exc.retry_after if isinstance(exc, ParseFailure) else None,
                    deferred=exc.deferred if isinstance(exc, ParseFailure) else False,
                )
            logger.warning(
                "Catalog check failed region=%s (%s, fields=%s)",
                request.region_id,
                code,
                ",".join(fields),
            )
        else:
            for job in jobs:
                local_result = result
                if plans:
                    local = ParseRequest(**job.query, page=job.page, request_id=result.request_id)
                    local_result = result.model_copy(
                        deep=True,
                        update={
                            "residual_filters": plans[job.id].residual,
                            "applied_filters": plans[job.id].applied,
                            "filter_logic": "AND_text_OR_sizes_OR_conditions"
                            if local.filters.keywords_all
                            else "AND_between_fields_OR_within_field",
                        },
                    )
                    validate_result(local, local_result)
                await self.store.finish(
                    job, local_result, interval=self.interval, max_pages=self.max_pages
                )
            logger.info(
                "Checked region=%s page=%s items=%s cards_seen=%s cards_skipped=%s "
                "completeness=%s shared_jobs=%s cache=%s elapsed_ms=%.1f",
                result.region_id,
                request.page,
                len(result.items),
                result.cards_seen,
                result.cards_skipped,
                result.completeness,
                len(jobs),
                cached is not None,
                (perf_counter() - started) * 1000,
            )
        return True

    async def run(self):
        regions = list(
            dict.fromkeys(
                region
                for source in self.store.registry.sources.values()
                if source.provider == "vinted"
                for region in source.regions
            )
        )

        async def check(region, event):
            try:
                return await self.once(region, started_event=event)
            except Exception as exc:
                logger.error("Monitoring cycle failed (%s)", type(exc).__name__)
                return False
            finally:
                event.set()

        # Each region owns its interval. Serializing all countries behind the
        # same interval multiplied discovery latency by the number of regions.
        # Stagger starts while retaining one request per region and no catch-up burst.
        spacing = self.interval / max(1, len(regions))
        next_region = {}
        pending = {}
        async with asyncio.TaskGroup() as group:
            while True:
                worked = False
                for region in regions:
                    pending = {r: task for r, task in pending.items() if not task.done()}
                    if region in pending:
                        continue
                    if perf_counter() < next_region.get(region, 0):
                        continue
                    if len(pending) >= 4:
                        await asyncio.wait(pending.values(), return_when=asyncio.FIRST_COMPLETED)
                    if self.last_request_started is not None:
                        # Some event loops wake timers early; never spend the
                        # same pacing slot twice, even after a delayed wakeup.
                        deadline = self.last_request_started + spacing
                        while (remaining := deadline - perf_counter()) > 0:
                            await asyncio.sleep(remaining)
                    before = self.last_request_started
                    event = asyncio.Event()
                    pending[region] = group.create_task(check(region, event))
                    await event.wait()
                    worked |= self.last_request_started != before
                    if self.last_request_started != before:
                        next_region[region] = self.last_request_started + self.interval
                if not worked:
                    await asyncio.sleep(min(0.5, spacing))
