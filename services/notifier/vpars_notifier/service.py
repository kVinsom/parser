import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from time import perf_counter
from uuid import uuid4

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiohttp import ClientConnectorError
from sqlalchemy import DateTime, and_, case, cast, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from vpars_bot.db import ControlCommand, Inbox, Search, User, utcnow
from vpars_bot.domain import Filters, UserError, euro_price
from vpars_bot.entitlements import access_error
from vpars_bot.formatting import Lot, safe_url
from vpars_bot.lot_messages import MAX_PHOTOS_PER_LOT, prepare_lot, rich_lot_message
from vpars_bot.search_control import WorkToken, run_state
from vpars_bot.transport import TelegramDeferred
from vpars_bot.wallet_links import delivery_checkout_url, direct_buy_keyboard
from vpars_parser.models import Item
from vpars_parser.sources.vinted_attributes import item_waist_size
from vpars_worker.db import Delivery, Job, SentLot
from vpars_worker.freshness import (
    MAX_AGE_SECONDS,
    freshness_error,
    publication_age_error,
    relative_snapshot_ttl,
)
from vpars_worker.matching import excluded_match
from vpars_worker.message_cache import cache_values, message_key, message_lock_key
from vpars_worker.monitor import aware
from vpars_worker.parser import DETAIL_TIMEOUT_SECONDS, ParseFailure

from .photo_cache import PhotoCache
from .queue_fairness import lock_lane, record_turn, service_order

logger = logging.getLogger("vpars.notifier")
MAX_PUBLICATION_ATTEMPTS = 3


def detail_budget(item, progress, now, *, required):
    """Reserve time for send; optional enrichment cannot consume the age proof."""
    origin = item.published_at or item.first_seen_at
    remaining = MAX_AGE_SECONDS - (now - origin).total_seconds() if origin else 0
    if not required and item.published_at is None:
        try:
            checked = datetime.fromisoformat(progress["publication_checked_at"])
            remaining = min(
                remaining,
                relative_snapshot_ttl(progress.get("publication_evidence"))
                - (now - checked).total_seconds(),
            )
        except (KeyError, ValueError, TypeError):
            return 0
    # Mandatory publication/exclusion checks need the browser's own budget plus
    # dispatch overhead. Never exceed the existing age window or its send reserve.
    return max(0, min(DETAIL_TIMEOUT_SECONDS + 5 if required else 3, remaining - 2))


