import asyncio
import json
from datetime import timedelta
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage, SendPhoto
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select
from vpars_bot.db import Inbox, SearchRun, utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import (
    Attribute,
    FilterCheck,
    Item,
    Money,
    Pagination,
    ParseRequest,
    ParseResult,
    Timings,
)

from vpars_worker.db import Delivery, Job
from vpars_worker.matching import matches, validate_result
from vpars_worker.monitor import Monitor, Store
from vpars_worker.parser import ParseFailure, ParserClient


def lot(number="10001", **updates):
    result = Item(
        source_id="vinted",
        region_id="FR",
        source_namespace="vinted:FR",
        source_item_id=number,
        title="Stone Island Hoodie",
        price=Money(amount="25", currency="EUR"),
        item_url=f"https://www.vinted.fr/items/{number}-test",
        photo_urls=["https://images1.vinted.net/test.jpg"],
        brand=Attribute(raw="Stone Island", normalized="stone island"),
        size=Attribute(raw="L", normalized="l"),
        fetched_at=utcnow(),
        photos_complete=True,
        published_at=utcnow(),
    )
    return result.model_copy(update=updates)


class Parser:
    def __init__(self, items=None):
        self.items = items if items is not None else [lot()]
        self.calls = []
        self.hook = None
        self.error = None
        self.more = False

    async def parse(self, request):
        self.calls.append(request)
        if self.hook:
            await self.hook()
        if self.error:
            raise ParseFailure(self.error)
        checks = []
        for name, item_field, operation in (
            ("brands", "brand.normalized", "any_exact_casefold"),
            ("sizes", "size.normalized", "any_exact_casefold"),
            ("models", "title", "any_alternative_all_words_casefold"),
            ("keywords", "title", "any_alternative_all_words_casefold"),
        ):
            values = getattr(request.filters, name)
            if values:
                if name == "keywords" and request.filters.keywords_all:
                    operation, item_field = "grouped_keywords_card_casefold", "searchable_card"
                checks.append(
                    FilterCheck(
                        field=name,
                        values=values,
                        normalized_values=[value.casefold() for value in values],
                        item_field=item_field,
                        operation=operation,
                    )
                )
        if request.filters.price:
            checks.append(
                FilterCheck(
                    field="price",
                    price=request.filters.price,
                    item_field="price",
                    operation="inclusive_range_same_currency",
                )
            )
        result = ParseResult(
            request_id=request.request_id,
            source_id="vinted",
            region_id="UK" if request.region_id == "GB" else request.region_id,
            items=self.items,
            pagination=Pagination(
                requested_page=request.page,
                has_more="true" if self.more else "false",
                next_page=request.page + 1 if self.more else None,
            ),
            completeness="complete",
            warnings=[],
            applied_filters=[],
            residual_filters=checks,
            filter_logic=(
                "AND_text_OR_sizes_OR_conditions"
                if request.filters.keywords_all
                else "AND_between_fields_OR_within_field"
            ),
            timings=Timings(access_ms=1, parse_ms=1, normalization_ms=1, total_ms=3),
            html_bytes=100,
            cards_seen=len(self.items),
            cards_skipped=0,
        )
        validate_result(request, result)
        return result


