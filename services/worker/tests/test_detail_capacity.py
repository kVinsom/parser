"""Capacity refusals are queue waits, never completed publication checks."""

from datetime import timedelta
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select
from test_filter_isolation import overlapping_filters
from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import utcnow
from vpars_notifier.service import Notifier
from vpars_parser.models import Capabilities, ParseRequest

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor, Store
from vpars_worker.parser import ParseFailure, ParserClient


@pytest.mark.parametrize("code", ["ParserBusy", "GatewayResourceExhausted", "ProxyBudgetWait", "ProxyBudgetExceeded", "ProxyBudgetNotStarted", "ProxyBudgetUnavailable"])
@pytest.mark.parametrize("lane", ["catalog", "details"])
@pytest.mark.parametrize("region,retry_after", [("FR", None), ("GB", 7)])
async def test_capacity_http_responses_defer_without_source_cooldown(code, lane, region, retry_after):
    async def busy(request):
        return web.json_response(
            {"error": {"code": code, "retry_after": retry_after}}, status=503
        )

    app = web.Application()
    app.router.add_post("/v1/catalog/parse", busy)
    app.router.add_post("/v1/items/details", busy)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        client.capabilities = Capabilities(
            source_id="vinted", version="test", aliases={"GB": "UK"}, filters={},
            regions=[{"code": r, "name": r, "base_url": url} for r, url in (
                ("FR", "https://www.vinted.fr"), ("UK", "https://www.vinted.co.uk")
            )],
        )
        client.expires = float("inf")
        try:
            with pytest.raises(ParseFailure) as error:
                if lane == "details":
                    await client.details(lot(region_id=region))
                else:
                    await client.parse(ParseRequest(region_id=region))
            assert error.value.deferred
            expected = retry_after or (30 if code.startswith("ProxyBudget") else 2)
            assert error.value.retry_after == (min(2, expected) if code == "ProxyBudgetWait" else expected)
            assert not client.cooldowns  # Local capacity must not pause a whole source.
        finally:
            await client.close()


@pytest.mark.parametrize("user,brand", [(101, "Nike"), (202, "Gucci")])
@pytest.mark.parametrize("code", ["ParserBusy", "GatewayResourceExhausted", "ProxyBudgetWait", "ProxyBudgetExceeded", "ProxyBudgetNotStarted", "ProxyBudgetUnavailable"])
@pytest.mark.parametrize("requirement,previous", [
    ("age", 0), ("age", 2), ("description", 0), ("description", 3),
])
async def test_required_details_survive_repeated_busy_and_restart(
    environment, monkeypatch, user, brand, code, requirement, previous
):
    store, catalog = environment
    _, wide = await overlapping_filters(environment, user, brand)
    if requirement == "description":
        await catalog.toggle_filter(user, wide.id, "require-exclusions")
    async with store.sessions.begin() as session:
        row = (await session.scalars(select(Delivery))).one()
        if requirement == "age":
            row.item = {**row.item, "published_at": None}
        row.media_progress = {
            **(row.media_progress or {}),
            "detail_attempts": previous, "publication_attempts": previous,
        }
        original = dict(row.item)
    requests, available = [], [False]

    async def details(request):
        requests.append(await request.json())
        if not available[0]:
            return web.json_response({"error": {"code": code}}, status=503)
        return web.json_response({
            "source_id": "vinted", "region_id": "FR", "source_item_id": "10001",
            "photo_urls": original["photo_urls"], "photos_complete": True,
            "description": "Good condition", "publication_evidence": "relative:just now",
            "fetched_at": utcnow().isoformat(),
        })

    app = web.Application()
    app.router.add_post("/v1/items/details", details)
    bot = Bot()
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        clock = [monotonic()]
        monkeypatch.setattr("vpars_worker.parser.monotonic", lambda: clock[0])
        # This server is local; production pacing is covered by test_source_rate.
        client.pacer.acquire = AsyncMock()
        try:
            for _ in range(4):  # More than the three actual age-check attempts.
                clock[0] += 3  # The explicitly advanced queue also passes its local budget poll.
                store = Store(store.sessions, store.registry)
                await due(store, Delivery)
                await Notifier(store, bot, parser=client).once("prepare")
                saved = (await rows(store, Delivery))[0]
                assert saved.state == "pending" and saved.last_error == code
                assert saved.media_progress["detail_attempts"] == previous
                assert saved.media_progress["publication_attempts"] == previous
                assert not saved.media_progress.get("publication_checked")
                assert not saved.media_progress.get("description_checked")
                assert not saved.media_progress.get("dispatch_ready") and not bot.calls
                assert saved.item == original
                maximum_wait = 30 if code.startswith("ProxyBudget") else 2
                assert 0 < (saved.next_due - utcnow()).total_seconds() <= maximum_wait
            available[0] = True
            clock[0] += 3
            await due(store, Delivery)
            notifier = Notifier(store, bot, parser=client)
            await notifier.once("prepare")
            await notifier.once("ready")
            saved = (await rows(store, Delivery))[0]
            assert saved.state == "sent" and len(bot.calls) == 1
            assert bot.calls[0][1] == user and len(requests) == 5
            assert saved.item["first_seen_at"] == original["first_seen_at"]
            assert saved.item["published_at"] == original["published_at"]
        finally:
            await client.close()


