import asyncio
from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select
from test_monitoring import Parser, lot
from vpars_bot.db import Search, SearchRun, Subscription, User, utcnow
from vpars_bot.domain import Filters
from vpars_parser.models import Capabilities, ParseRequest

from vpars_worker.activity import SourceActivity
from vpars_worker.parser import ParseFailure, ParserClient


@pytest.mark.parametrize(
    "brand,region,source", [("Nike", "FR", "vinted_fr"), ("Gucci", "GB", "vinted_gb")]
)
async def test_global_demand_follows_all_users_and_saved_search_intent(
    environment, brand, region, source
):
    store, catalog = environment
    await catalog.register(202, 202, "second", "Second", 2, start=True)
    filters = Filters(brands=[brand], sources={source: [region]})
    saved = []
    for user in (101, 202):
        saved.append(await catalog.save(user, filters, f"save-{user}"))
    assert not await store.has_active_searches()  # Saved filters alone spend no traffic.
    for user in (101, 202):
        await store.control.start(user, f"start-{user}")
    assert await store.has_active_searches()  # Works before command ingestion.
    await store.control.stop(101, "stop-101")
    assert await store.has_active_searches()  # Another user's Stop cannot pause this user.
    await store.control.stop(202, "stop-202")
    assert not await store.has_active_searches()
    await store.control.start(101, "restart-101")
    assert await store.has_active_searches()
    async with store.sessions() as session:
        current = list(await session.scalars(select(Search).order_by(Search.user_id)))
        assert [item.filters for item in current] == [item.filters for item in saved]
        assert all(item.enabled and not item.archived for item in current)


@pytest.mark.parametrize(
    "reason",
    ["disabled", "archived", "source_disabled", "expired", "demo", "stopped", "paused_limit"],
)
async def test_ineligible_searches_cannot_keep_proxy_traffic_alive(environment, reason):
    store, catalog = environment
    search = await catalog.save(
        101, Filters(brands=["Nike"], sources={"vinted_fr": ["FR"]}), "save"
    )
    await store.control.start(101, "start")
    assert await store.has_active_searches()
    async with store.sessions.begin() as session:
        if reason in {"disabled", "archived"}:
            item = await session.get(Search, search.id)
            setattr(item, "enabled" if reason == "disabled" else "archived", reason != "disabled")
        elif reason == "source_disabled":
            (await session.get(User, 101)).enabled_sources = []
        elif reason == "expired":
            (await session.get(Subscription, 101)).expires_at = utcnow() - timedelta(seconds=1)
        else:
            (await session.get(SearchRun, (search.id, "vinted_fr"))).state = reason
    assert not await store.has_active_searches()


async def test_last_enabled_filter_pauses_and_explicit_start_resumes(environment):
    store, catalog = environment
    saved = []
    for brand in ("Nike", "Gucci"):
        saved.append(
            await catalog.save(101, Filters(brands=[brand], sources={"vinted_fr": ["FR"]}), brand)
        )
    await store.control.start(101, "start")
    assert await store.has_active_searches()
    await catalog.toggle_filter(101, saved[0].id, "disable-first")
    assert await store.has_active_searches()
    await catalog.toggle_filter(101, saved[1].id, "disable-last")
    assert not await store.has_active_searches()
    await catalog.toggle_filter(101, saved[0].id, "enable-first")
    assert not await store.has_active_searches()  # Selection never invents a Start.
    await store.control.start(101, "restart")
    assert await store.has_active_searches()


async def test_last_stop_cancels_catalog_and_details_then_any_start_resumes(environment):
    store, catalog = environment
    await catalog.save(101, Filters(brands=["Nike"], sources={"vinted_fr": ["FR"]}), "save")
    activity = SourceActivity(store.has_active_searches, poll_interval=0.01)
    entered = [asyncio.Event(), asyncio.Event()]
    ended = [asyncio.Event(), asyncio.Event()]

    async def operation(index):
        entered[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            ended[index].set()

    with pytest.raises(ParseFailure, match="SearchIdle") as idle:
        await activity.run(lambda: operation(0))
    assert idle.value.deferred and not entered[0].is_set()
    await store.control.start(101, "start")
    tasks = [asyncio.create_task(activity.run(lambda i=i: operation(i))) for i in range(2)]
    watcher = asyncio.create_task(activity.watch())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 3)
        await store.control.stop(101, "stop")
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        assert all(isinstance(result, ParseFailure) and result.deferred for result in results)
        assert all(event.is_set() for event in ended)
        assert not activity.requests
        await store.control.start(101, "restart")

        async def resumed():
            return "resumed"

        assert await activity.run(resumed) == "resumed"
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, *tasks, return_exceptions=True)


async def test_gate_retains_owner_cancellation_and_pauses_on_lookup_failure():
    entered, ended = asyncio.Event(), asyncio.Event()

    async def active():
        return True

    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    activity = SourceActivity(active)
    task = asyncio.create_task(activity.run(operation))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set() and not activity.requests

    async def unavailable():
        raise OSError("database unavailable")

    activity.has_searches = unavailable
    entered.clear()
    with pytest.raises(ParseFailure, match="SearchIdle"):
        await activity.run(operation)
    assert not entered.is_set()


async def test_parser_http_stops_both_lanes_without_requests_while_idle():
    active = False
    stall = True
    calls = []
    started = {path: asyncio.Event() for path in ("catalog", "details")}
    disconnected = {path: asyncio.Event() for path in started}

    async def has_searches():
        return active

    async def stalled(request):
        lane = request.match_info["lane"]
        calls.append(lane)
        started[lane].set()
        if not stall:
            query = ParseRequest.model_validate(await request.json())
            parsed = await Parser(items=[]).parse(query)
            parsed.empty_confirmed = True
            parsed.identities_complete = True
            return web.json_response(parsed.model_dump(mode="json"))
        try:
            async with asyncio.timeout(5):
                while request.transport is not None and not request.transport.is_closing():
                    await asyncio.sleep(0.01)
        finally:
            disconnected[lane].set()
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/v1/{lane:catalog}/parse", stalled)
    app.router.add_post("/v1/items/{lane:details}", stalled)
    async with TestServer(app) as server:
        activity = SourceActivity(has_searches)
        client = ParserClient(str(server.make_url("")), activity=activity)
        client.capabilities = Capabilities(
            source_id="vinted",
            version="test",
            aliases={},
            filters={},
            regions=[{"code": "FR", "name": "France", "base_url": "https://www.vinted.fr"}],
        )
        client.expires = float("inf")
        tasks = []
        try:
            for call in (
                lambda: client.parse(ParseRequest(region_id="FR")),
                lambda: client.details(lot()),
            ):
                with pytest.raises(ParseFailure, match="SearchIdle"):
                    await call()
            assert not calls
            active = True
            tasks = [
                asyncio.create_task(client.parse(ParseRequest(region_id="FR"))),
                asyncio.create_task(client.details(lot())),
            ]
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 3)
            active = False
            await activity.refresh()
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
            assert all(isinstance(result, ParseFailure) and result.deferred for result in results)
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in disconnected.values())), 3
            )
            assert sorted(calls) == ["catalog", "details"]
            active = True
            stall = False
            result = await asyncio.wait_for(client.parse(ParseRequest(region_id="FR")), 5)
            assert result.empty_confirmed and calls == ["catalog", "details", "catalog"]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()
