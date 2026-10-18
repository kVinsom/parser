import asyncio
import logging
import time
from datetime import UTC, datetime

from .errors import ParserError
from .models import Item, ParseRequest, ParseResult, Timings, Warning
from .ports import GatewayClient, GatewayQuery, QueryPlan, SourceAdapter
from .settings import Settings
from .sources.vinted import VintedAdapter

logger = logging.getLogger("uvicorn.error.vpars_parser")
OUTCOMES = {
    "BLOCKED": ("AccessBlocked", 502, "Vinted access was blocked"),
    "RATE_LIMITED": ("SourceRateLimited", 503, "Vinted rate limited access"),
    "UNEXPECTED_REDIRECT": (
        "UnexpectedRedirect",
        502,
        "Gateway did not reach the expected catalog",
    ),
    "UNEXPECTED_CONTENT": (
        "UnexpectedContent",
        502,
        "Catalog page has no confirmed item inventory or explicit empty state",
    ),
    "UPSTREAM_ERROR": ("UpstreamError", 502, "Vinted returned an upstream error"),
    "SERVICE_ERROR": ("GatewayServiceError", 502, "Gateway access service failed"),
    "TIMEOUT": ("GatewayTimeout", 504, "Gateway browser request timed out"),
}


async def cpu_work(function, *args):
    """A cancelled network request must still account for an already running thread."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result


class AdapterRegistry:
    def __init__(self, adapters: list[SourceAdapter]):
        self.adapters = {adapter.source_id: adapter for adapter in adapters}
        if len(self.adapters) != len(adapters):
            raise ValueError("Duplicate source adapter")

    def get(self, source_id: str) -> SourceAdapter:
        try:
            return self.adapters[source_id]
        except KeyError:
            raise ParserError("UnsupportedSource", "Source is not enabled", 422) from None


def normalize_page(
    adapter: SourceAdapter, html: str, query: GatewayQuery, plan: QueryPlan, fetched_at: datetime
):
    start = time.perf_counter()
    page = adapter.parse_page(html, query)
    parsed_at = time.perf_counter()
    items: dict[tuple[str, str, str], Item] = {}
    skipped = page.unrecognized_cards
    warnings = page.warnings
    conflicts = False
    unknown_duplicates: set[tuple[tuple[str, str, str], str]] = set()
    for record in page.records:
        try:
            item = adapter.normalize_item(record, query.region, fetched_at)
        except ValueError:
            skipped += 1
            warnings.append(
                Warning(
                    code="InvalidCard", message="Card has missing or inconsistent required fields"
                )
            )
            continue
        key = (item.source_id, item.source_namespace, item.source_item_id)
        previous = items.get(key)
        if previous:
            if (previous.title, previous.price, previous.item_url) != (
                item.title,
                item.price,
                item.item_url,
            ):
                conflicts = True
                warnings.append(
                    Warning(
                        code="DuplicateConflict",
                        message="Duplicate cards disagree; first valid record retained",
                        item_id=item.source_item_id,
                    )
                )
            previous.photo_urls = list(dict.fromkeys(previous.photo_urls + item.photo_urls))
            for field in ("brand", "condition", "size", "colors", "category"):
                if (key, field) in unknown_duplicates:
                    continue
                old, new = getattr(previous, field), getattr(item, field)
                if old is None:
                    setattr(previous, field, new)
                elif new is not None and old != new:
                    conflicts = True
                    # Conflicting optional data must not produce a confirmed worker match.
                    setattr(previous, field, None)
                    unknown_duplicates.add((key, field))
                    warnings.append(
                        Warning(
                            code="DuplicateAttributeConflict",
                            message="Conflicting attribute is unknown",
                            item_id=item.source_item_id,
                            field=field,
                        )
                    )
        else:
            items[key] = item
    if not items and not page.empty_confirmed:
        raise ParserError(
            "SchemaChanged",
            "All recognized cards failed validation; an empty catalog is not confirmed",
        )
    for check in plan.residual:
        if items and check.item_field and check.item_field.endswith(".normalized"):
            field = check.item_field.split(".")[0]
            missing = [
                i
                for i in items.values()
                if getattr(i, field) is None or getattr(i, field).normalized is None
            ]
            if len(missing) == len(items):
                raise ParserError(
                    "UnsupportedFilter",
                    "Active filter has no verifiable values in the returned page",
                    422,
                    fields=[check.field],
                )
            for item in missing:
                warnings.append(
                    Warning(
                        code="FilterValueUnknown",
                        message="Worker must not treat this item as a confirmed filter match",
                        item_id=item.source_item_id,
                        field=check.field,
                    )
                )
    return (
        list(items.values()),
        page.pagination,
        warnings,
        skipped,
        len(page.records) + page.unrecognized_cards,
        "partial" if skipped or conflicts else "complete",
        (parsed_at - start) * 1000,
        (time.perf_counter() - parsed_at) * 1000,
        page.observed_item_ids,
        page.identities_complete,
        page.empty_confirmed,
    )


class ParserService:
    def __init__(
        self, gateway: GatewayClient, settings: Settings, registry: AdapterRegistry | None = None
    ):
        self.gateway = gateway
        self.settings = settings
        self.registry = registry or AdapterRegistry([VintedAdapter()])
        self.active: set[asyncio.Task] = set()
        self.detail_active: set[asyncio.Task] = set()
        self.closed = False

    def _finished(self, task: asyncio.Task):
        self.active.discard(task)
        self.detail_active.discard(task)
        if not task.cancelled():
            task.exception()  # Consume errors after HTTP caller cancellation/timeout.

    async def parse(self, request: ParseRequest) -> ParseResult:
        adapter = self.registry.get(request.source_id)
        plan = adapter.build_query(
            request, request.timeout_seconds or self.settings.browser_timeout
        )
        if (
            self.closed
            or len(self.active) >= self.settings.max_inflight
            or len(self.active) - len(self.detail_active) >= min(4, self.settings.max_inflight)
        ):
            raise ParserError(
                "ParserBusy", "Parser capacity is occupied; worker decides when to retry", 503
            )
        task = asyncio.create_task(self._run(request, adapter, plan))
        self.active.add(task)
        task.add_done_callback(self._finished)
        budget = (
            self.settings.control_timeout
            + plan.query.timeout_seconds
            + self.settings.rpc_grace
            + self.settings.parse_timeout
        )
        try:
            # Cancellation propagates through gRPC to the browser. cpu_work keeps
            # capacity reserved if normalization has already entered a thread.
            return await asyncio.wait_for(task, timeout=budget)
        except TimeoutError:
            raise ParserError(
                "ParserTimeout", "Parser request exceeded its total time budget", 504
            ) from None

    async def _run(
        self, request: ParseRequest, adapter: SourceAdapter, plan: QueryPlan
    ) -> ParseResult:
        start = time.perf_counter()
        outcome = "InternalError"
        try:
            async with asyncio.timeout(self.settings.control_timeout):
                regions = adapter.available_regions(
                    await self.gateway.list_regions(request.request_id)
                )
            if plan.query.region not in {r.code for r in regions}:
                raise ParserError(
                    "UnsupportedRegion", "Region is not currently advertised by the gateway", 422
                )
            async with asyncio.timeout(plan.query.timeout_seconds + self.settings.rpc_grace):
                page = await self.gateway.fetch_catalog(plan.query)
            fetched_at = datetime.now(UTC)
            access_ms = (time.perf_counter() - start) * 1000
            if page.outcome != "PAGE_LOADED":
                code, status, message = OUTCOMES.get(
                    page.outcome, ("GatewayContractError", 502, "Unknown gateway outcome")
                )
                raise ParserError(code, message, status, page.outcome, retry_after=page.retry_after)
            adapter.validate_response(page, plan.query)
            if not page.html:
                raise ParserError("GatewayContractError", "PAGE_LOADED did not include HTML")
            if len(page.html) > self.settings.max_html_bytes:
                raise ParserError(
                    "ResponseTooLarge", "Gateway HTML exceeds the configured parser limit", 502
                )
            try:
                html = page.html.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise ParserError(
                    "GatewayContractError", "Gateway HTML is not valid UTF-8"
                ) from None
            # DOM work runs outside the event loop; its active slot covers the thread lifetime.
            result = await cpu_work(
                normalize_page, adapter, html, plan.query, plan, fetched_at
            )
            (
                items,
                pagination,
                warnings,
                skipped,
                seen,
                completeness,
                parse_ms,
                normal_ms,
                observed_ids,
                identities_complete,
                empty_confirmed,
            ) = result
            if parse_ms + normal_ms > self.settings.parse_timeout * 1000:
                raise ParserError("ParserTimeout", "HTML processing exceeded its time budget", 504)
            outcome = completeness
            logger.info(
                "catalog_result request_id=%s cards=%d skipped=%d items=%d html_bytes=%d "
                "access_ms=%.2f parse_ms=%.2f normalization_ms=%.2f",
                request.request_id,
                seen,
                skipped,
                len(items),
                len(page.html),
                access_ms,
                parse_ms,
                normal_ms,
            )
            return ParseResult(
                request_id=request.request_id,
                source_id=request.source_id,
                region_id=plan.query.region,
                items=items,
                pagination=pagination,
                completeness=completeness,
                warnings=warnings,
                applied_filters=plan.applied,
                residual_filters=plan.residual,
                filter_logic=(
                    "AND_text_OR_sizes_OR_conditions"
                    if request.filters.keywords_all
                    else "AND_between_fields_OR_within_field"
                ),
                timings=Timings(
                    access_ms=access_ms,
                    parse_ms=parse_ms,
                    normalization_ms=normal_ms,
                    total_ms=(time.perf_counter() - start) * 1000,
                ),
                html_bytes=len(page.html),
                cards_seen=seen,
                cards_skipped=skipped,
                observed_item_ids=observed_ids,
                identities_complete=identities_complete,
                identities_ordered=isinstance(adapter, VintedAdapter),
                sort_order="newest_first_requested" if request.newest_first else "source_default",
                empty_confirmed=empty_confirmed,
            )
        except TimeoutError:
            outcome = "GatewayTimeout"
            raise ParserError("GatewayTimeout", "Gateway exceeded its time budget", 504) from None
        except ParserError as exc:
            outcome = exc.detail.code
            raise
        finally:
            logger.info(
                "catalog request_id=%s source=%s region=%s outcome=%s total_ms=%.2f",
                request.request_id,
                request.source_id,
                plan.query.region,
                outcome,
                (time.perf_counter() - start) * 1000,
            )

    async def close(self):
        self.closed = True
        if self.active:
            await asyncio.gather(*self.active, return_exceptions=True)
        await self.gateway.close()

    async def details(self, request):
        adapter = self.registry.get(request.source_id)
        request = request.model_copy(
            update={"region_id": adapter.normalize_region(request.region_id)}
        )
        if not hasattr(adapter, "parse_details"):
            raise ParserError("UnsupportedSource", "Item details are not supported", 422)
        if (
            self.closed
            or len(self.active) >= self.settings.max_inflight
            or len(self.detail_active) >= max(1, min(2, self.settings.max_inflight // 2))
        ):
            raise ParserError("ParserBusy", "Parser capacity is occupied", 503)
        task = asyncio.create_task(self._details(request, adapter))
        self.active.add(task)
        self.detail_active.add(task)
        task.add_done_callback(self._finished)
        return await task

    async def _details(self, request, adapter):
        try:
            async with asyncio.timeout(request.timeout_seconds + self.settings.rpc_grace):
                page, result = await self._item_response(request, adapter)
                if (
                    page.item_json
                    and not request.force_html
                    and (result.publication_evidence == "unknown" or not result.photos_complete)
                ):
                    logger.info(
                        "Item representation fallback request_id=%s region=%s "
                        "reason=incomplete_api_evidence", request.request_id, request.region_id
                    )
                    # One bounded HTML attempt, never another API call with the
                    # same missing age. Errors and Retry-After propagate to the queue.
                    _, html = await self._item_response(
                        request.model_copy(update={"force_html": True}), adapter
                    )
                    return merge_item_details(result, html)
                return result
        except TimeoutError:
            raise ParserError("GatewayTimeout", "Item details timed out", 504) from None

    async def _item_response(self, request, adapter):
        started = time.perf_counter()
        page = await self.gateway.fetch_item(request)
        fetched = time.perf_counter()
        if page.outcome != "PAGE_LOADED":
            code, status, message = OUTCOMES.get(
                page.outcome, ("GatewayContractError", 502, "Unknown gateway outcome")
            )
            raise ParserError(code, message, status, page.outcome, retry_after=page.retry_after)
        adapter.validate_item_response(page, request)
        if page.item_json:
            if request.force_html:
                raise ParserError("GatewayContractError", "Gateway ignored the HTML requirement")
            if len(page.item_json) > self.settings.max_html_bytes:
                raise ParserError("GatewayContractError", "Item JSON is too large")
            result = await cpu_work(
                adapter.parse_api_details, page.item_json, request, datetime.now(UTC)
            )
        else:
            if not page.html or len(page.html) > self.settings.max_html_bytes:
                raise ParserError("GatewayContractError", "Item HTML is missing or too large")
            try:
                html = page.html.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise ParserError("GatewayContractError", "Invalid item HTML encoding") from None
            result = await cpu_work(
                adapter.parse_details, html, request, datetime.now(UTC)
            )
        logger.info("Item phases request_id=%s region=%s representation=%s "
                    "gateway_ms=%.1f parse_ms=%.1f",
                    request.request_id, request.region_id, "api" if page.item_json else "html",
                    (fetched - started) * 1000, (time.perf_counter() - fetched) * 1000)
        return page, result


def merge_item_details(api, html):
    """Fill missing evidence while preserving contradictory publication dates."""
    dates = {stamp for stamp in (api.published_at, html.published_at) if stamp is not None}
    conflict = len(dates) > 1 or "conflicting_dates" in {
        api.publication_evidence,
        html.publication_evidence,
    }
    published = None if conflict else next(iter(dates), None)
    evidence = (
        "conflicting_dates"
        if conflict
        else api.publication_evidence
        if api.published_at
        else html.publication_evidence
    )
    gallery = html if html.photos_complete else api
    return api.model_copy(
        update={
            "published_at": published,
            "publication_evidence": evidence,
            "photo_urls": gallery.photo_urls,
            "photos_complete": gallery.photos_complete,
            "description": api.description if api.description is not None else html.description,
            "seller": api.seller or html.seller,
            "fetched_at": html.fetched_at,
        }
    )