class Notifier:
    def __init__(self, store, bot, *, parser=None, handoff_url=""):
        self.store, self.bot, self.parser = store, bot, parser
        self.handoff_url = handoff_url
        self.ready_event = None
        self.next_expiry_check = 0
        self.photo_cache = PhotoCache(store.sessions, getattr(bot, "id", None))
        logger.info(
            "Telegram photo reference cache enabled=%s",
            self.photo_cache.bot_id is not None,
        )
        logger.info("Fair delivery scheduling enabled=True")

    async def claim(self, lane=None, *, fresh_first=False):
        now = utcnow()
        # Rows are inserted in source order. Sorting every row's insertion time
        # DESC reversed each catalog page and prepared its tail first. Within
        # each search prefer recent batches, retaining their source order. Legacy
        # rows keep their original priority; this is never publication-age proof.
        batch_at = func.coalesce(
            cast(
                Delivery.media_progress["catalog_batch_at"].as_string(),
                DateTime(timezone=True),
            ),
            Delivery.created_at,
        )
        async with self.store.sessions.begin() as session:
            await lock_lane(session, lane)
            now = utcnow()
            # All recipients have the same freshness limit. Keep confirmed history
            # and in-flight sends; expire only old, unclaimed queued deliveries.
            if perf_counter() >= self.next_expiry_check:
                # All four lanes share this notifier. Avoid rescanning the same
                # queue on every idle poll; decide() still checks every claimed lot.
                self.next_expiry_check = perf_counter() + 1
                published = Delivery.item["published_at"].as_string()
                discovered = Delivery.item["first_seen_at"].as_string()
                cutoff = now - timedelta(seconds=60)
                old_ids = list(
                    await session.scalars(
                        select(Delivery.id)
                        .where(
                            Delivery.state == "pending",
                            or_(
                                Delivery.lease_until.is_(None),
                                Delivery.lease_until <= now,
                            ),
                            or_(
                                cast(published, DateTime(timezone=True)) <= cutoff,
                                and_(
                                    published.is_(None),
                                    cast(discovered, DateTime(timezone=True)) <= cutoff,
                                ),
                            ),
                        )
                        .order_by(Delivery.next_due, Delivery.id)
                        .limit(500)
                        .with_for_update(skip_locked=True)
                    )
                )
                if old_ids:
                    await session.execute(
                        update(Delivery)
                        .where(
                            Delivery.id.in_(old_ids),
                        )
                        .values(
                            state="expired",
                            last_error="FreshnessWindowExpired",
                            lease_id=None,
                            lease_until=None,
                        )
                    )
                    logger.info(
                        "Old queued lots expired for all recipients count=%s",
                        len(old_ids),
                    )
            row = await session.scalar(
                select(Delivery)
                .where(
                    Delivery.state.in_(["pending", "sending"]),
                    Delivery.next_due <= now,
                    or_(Delivery.lease_until.is_(None), Delivery.lease_until <= now),
                    *(
                        [
                            Delivery.media_progress["dispatch_ready"]
                            .as_boolean()
                            .is_(True)
                        ]
                        if lane == "ready"
                        else [
                            Delivery.media_progress["dispatch_ready"]
                            .as_boolean()
                            .is_not(True)
                        ]
                        if lane == "prepare"
                        else []
                    ),
                )
                # Finish an enriched/partially sent lot before loading many more galleries.
                .order_by(
                    # A new queue row from a deep catalog is not a new listing.
                    # Reserve preparation capacity for head discoveries. The
                    # periodic FIFO turn still services deeper discoveries.
                    *(
                        [
                            case(
                                (
                                    Delivery.media_progress["catalog_page"].as_integer()
                                    > 1,
                                    1,
                                ),
                                else_=0,
                            )
                        ]
                        if fresh_first and lane == "prepare"
                        else []
                    ),
                    # A busy search cannot monopolize either delivery lane.
                    # Within each search retain recent batches and source order.
                    *([service_order(lane)] if fresh_first else []),
                    *(
                        [
                            case(
                                (
                                    batch_at >= now - timedelta(seconds=60),
                                    batch_at,
                                ),
                                else_=None,
                            )
                            .desc()
                            .nullslast(),
                            Delivery.media_progress["catalog_position"]
                            .as_integer()
                            .asc()
                            .nullslast(),
                        ]
                        if fresh_first
                        else []
                    ),
                    Delivery.media_progress["dispatch_ready"]
                    .as_boolean()
                    .desc()
                    .nullslast(),
                    Delivery.created_at.asc(),
                    Delivery.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            # FIFO recovery turns also count as service for the same search.
            await record_turn(session, row, lane, now)
            if row.state == "sending":
                # Only the transport phase can have reached Telegram. Preparation
                # can resume immediately; ambiguous sends require manual review.
                uncertain = (row.media_progress or {}).get("transport_started", True)
                row.uncertain += int(uncertain)
                row.state = "unknown" if uncertain else "pending"
                row.last_error = (
                    "InterruptedSend" if uncertain else "InterruptedPreparation"
                )
                row.lease_id = row.lease_until = None
                row.next_due = now
                return None
            row.state = "sending"
            row.media_progress = {
                **(row.media_progress or {}),
                "transport_started": False,
            }
            row.media_progress.setdefault(
                f"{lane or 'any'}_claimed_at", now.isoformat()
            )
            row.lease_id = str(uuid4())
            row.lease_until = now + timedelta(seconds=120)
            return row

    async def decide(self, row):
        item = Item.model_validate(row.item)
        async with self.store.sessions() as session:
            waiting = False
            rejected = False
            rejection_error = "NotNewForSearch"
            excluded = False
            needs_details = None
            for candidate in row.candidates:
                token = WorkToken(**candidate["token"])
                if token.user_id != row.user_id:
                    continue
                current = await self.store.current(session, token)
                if current is None:
                    user = await session.get(User, token.user_id)
                    search = await session.get(Search, token.search_id)
                    if user and search:
                        error = await access_error(
                            session, user, search, self.store.registry, token.source_id
                        )
                        if error:
                            rejected, rejection_error = True, error
                    continue
                # Legacy queued rows cannot acquire eligibility merely on upgrade.
                if candidate.get(
                    "discovery_mode"
                ) != "after_baseline" or not candidate.get("job_id"):
                    rejected = True
                    continue
                job = await session.get(Job, candidate["job_id"])
                command = await session.get(ControlCommand, candidate["job_id"])
                if (
                    job is None
                    or not job.baseline_ready
                    or job.token != candidate["token"]
                    or command is None
                    or command.kind != "start"
                    or command.payload.get("token") != candidate["token"]
                    or freshness_error(item, utcnow(), aware(command.created_at))
                ):
                    rejected = True
                    continue
                user, search, run = current
                saved = Filters.model_validate(search.filters)
                if euro_price(saved):
                    try:
                        price = self.store.registry.for_source(
                            saved, token.source_id
                        ).price
                    except UserError:
                        waiting = True
                        continue
                    if (
                        item.price.currency != price.currency
                        or (
                            price.minimum is not None
                            and item.price.amount < price.minimum
                        )
                        or (
                            price.maximum is not None
                            and item.price.amount > price.maximum
                        )
                    ):
                        rejected, rejection_error = True, "PriceOutsideRange"
                        continue
                age_error = publication_age_error(
                    item, utcnow(), row.media_progress or {}
                )
                if age_error == "PublicationCheckRequired":
                    candidate = {**candidate, "needs_publication": True}
                elif age_error is not None:
                    rejected = True
                    rejection_error = age_error
                    continue
                exclusions = command.payload["filters"].get("excluded_keywords", [])
                if excluded_match(item, exclusions):
                    excluded = True
                    continue
                # An exclusion filter must check the item's description before sending.
                if exclusions and item.description is None:
                    if (row.media_progress or {}).get("description_checked"):
                        excluded = True
                        continue
                    candidate = {**candidate, "needs_description": True}
                if run_state(run) != "active":
                    waiting = True
                    continue
                # Reserve menu responsiveness: lot delivery waits behind pending chat work.
                menu_pending = await session.scalar(
                    select(Inbox.update_id)
                    .where(
                        Inbox.bot_id == self.bot.id,
                        Inbox.lane_key == f"chat:{user.chat_id}",
                        Inbox.state.in_(["received", "processing"]),
                        Inbox.next_attempt_at <= utcnow(),
                        Inbox.received_at >= utcnow() - timedelta(seconds=2),
                    )
                    .limit(1)
                )
                if menu_pending is not None:
                    return "waiting", None
                if candidate.get("needs_description"):
                    # Matching searches are alternatives. A stricter search
                    # must not force description I/O when another current
                    # search already permits this same lot for this recipient.
                    if needs_details is None:
                        needs_details = (user.chat_id, candidate)
                    continue
                return "ready", (user.chat_id, candidate)
            if needs_details is not None:
                return "ready", needs_details
            return (
                "waiting"
                if waiting
                else "excluded"
                if excluded
                else "expired"
                if rejected
                else "cancelled"
            ), rejection_error if rejected and not waiting and not excluded else None

    async def settle(
        self,
        claimed,
        state,
        *,
        delay=0,
        error=None,
        message_id=None,
        attempted=False,
        uncertain=False,
        text_only=None,
        progress=None,
        item=None,
    ):
        async with self.store.sessions.begin() as session:
            row = await session.scalar(
                select(Delivery).where(Delivery.id == claimed.id).with_for_update()
            )
            if row.lease_id != claimed.lease_id:
                return
            if (
                state in {"excluded", "cancelled", "expired"}
                and row.candidates != claimed.candidates
            ):
                # Collection may add a matching search after decide() reads its
                # snapshot. Recheck a stale verdict with the original age proof;
                # this never reopens historical expired/uncertain/sent deliveries.
                state, error = "pending", None
                delay = 0
            elif (
                state == "pending"
                and delay
                and progress
                and progress.get("detail_error") == error
                and error is not None
                and not progress.get("transport_started")
                and row.candidates != claimed.candidates
            ):
                # An alternative arrived while details were in flight. Do not
                # park it behind the old match's optional description failure.
                delay = 0
            row.attempts += int(attempted)
            row.uncertain += int(uncertain)
            row.state = "failed" if uncertain and row.uncertain > 1 else state
            row.lease_id = row.lease_until = None
            row.next_due = utcnow() + timedelta(seconds=delay)
            row.last_error = error
            if text_only is not None:
                row.text_only = text_only
            if progress is not None:
                row.media_progress = progress
            if item is not None:
                row.item = item.model_dump(mode="json")
            if message_id is not None:
                row.message_id = message_id
                row.sent_at = utcnow()
                # Success and notification history commit atomically.
                await session.execute(
                    insert(SentLot)
                    .values(
                        **cache_values(
                            row.user_id,
                            row.item,
                            row.id,
                            message_id,
                            row.sent_at,
                        )
                    )
                    .on_conflict_do_nothing(index_elements=[SentLot.id])
                )
        if (
            state == "pending"
            and (progress or {}).get("dispatch_ready")
            and self.ready_event
        ):
            self.ready_event.set()

    async def deliver(self, row, lane=None):
        key = message_key(row.user_id, row.item)
        # Serialize different regional delivery rows for the same user and lot.
        # No user/search locks are held here, so Stop remains responsive.
        async with self.store.sessions.begin() as guard:
            acquired = await guard.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": message_lock_key(key)},
            )
            if not acquired:
                await self.settle(row, "pending", delay=1)
                return
            if await guard.get(SentLot, key) is not None:
                await self.settle(row, "duplicate", error="AlreadySent")
                logger.info("Previously sent lot suppressed")
                return
            # An ambiguous result in another region must not cause a second send.
            ambiguous = await guard.scalar(
                select(Delivery.id)
                .where(
                    Delivery.user_id == row.user_id,
                    Delivery.item["source_id"].as_string() == row.item["source_id"],
                    Delivery.item["source_item_id"].as_string()
                    == row.item["source_item_id"],
                    *(
                        []
                        if row.item["source_id"] == "vinted"
                        else [
                            Delivery.item["source_namespace"].as_string()
                            == row.item["source_namespace"]
                        ]
                    ),
                    Delivery.state == "unknown",
                )
                .limit(1)
            )
            if ambiguous:
                await self.settle(row, "unknown", error="RelatedDeliveryUncertain")
                return
            if lane == "prepare":
                # Give an already prepared regional copy time to send instead of
                # spending a second browser slot on the same user's same item.
                peers = await guard.scalars(
                    select(Delivery)
                    .where(
                        Delivery.user_id == row.user_id,
                        Delivery.id != row.id,
                        Delivery.state.in_(["pending", "sending"]),
                        Delivery.item["source_id"].as_string() == row.item["source_id"],
                        Delivery.item["source_item_id"].as_string()
                        == row.item["source_item_id"],
                        Delivery.media_progress["dispatch_ready"]
                        .as_boolean()
                        .is_(True),
                        Delivery.next_due <= utcnow(),
                    )
                    .limit(10)
                )
                for peer in peers:
                    if message_key(peer.user_id, peer.item) != key:
                        continue
                    decision, _ = await self.decide(peer)
                    if decision == "ready":
                        await self.settle(row, "pending", delay=1)
                        return
            await self._deliver(row, lane=lane)

    async def _deliver(self, row, lane=None):
        item = Item.model_validate(row.item)
        decision, recipient = await self.decide(row)
        if decision != "ready":
            if (
                decision == "expired"
                and recipient == "PublicationAgeUnknown"
                and await self.retry_unknown_age(
                    row, item, dict(row.media_progress or {})
                )
            ):
                return
            await self.settle(
                row,
                "pending" if decision == "waiting" else decision,
                delay=1,
                error="ExclusionFilter"
                if decision == "excluded"
                else recipient or "NotNewForSearch"
                if decision == "expired"
                else None,
            )
            return
        progress = dict(row.media_progress or {})
        needs_description = recipient[1].get("needs_description", False)
        needs_publication = recipient[1].get("needs_publication", False)
        needs_seller = (
            self.parser is not None
            and item.seller is None
            and not progress.get("seller_checked")
            and not progress.get("caption_sent")
            and not progress.get("offset")
        )
        if (
            needs_publication
            or needs_description
            or (
                (not item.photos_complete or needs_seller)
                and not row.text_only
                and not progress.get("gallery_unavailable")
            )
        ):
            if lane == "ready":
                await self.settle(
                    row, "pending", progress={**progress, "dispatch_ready": False}
                )
                return
            if self.parser is None:
                await self.settle(
                    row,
                    "expired" if needs_publication else "failed",
                    error="PublicationAgeUnknown"
                    if needs_publication
                    else "DetailsUnavailable",
                )
                return
            try:
                started = perf_counter()
                fetch_details = (
                    getattr(self.parser, "refresh_details", self.parser.details)
                    if progress.get("publication_retry")
                    else self.parser.details
                )
                budget = detail_budget(
                    item,
                    progress,
                    utcnow(),
                    required=needs_publication or needs_description,
                )
                if budget <= 0:
                    raise ParseFailure("DetailsDeadlineExceeded")
                try:
                    details = await asyncio.wait_for(
                        fetch_details(item), timeout=budget
                    )
                except TimeoutError:
                    raise ParseFailure("DetailsDeadlineExceeded") from None
            except ParseFailure as exc:
                if exc.deferred and (needs_publication or needs_description):
                    # No check ran. Preserve previous counters and evidence,
                    # including persisted progress from an earlier worker. A
                    # queue wait cannot establish unknown age or checked text.
                    await self.settle(
                        row,
                        "pending",
                        delay=exc.retry_after or 2,
                        error=exc.code,
                        progress={
                            **progress,
                            "detail_error": exc.code,
                            "dispatch_ready": False,
                        },
                    )
                    return
                attempts = progress.get("detail_attempts", 0) + int(not exc.deferred)
                if not needs_publication and not needs_description:
                    # Optional gallery/reputation enrichment must not make an
                    # already verified young lot wait past its delivery window.
                    # Keep the existing age proof; decide() rechecks it before
                    # sending, along with Stop and every exclusion predicate.
                    await self.settle(
                        row,
                        "pending",
                        error=exc.code,
                        progress={
                            **progress,
                            "dispatch_ready": True,
                            "detail_attempts": attempts,
                            "detail_error": exc.code,
                            "gallery_unavailable": not item.photos_complete,
                            "seller_checked": True,
                        },
                    )
                    return
                await self.settle(
                    row,
                    "pending",
                    delay=exc.retry_after
                    or (
                        120 if exc.code in {"AccessBlocked", "SourceRateLimited"} else 2
                    ),
                    error=exc.code,
                    progress={
                        **progress,
                        "detail_attempts": attempts,
                        "publication_attempts": progress.get("publication_attempts", 0)
                        + int(not exc.deferred),
                        "detail_error": exc.code,
                        "gallery_unavailable": attempts >= 3
                        and not item.photos_complete,
                        "seller_checked": attempts >= 3,
                        "description_checked": attempts >= 3,
                        **(
                            {
                                "publication_checked": True,
                                "publication_evidence": "unknown",
                                "publication_checked_at": None,
                            }
                            if attempts >= 3
                            else {}
                        ),
                    },
                )
                return
            logger.info(
                "Lot details loaded region=%s elapsed_ms=%.1f",
                item.region_id,
                (perf_counter() - started) * 1000,
            )
            published_at = details.published_at or item.published_at
            publication_evidence = getattr(details, "publication_evidence", "unknown")
            if (
                details.published_at
                and item.published_at
                and details.published_at != item.published_at
            ):
                # Retain the earliest source date, so a known pre-start item stays excluded.
                published_at = min(details.published_at, item.published_at)
                publication_evidence = "conflicting_dates"
            item = item.model_copy(
                update={
                    "photo_urls": details.photo_urls or item.photo_urls,
                    "photos_complete": details.photos_complete,
                    "published_at": published_at,
                    "seller": getattr(details, "seller", None) or item.seller,
                    "description": getattr(details, "description", None)
                    if getattr(details, "description", None) is not None
                    else item.description,
                }
            )
            # Commit enrichment before sending; retries/restarts reuse the frozen gallery.
            # The next claim checks Stop and discovery eligibility again.
            enriched = {
                **progress,
                "dispatch_ready": True,
                "gallery_unavailable": not details.photos_complete,
                "seller_checked": True,
                "description_checked": True,
                "publication_checked": True,
                "publication_evidence": publication_evidence,
                "publication_checked_at": details.fetched_at.isoformat()
                if getattr(details, "fetched_at", None) is not None
                else None,
                "publication_attempts": progress.get("publication_attempts", 0) + 1,
            }
            if publication_age_error(
                item, utcnow(), enriched
            ) == "PublicationAgeUnknown" and await self.retry_unknown_age(
                row, item, enriched
            ):
                return
            await self.settle(
                row,
                "pending",
                item=item,
                progress=enriched,
            )
            return
        if lane == "prepare":
            await self.settle(
                row, "pending", progress={**progress, "dispatch_ready": True}
            )
            return
        chat_id, candidate = recipient
        token = WorkToken(**candidate["token"])
        source = self.store.registry.sources[token.source_id]
        card = await prepare_lot(
            Lot(
                title=item.title,
                price=item.price.amount,
                currency=item.price.currency,
                source_label="Vinted" if source.provider == "vinted" else source.label,
                region=item.region_id,
                url=item.item_url,
                search_name=candidate["search_name"],
                brand=item.brand.raw if item.brand else None,
                photo_url=item.photo_urls[0]
                if item.photo_urls and not row.text_only
                else None,
                size=item_waist_size(item.size.raw if item.size else None, item.title)
                or (item.size.normalized if item.size else None),
                condition=(item.condition.normalized or item.condition.raw)
                if item.condition
                else None,
                seller_rating=item.seller.rating if item.seller else None,
                seller_feedback_count=item.seller.feedback_count
                if item.seller
                else None,
            )
        )
        if source.provider == "vinted" and self.handoff_url:
            card = replace(
                card,
                keyboard=direct_buy_keyboard(
                    card.keyboard,
                    delivery_checkout_url(self.handoff_url, row.id),
                ),
            )
        photo_ids = (
            {}
            if progress.get("photo_cache_bypass") or row.text_only
            else await self.photo_cache.resolve(
                list(dict.fromkeys(url for url in item.photo_urls if safe_url(url)))[
                    :MAX_PHOTOS_PER_LOT
                ]
            )
        )
        # Formatting/cache lookup may yield; recheck Stop, age and source selection last.
        decision, recipient = await self.decide(row)
        if decision != "ready":
            await self.settle(
                row,
                "pending" if decision == "waiting" else decision,
                delay=1,
                error="ExclusionFilter"
                if decision == "excluded"
                else recipient or "NotNewForSearch"
                if decision == "expired"
                else None,
            )
            return
        if recipient[1].get("needs_publication") or recipient[1].get(
            "needs_description"
        ):
            # Formatting can outlive age evidence or a filter toggle: the
            # remaining match may now require its description exclusions.
            # Re-enter preparation before sending under that different match.
            await self.settle(
                row, "pending", progress={**progress, "dispatch_ready": False}
            )
            return
        chat_id, _ = recipient
        # Respect confirmed photo offsets from the previous album-based sender.
        # New deliveries use one atomic rich message with at most ten photos.
        photos = (
            list(dict.fromkeys(url for url in item.photo_urls if safe_url(url)))[
                :MAX_PHOTOS_PER_LOT
            ]
            if not row.text_only
            else []
        )
        offset = progress.get("offset", 0)
        batch = photos[offset:]
        if progress.get("cover_only"):
            batch = batch[:1]
        notices = []
        if progress.get("gallery_unavailable"):
            notices.append(
                "Full gallery unavailable. Open the listing for more photos."
            )
        if progress.get("skipped") or progress.get("cover_only") or row.text_only:
            notices.append("Some photos could not be delivered.")
        # Build the payload before recording transport intent. A mapping error
        # here has sent no request and must remain retryable, never "unknown".
        used_photo_ids = {url: photo_ids[url] for url in batch if url in photo_ids}
        rich_message = rich_lot_message(
            card, batch, notices=notices, photo_ids=used_photo_ids
        )
        try:
            started = perf_counter()
            async with self.store.sessions.begin() as session:
                current = await session.get(Delivery, row.id, with_for_update=True)
                if current.lease_id != row.lease_id:
                    return
                current.media_progress = {**progress, "transport_started": True}
            message = await self.bot.send_rich_message(
                chat_id,
                rich_message=rich_message,
                reply_markup=card.keyboard,
                request_timeout=25,
            )
        except (TelegramDeferred, TelegramRetryAfter) as exc:
            await self.settle(
                row,
                "pending",
                delay=exc.retry_after + 0.1,
                error=type(exc).__name__,
                progress=progress,
            )
        except TelegramForbiddenError:
            await self.store.control.stop(row.user_id, "notifier-blocked:" + row.id)
            await self.settle(row, "failed", error="BotBlocked", attempted=True)
        except TelegramBadRequest:
            if used_photo_ids:
                # Definite rejection: keep every original photo and retry URLs
                # through the queue, with Stop/age/eligibility checked again.
                await self.photo_cache.invalidate(used_photo_ids)
                await self.settle(
                    row,
                    "pending",
                    delay=0,
                    error="CachedPhotoRejected",
                    attempted=True,
                    progress={**progress, "photo_cache_bypass": True},
                )
                return
            # Definite rejection sends nothing. Retry one cover, then text if needed;
            # never split a new lot into separate photo/card/button messages.
            if len(batch) > 1:
                progress["cover_only"] = True
            await self.settle(
                row,
                "pending" if batch else "failed",
                delay=1,
                error="PhotoRejected" if batch else "MessageRejected",
                attempted=True,
                progress=progress,
                text_only=True if len(batch) == 1 else None,
            )
        except TelegramNetworkError as exc:
            # aiogram chains aiohttp's connection failure. It happens before an
            # HTTP request can be sent, unlike a timeout/disconnect after upload.
            # Never infer this from exception text or retry an ambiguous send.
            not_sent = isinstance(exc.__cause__, ClientConnectorError)
            await self.settle(
                row,
                "pending" if not_sent else "unknown",
                delay=min(10, 2 ** min(row.attempts + 1, 4)) if not_sent else 0,
                error="TelegramConnectionFailed" if not_sent else "DeliveryUncertain",
                attempted=True,
                uncertain=not not_sent,
                progress=progress if not_sent else None,
            )
            logger.warning(
                "Telegram delivery failed stage=%s error=%s",
                "connect" if not_sent else "uncertain",
                type(exc.__cause__ or exc).__name__,
            )
        except (TelegramServerError, TimeoutError):
            await self.settle(
                row,
                "unknown",
                error="DeliveryUncertain",
                attempted=True,
                uncertain=True,
            )
        else:
            progress.update(
                offset=offset + len(batch),
                caption_sent=True,
                message_ids=progress.get("message_ids", []) + [message.message_id],
            )
            await self.settle(
                row,
                "sent",
                message_id=message.message_id,
                attempted=True,
                progress=progress,
            )
            # Confirm delivery first; cache failure must never replay a sent lot.
            await self.photo_cache.remember(message, batch)
            if used_photo_ids:
                logger.info(
                    "Telegram photo references reused count=%s", len(used_photo_ids)
                )
            logger.info(
                "Lot delivery accepted as one message transport_ms=%.1f queue_to_sent_ms=%.1f",
                (perf_counter() - started) * 1000,
                (utcnow() - aware(row.created_at)).total_seconds() * 1000,
            )

    async def retry_unknown_age(self, row, item, progress):
        """Persist a bounded retry; never turn an unknown or old age into permission."""
        attempts = progress.get("publication_attempts", 0)
        if self.parser is None or attempts >= MAX_PUBLICATION_ATTEMPTS:
            return False
        # first_seen_at is immutable. Backlog does not acquire a new freshness window.
        if (
            item.first_seen_at is None
            or (utcnow() - item.first_seen_at).total_seconds() >= 59
        ):
            return False
        await self.settle(
            row,
            "pending",
            delay=1,
            error="PublicationAgeRetry",
            item=item,
            progress={
                **progress,
                "publication_checked": False,
                "publication_retry": True,
                "dispatch_ready": False,
            },
        )
        return True

    async def once(self, lane=None, *, fresh_first=False):
        row = await self.claim(lane, fresh_first=fresh_first)
        if row is None:
            return False
        try:
            await self.deliver(row, lane=lane)
        except Exception as exc:
            # Release known pre-send failures instead of stranding them for 120 s.
            async with self.store.sessions() as session:
                current = await session.get(Delivery, row.id)
                uncertain = (current.media_progress or {}).get(
                    "transport_started", False
                )
            await self.settle(
                row,
                "unknown" if uncertain else "pending",
                delay=2,
                error=type(exc).__name__[:64],
                uncertain=uncertain,
            )
            raise
        return True

    async def run(self, lane=None, *, fresh_first=False, backlog_every=0):
        handled = 0
        if lane == "ready" and self.ready_event is None:
            self.ready_event = asyncio.Event()
        while True:
            try:
                if lane == "ready":
                    self.ready_event.clear()
                priority = fresh_first and not (
                    backlog_every and handled % backlog_every == backlog_every - 1
                )
                worked = await self.once(lane, fresh_first=priority)
                handled += int(worked)
                # Drain ready work immediately, including committed enrichment and
                # cancelled/duplicate rows. Telegram rate limits live in transport;
                # sleeping after every stage only grows the delivery backlog.
                if not worked and lane == "ready":
                    try:
                        await asyncio.wait_for(self.ready_event.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0 if worked else 0.5)
            except Exception as exc:  # noqa: BLE001 - isolate one failed delivery cycle
                logger.error("Delivery cycle failed (%s)", type(exc).__name__)
                await asyncio.sleep(5)