class Bot:
    id = 42

    def __init__(self):
        self.calls = []
        self.error = None

    async def send_rich_message(self, chat_id, **kwargs):
        self.calls.append(("rich", chat_id, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=501)


def rich_text(payload):
    return json.dumps(
        payload["rich_message"].model_dump(mode="json", exclude_defaults=True), ensure_ascii=False
    )


def rich_photos(payload):
    result = []
    for block in payload["rich_message"].blocks:
        if block.type == "photo":
            result.append(block.photo.media)
        elif block.type == "collage":
            result.extend(part.photo.media for part in block.blocks)
    return result


async def start(environment, *, user=101, key="save"):
    store, catalog = environment
    item = await catalog.save(
        user,
        Filters(
            brands=["Stone Island"],
            models=["Hoodie"],
            sizes=["L", "42"],
            native_prices={"EUR": {"minimum": "20", "maximum": "40"}},
            sources={"vinted_fr": ["FR"]},
        ),
        key,
    )
    await store.control.start(user, "start-" + key)
    # Existing regression scenarios start after a real baseline pass.
    await Monitor(store, Parser(items=[lot("999")])).once()
    await due(store, Job)
    return item


async def rows(store, model):
    async with store.sessions() as session:
        return list(await session.scalars(select(model)))


async def due(store, model):
    async with store.sessions.begin() as session:
        for row in await session.scalars(select(model)):
            row.next_due = utcnow() - timedelta(seconds=1)
            if model is Job and row.scheduling:
                # These traversal tests explicitly advance every work deadline.
                row.scheduling = {**row.scheduling, "deep_due_at": None}


async def test_and_or_missing_attributes_boundaries_and_contract():
    parser = Parser()
    request = ParseRequest(
        region_id="FR",
        filters={
            "brands": ["Stone Island"],
            "models": ["Hoodie"],
            "sizes": ["L", "42"],
            "price": {"minimum": "20", "maximum": "40", "currency": "EUR"},
        },
    )
    result = await parser.parse(request)
    assert matches(lot(), result.residual_filters)
    for amount in ("20", "40"):
        assert matches(lot(price=Money(amount=amount, currency="EUR")), result.residual_filters)
    for item in (
        lot(size=None),
        lot(brand=None),
        lot(title="Stone Island Hoodieish"),
        lot(price=Money(amount="19.99", currency="EUR")),
        lot(price=Money(amount="25", currency="GBP")),
        lot(availability="sold"),
        lot(size=Attribute(raw="XL", normalized="xl")),
    ):
        assert not matches(item, result.residual_filters)
    assert matches(lot(size=Attribute(raw="42", normalized="42")), result.residual_filters)
    with pytest.raises(ValueError):
        validate_result(request, result.model_copy(update={"request_id": "another"}))
    with pytest.raises(ValueError):
        validate_result(request, result.model_copy(update={"residual_filters": []}))


async def test_shared_query_dedup_delivery_and_restart(environment):
    store, _ = environment
    await start(environment)
    await start(environment, key="second")
    parser, bot = Parser(), Bot()
    monitor = Monitor(store, parser)
    assert await monitor.once()
    assert len(parser.calls) == 1
    assert len(await rows(store, Job)) == 2
    assert set((await store.control.status(101)).values()) == {"active"}
    deliveries = await rows(store, Delivery)
    assert len(deliveries) == 1 and len(deliveries[0].candidates) == 2
    assert await Notifier(store, bot).once()
    assert len(bot.calls) == 1
    assert "Stone Island" in rich_text(bot.calls[0][2])
    assert "Brand:" in rich_text(bot.calls[0][2])
    assert "Demo" not in rich_text(bot.calls[0][2])
    reopened = Store(store.sessions, store.registry)
    await due(store, Job)
    await Monitor(reopened, parser).once()
    assert not await Notifier(reopened, bot).once()
    assert len(bot.calls) == 1 and (await rows(store, Delivery))[0].state == "sent"


@pytest.mark.parametrize("change", ["stop", "archive", "disable", "disable_filter"])
async def test_change_during_parse_never_acknowledges_or_queues(environment, change):
    store, catalog = environment
    item = await start(environment)
    parser = Parser()

    async def mutate():
        if change == "stop":
            await store.control.stop(101, "stop")
        elif change == "archive":
            await catalog.archive(101, item.id, True, item.version, "archive")
        elif change == "disable_filter":
            await catalog.toggle_filter(101, item.id, "disable-filter")
        else:
            await catalog.toggle_marketplace(101, "vinted_fr", "disable")

    parser.hook = mutate
    await Monitor(store, parser).once()
    assert not await rows(store, Delivery)
    assert (await rows(store, Job))[0].state == "cancelled"


@pytest.mark.parametrize("change", ["stop", "disable_filter"])
async def test_stop_before_delivery_and_new_start(environment, change):
    store, catalog = environment
    item = await start(environment)
    parser, bot = Parser(), Bot()
    await Monitor(store, parser).once()
    if change == "disable_filter":
        await catalog.toggle_filter(101, item.id, "disable-filter")
    else:
        await store.control.stop(101, "stop")
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "cancelled"
    if change == "disable_filter":
        await catalog.toggle_filter(101, item.id, "enable-filter")
    await store.control.start(101, "restart")
    await Monitor(store, parser).once()
    assert len(bot.calls) == 0  # The new generation also starts with a silent baseline.
    parser.items = [lot("10002")]
    await due(store, Job)
    await Monitor(store, parser).once()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1


@pytest.mark.parametrize("change", ["archive", "disable_filter"])
async def test_one_inactive_match_does_not_cancel_other_match(environment, change):
    store, catalog = environment
    first = await start(environment)
    await start(environment, key="second")
    await Monitor(store, Parser()).once()
    if change == "disable_filter":
        await catalog.toggle_filter(101, first.id, "disable-filter")
    else:
        await catalog.archive(101, first.id, True, first.version, "archive")
    bot = Bot()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1


async def test_users_are_isolated(environment):
    store, catalog = environment
    await start(environment)
    await catalog.register(202, 202, "bob", "Bob", 2, start=True)
    await start(environment, user=202, key="bob")
    await Monitor(store, Parser()).once()
    await store.control.stop(101, "stop")
    bot = Bot()
    for _ in range(2):
        await Notifier(store, bot).once()
    assert [call[1] for call in bot.calls] == [202]


async def test_pagination_caps_and_error_backoff(environment):
    store, _ = environment
    await start(environment)
    parser = Parser()
    parser.more = True
    monitor = Monitor(store, parser, max_pages=3)
    for index in range(3):
        parser.items = [lot(str(10001 + index))]
        await due(store, Job)
        await monitor.once()
    assert [call.page for call in parser.calls] == [1, 2, 3]
    job = (await rows(store, Job))[0]
    assert job.page == 1 and job.coverage == "catchup_pending"
    assert job.scan_state["resume_page"] == 4
    assert not await monitor.once()
    await due(store, Job)
    parser.error = "SourceRateLimited"
    await monitor.once()
    job = (await rows(store, Job))[0]
    assert job.next_due >= utcnow() + timedelta(seconds=115)
    # The rate limit pauses new source work, not delivery from the last successful poll.
    assert set((await store.control.status(101)).values()) == {"active"}
    assert "Vinted обмежив частоту" in (await store.control.problems(101))[0]


async def test_stale_executor_lease_cannot_commit(environment):
    store, _ = environment
    await start(environment)
    await store.ingest()
    job = (await store.claim())[0]
    result = await Parser().parse(ParseRequest(**job.query, page=job.page))
    async with store.sessions.begin() as session:
        (await session.get(Job, job.id)).lease_id = str(uuid4())
    assert not await store.finish(job, result, interval=30, max_pages=3)
    assert not await rows(store, Delivery)


async def test_restart_releases_old_work_but_cannot_commit_its_response(environment):
    store, _ = environment
    await start(environment)
    await store.ingest()
    old = (await store.claim("FR"))[0]
    result = await Parser().parse(ParseRequest(**old.query, page=old.page))
    await store.recover_executions()
    current = (await store.claim("FR"))[0]
    assert current.lease_id != old.lease_id
    assert not await store.finish(old, result, interval=4, max_pages=3)
    assert await store.finish(current, result, interval=4, max_pages=3)
    assert len(await rows(store, Delivery)) == 1


@pytest.mark.parametrize("slow_first", [False, True])
async def test_regions_have_independent_intervals_without_overlapping_requests(slow_first):
    class TestStore:
        registry = SimpleNamespace(
            sources={"vinted": SimpleNamespace(provider="vinted", regions=["GB", "FR", "IT", "DE"])}
        )

        async def ingest(self):
            pass

        async def claim(self, region):
            return [SimpleNamespace(query={"region_id": region}, page=1, scheduling={})]

        async def finish(self, *args, **kwargs):
            return True

    class TimedParser:
        def __init__(self):
            self.started = []
            self.active = 0
            self.peak = 0
            self.active_regions = set()
            self.done = asyncio.Event()

        async def parse(self, request):
            assert request.region_id not in self.active_regions
            self.active_regions.add(request.region_id)
            self.started.append((request.region_id, perf_counter()))
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.09 if slow_first and len(self.started) == 1 else 0.005)
            self.active -= 1
            self.active_regions.remove(request.region_id)
            if len(self.started) == 8:
                self.done.set()
            return SimpleNamespace(
                region_id=request.region_id,
                items=[],
                cards_seen=0,
                cards_skipped=0,
                completeness="complete",
            )

    parser = TimedParser()
    task = asyncio.create_task(Monitor(TestStore(), parser, interval=0.04).run())
    try:
        await asyncio.wait_for(parser.done.wait(), timeout=3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [r for r, _ in parser.started[:4]] == ["GB", "FR", "IT", "DE"]
    gaps = [b[1] - a[1] for a, b in zip(parser.started[:-1], parser.started[1:], strict=True)]
    assert all(gap >= 0.009 for gap in gaps)
    assert gaps[0] < 0.039  # Other regions do not wait for the full regional interval.
    for region in ("GB", "FR", "IT", "DE"):
        times = [at for code, at in parser.started if code == region]
        assert all(b - a >= 0.039 for a, b in zip(times, times[1:], strict=False))
    assert parser.peak <= 4


@pytest.mark.parametrize("kind", ["photo", "rate", "unknown", "blocked"])
async def test_telegram_failures_are_durable_and_bounded(environment, kind):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    bot = Bot()
    errors = {
        "photo": TelegramBadRequest(
            method=SendPhoto(chat_id=101, photo="https://test.invalid/x"), message="image rejected"
        ),
        "rate": TelegramRetryAfter(
            method=SendMessage(chat_id=101, text="test"), message="rate", retry_after=90
        ),
        "unknown": TelegramNetworkError(
            method=SendMessage(chat_id=101, text="test"), message="network"
        ),
        "blocked": TelegramForbiddenError(
            method=SendMessage(chat_id=101, text="test"), message="blocked"
        ),
    }
    bot.error = errors[kind]
    notifier = Notifier(store, bot)
    await notifier.once()
    row = (await rows(store, Delivery))[0]
    if kind == "rate":
        assert row.state == "pending" and row.attempts == 0
        assert row.next_due >= utcnow() + timedelta(seconds=85)
    elif kind == "unknown":
        assert row.state == "unknown" and row.uncertain == 1
        await due(store, Delivery)
        await notifier.once()
        assert (await rows(store, Delivery))[0].state == "unknown"
        assert len(bot.calls) == 1  # Ambiguous transport is never retried automatically.
    elif kind == "blocked":
        assert row.state == "failed"
        assert set((await store.control.status(101)).values()) == {"stopped"}
    else:
        assert row.text_only and row.state == "pending"
        bot.error = None
        await due(store, Delivery)
        await notifier.once()
        assert bot.calls[-1][0] == "rich"
        assert not rich_photos(bot.calls[-1][2])
        assert (await rows(store, Delivery))[0].state == "sent"


async def test_menu_priority_expired_monitor_and_uncertain_crash(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    async with store.sessions.begin() as session:
        session.add(Inbox(bot_id=42, update_id=10, state="received", lane_key="chat:101"))
    bot, notifier = Bot(), None
    notifier = Notifier(store, bot)
    await notifier.once()
    assert not bot.calls
    async with store.sessions.begin() as session:
        (await session.get(Inbox, (42, 10))).state = "processed"
        run = await session.scalar(select(SearchRun))
        run.active_until = utcnow() - timedelta(seconds=1)
    await due(store, Delivery)
    await notifier.once()
    assert not bot.calls  # A dead monitor cannot keep delivering indefinitely.
    await due(store, Job)
    await Monitor(store, Parser()).once()
    await due(store, Delivery)
    row = await notifier.claim()
    async with store.sessions.begin() as session:
        crashed = await session.get(Delivery, row.id)
        crashed.lease_until = utcnow() - timedelta(seconds=1)
        crashed.media_progress = {"transport_started": True}
    assert await notifier.claim() is None
    assert (await rows(store, Delivery))[0].state == "unknown"


async def test_concurrent_notifiers_send_once(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    bot = Bot()
    await asyncio.gather(Notifier(store, bot).once(), Notifier(store, bot).once())
    assert len(bot.calls) == 1
    assert (await rows(store, Delivery))[0].state == "sent"


async def test_stop_during_card_preparation_is_rechecked(environment, monkeypatch):
    import vpars_notifier.service as service

    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    original = service.prepare_lot

    async def delayed(lot):
        card = await original(lot)
        await store.control.stop(101, "stop-before-send")
        return card

    monkeypatch.setattr(service, "prepare_lot", delayed)
    bot = Bot()
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "cancelled"


async def test_http_parser_contract_and_errors():
    app = web.Application()
    state = {"error": None, "wrong_page": False}

    async def capabilities(request):
        return web.json_response(
            {
                "source_id": "vinted",
                "version": "test",
                "aliases": {"GB": "UK"},
                "regions": [{"code": "FR", "name": "France", "base_url": "https://www.vinted.fr"}],
                "filters": {
                    field: {"mode": "residual", "description": "test"}
                    for field in ("brands", "models", "sizes", "price")
                },
            }
        )

    async def parse(request):
        if state["error"]:
            return web.json_response({"error": {"code": state["error"]}}, status=503)
        query = ParseRequest.model_validate(await request.json())
        result = await Parser().parse(query)
        if state["wrong_page"]:
            result.pagination.requested_page += 1
        return web.json_response(result.model_dump(mode="json"))

    app.router.add_get("/v1/sources/vinted/capabilities", capabilities)
    app.router.add_post("/v1/catalog/parse", parse)
    server = TestServer(app)
    await server.start_server()
    client = ParserClient(str(server.make_url("")))
    query = ParseRequest(region_id="FR", filters={"brands": ["Stone Island"]})
    try:
        assert len((await client.parse(query)).items) == 1
        state["wrong_page"] = True
        with pytest.raises(ParseFailure, match="ValueError"):
            await client.parse(query)
        state["error"] = "SourceRateLimited"
        with pytest.raises(ParseFailure, match="SourceRateLimited"):
            await client.parse(query)
        client.cooldowns.clear()  # Check validation separately from the shared source cooldown.
        with pytest.raises(ParseFailure, match="UnsupportedFilter"):
            await client.parse(ParseRequest(region_id="FR", filters={"colors": ["black"]}))
        with pytest.raises(ParseFailure, match="UnsupportedRegion"):
            await client.parse(ParseRequest(region_id="PL"))
    finally:
        await client.close()
        await server.close()


@pytest.mark.parametrize(
    "region,raw,normalized,title,wanted,expected",
    [
        ("FR", "42", "42", "Nike Air Max 90", "eu 42", True),
        ("IT", "42,5", "42.5", "Nike Air Max 90", "eu 42.5", True),
        ("DE", "41", "41", "Nike Air Max 90", "eu 42", False),
        ("UK", "8", "8", "Nike Air Max 90", "eu 42", False),
        ("UK", "8", "8", "Nike Air Max 90 EU 42", "eu 42", True),
        ("UK", "8", "8", "Nike Air Max 90 EU 42 or EU 43", "eu 42", False),
        ("FR", "41", "41", "Nike Air Max 90 EU 42", "eu 42", False),
        ("UK", "8", "8", "Nike Air Max 90", "uk 8", True),
        ("FR", "M / 38 / 10", "m", "Jacket", "eu 38", False),
        ("UK", "EU 42", "eu 42", "Nike Air Max 90", "eu 42", True),
        ("UK", "8 | EU 42", None, "Nike Air Max 90", "eu 42", True),
    ],
)
def test_sizes_respect_country_system_and_conflicting_labels(
    region, raw, normalized, title, wanted, expected
):
    check = FilterCheck(
        field="sizes",
        values=[wanted],
        normalized_values=[wanted],
        item_field="size.normalized",
        operation="any_exact_casefold",
    )
    item = lot(region_id=region, size=Attribute(raw=raw, normalized=normalized), title=title)
    assert matches(item, [check]) is expected


async def test_query_upgrade_preserves_filters_tokens_and_or_alternatives(environment):
    from vpars_worker.monitor import source_text

    store, _ = environment
    await start(environment)
    await store.ingest()
    job = (await rows(store, Job))[0]
    assert job.query["search_text"] == "Stone Island"
    original_filters, original_token = job.query["filters"], job.token
    async with store.sessions.begin() as session:
        saved = await session.get(Job, job.id)
        saved.query = {**saved.query, "search_text": "Stone Island Hoodie"}
        saved.page = 3
        saved.next_due = utcnow() + timedelta(minutes=10)
    await store.refresh_queries()
    updated = (await rows(store, Job))[0]
    assert updated.query["filters"] == original_filters and updated.token == original_token
    assert updated.page == 1 and updated.next_due <= utcnow()
    assert updated.query["search_text"] == "Stone Island"
    assert source_text({"brands": ["Nike"], "models": ["Air Max 90", "Air Force 1"]}) == "Nike"


async def test_search_failure_is_visible_and_clears_after_recovery(environment):
    store, _ = environment
    await start(environment)
    await store.ingest()
    job = (await store.claim())[0]
    await store.fail(job, "UnsupportedFilter", ["sizes"])
    problems = await store.control.problems(101)
    assert len(problems) == 1 and "розміри" in problems[0] and "Stone Island" in problems[0]
    assert await store.control.problems(202) == []
    await due(store, Job)
    await Monitor(store, Parser()).once()
    assert await store.control.problems(101) == []