@pytest.mark.parametrize("revocation", ["stop", "age", "exclusion"])
async def test_deferred_details_cannot_bypass_stop_age_or_exclusions(environment, monkeypatch, revocation):
    store, catalog = environment
    _, wide = await overlapping_filters(environment, 101, "Nike")
    await catalog.toggle_filter(101, wide.id, "require-exclusions")
    calls, available = [], [False]

    class Details:
        async def details(self, item):
            calls.append(item.source_item_id)
            if not available[0]:
                raise ParseFailure("ParserBusy")
            return SimpleNamespace(
                photo_urls=item.photo_urls, photos_complete=True,
                description="Small hole", publication_evidence="exact",
                published_at=item.published_at, fetched_at=utcnow(),
            )

    notifier = Notifier(store, Bot(), parser=Details())
    await notifier.once("prepare")
    if revocation == "stop":
        await store.control.stop(101, "stop-during-capacity-wait")
    elif revocation == "age":
        tick = utcnow() + timedelta(seconds=61)
        monkeypatch.setattr("vpars_notifier.service.utcnow", lambda: tick)
        notifier.next_expiry_check = 0
    available[0] = True
    await due(store, Delivery)
    await notifier.once("prepare")
    await notifier.once("ready")
    assert not notifier.bot.calls
    assert (await rows(store, Delivery))[0].state in {"cancelled", "expired", "excluded"}
    assert len(calls) == (2 if revocation == "exclusion" else 1)


@pytest.mark.parametrize("code", [
    "ParserBusy", "GatewayResourceExhausted", "ProxyBudgetWait", "ProxyBudgetExceeded",
    "ProxyBudgetNotStarted", "ProxyBudgetUnavailable"
])
async def test_capacity_wait_keeps_catalog_cursor_and_real_failure_budget(environment, code):
    store, _ = environment
    await start(environment)
    parser = Parser()
    parser.more = True
    monitor = Monitor(store, parser)
    await monitor.once()
    original = (await rows(store, Job))[0]
    streaks = dict(store.head_streak)
    parser.error = code
    for _ in range(4):
        await due(store, Job)
        await monitor.once()
    saved = (await rows(store, Job))[0]
    assert saved.failures == original.failures
    assert saved.scan_state == original.scan_state
    assert saved.coverage == original.coverage and saved.page == original.page
    assert saved.checkpoint == original.checkpoint and saved.state == "waiting"
    assert store.head_streak == streaks


async def test_budget_wait_is_shared_briefly_and_cannot_extend_itself(monkeypatch):
    tick = [100.0]
    monkeypatch.setattr("vpars_worker.parser.monotonic", lambda: tick[0])
    client = ParserClient()
    try:
        client.remember_failure("FR", ParseFailure("ProxyBudgetWait", retry_after=147))
        assert not client.cooldowns
        for region in ("FR", "IT", "DE", "GB"):
            with pytest.raises(ParseFailure) as caught:
                client.check_cooldown(region)
            assert caught.value.code == "ProxyBudgetWait" and caught.value.retry_after == 2
        tick[0] += 1.1
        with pytest.raises(ParseFailure) as caught:
            client.check_cooldown("DE")
        assert caught.value.retry_after == 1
        tick[0] += 1
        client.check_cooldown("FR")
        client.remember_failure("GB", ParseFailure("SourceRateLimited", retry_after=120))
        with pytest.raises(ParseFailure) as caught:
            client.check_cooldown("UK")
        assert caught.value.code == "SourceRateLimited" and caught.value.retry_after == 120
    finally:
        await client.close()
