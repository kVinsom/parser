import asyncio
from datetime import timedelta
from time import perf_counter
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_catchup import MovingCatalog, setup_catalog
from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute, Money, ParseRequest

from vpars_worker.db import Delivery, Job
from vpars_worker.matching import validate_result
from vpars_worker.monitor import Monitor
from vpars_worker.page_cache import PageCache


async def test_timed_out_head_adapts_across_restarts_without_advancing_checkpoint(environment):
    parser = MovingCatalog()
    store, _ = await setup_catalog(environment, parser)
    checkpoint = (await rows(store, Job))[0].checkpoint
    parser.publish()
    parser.error = "GatewayTimeout"
    for expected in (20, 24, 24):
        await due(store, Job)
        # A new monitor has no in-memory record of previous failures.
        await Monitor(store, parser).once()
        job = (await rows(store, Job))[0]
        assert parser.calls[-1].page == 1
        assert parser.calls[-1].timeout_seconds == expected
        assert job.checkpoint == checkpoint and job.coverage == "catchup_error"
        assert not await rows(store, Delivery)
    parser.error = None
    await due(store, Job)
    await Monitor(store, parser).once()
    job = (await rows(store, Job))[0]
    assert parser.calls[-1].timeout_seconds == 24
    assert job.scheduling["head_timeouts"] == 0
    assert job.checkpoint == checkpoint  # The successful head still has deeper work.


def test_extended_head_budget_respects_explicit_caller_limit():
    from vpars_worker.scheduling import head_timeout

    peers = [SimpleNamespace(scheduling={}), SimpleNamespace(scheduling={"head_timeouts": 2})]
    assert head_timeout(peers, configured=8) == 8
    assert head_timeout(peers) == 24


def test_only_an_unconfirmed_first_baseline_keeps_time_for_cold_browser():
    from vpars_worker.scheduling import head_timeout

    cold = SimpleNamespace(scheduling={}, baseline_ready=False, checks=0)
    warm = SimpleNamespace(scheduling={}, baseline_ready=True, checks=1)
    assert head_timeout([cold]) == 40
    assert head_timeout([cold], configured=8) == 8
    assert head_timeout([cold, warm]) == 40
    assert head_timeout([warm]) == 20
    cold.baseline_ready = True
    cold.checks = 1
    assert head_timeout([cold, warm]) == 20


@pytest.mark.parametrize('user,brand', [(101,'Nike'),(202,'Gucci')])
async def test_real_start_uses_cold_budget_once_then_returns_to_warm_polling(environment,user,brand):
    store,catalog=environment
    if user!=101:
        await catalog.register(user,user,'second','Second',2,start=True)
    await catalog.save(user,Filters(brands=[brand],sources={'vinted_fr':['FR']}),'cold-save')
    await store.control.start(user,'cold-start')
    parser=Parser(items=[lot('baseline',brand=Attribute(raw=brand,normalized=brand.lower()))])
    monitor=Monitor(store,parser)
    await monitor.once()
    assert len(parser.calls)==1 and parser.calls[0].timeout_seconds==40
    assert all(job.checks==1 for job in await rows(store,Job))
    assert not await rows(store,Delivery)
    await due(store,Job)
    await monitor.once()
    assert parser.calls[-1].timeout_seconds==20


@pytest.mark.parametrize("code", ["SourceRateLimited", "AccessBlocked", "UnexpectedContent"])
async def test_non_timeout_failure_does_not_extend_head_budget(environment, code):
    store, _ = environment
    await start(environment)
    parser = Parser()
    parser.error = code
    await Monitor(store, parser).once()
    job = (await rows(store, Job))[0]
    assert not job.scheduling.get("head_timeouts")


@pytest.mark.parametrize(
    "code,explanation",
    [
        ("GatewayTimeout", "завантажувалася надто довго"),
        ("GatewayServiceError", "Помилка сервісу завантаження"),
        ("UnexpectedContent", "Перегляд ще не завершений"),
        ("AccessBlocked", "Vinted обмежив доступ"),
        ("SourceRateLimited", "Vinted обмежив частоту"),
    ],
)
async def test_status_explains_distinct_regional_failures(environment, code, explanation):
    store, _ = environment
    await start(environment)
    parser = Parser()
    parser.error = code
    await Monitor(store, parser).once()
    problems = await store.control.problems(101)
    assert len(problems) == 1 and explanation in problems[0]
    assert await store.control.problems(202) == []


async def test_confirmed_empty_offset_finishes_small_catalog_without_hiding_new_lots(environment):
    class SmallCatalog(MovingCatalog):
        async def parse(self, request):
            result = await super().parse(request)
            # A populated page has no trustworthy next control. Its following
            # empty page has the explicit source proof tested in parser/gateway.
            result.pagination.has_more = "unknown" if result.items else "false"
            result.pagination.next_page = None
            result.empty_confirmed = not result.items
            # Match the real worker path, which validates the parser's wire result.
            validate_result(request, type(result).model_validate_json(result.model_dump_json()))
            return result

    parser = SmallCatalog(size=96)
    store, monitor = await setup_catalog(environment, parser)
    initial = (await rows(store, Job))[0]
    assert initial.baseline_ready and initial.baseline_pages == [1, 2]
    parser.publish()
    previous = initial.checkpoint
    confirmed = False
    for _ in range(12):
        await due(store, Job)
        await monitor.once()
        job = (await rows(store, Job))[0]
        confirmed |= bool(job.scan_state and job.scan_state["step"] == "confirm_head")
        if job.coverage == "catchup_complete":
            break
        assert job.checkpoint == previous
    assert confirmed and job.coverage == "catchup_complete"
    assert {d.item["source_item_id"] for d in await rows(store, Delivery)} == parser.expected


async def test_one_upstream_page_serves_different_sizes_and_prices(environment):
    store, catalog = environment
    searches = []
    for size, maximum in [("L", "30"), ("XL", "40"), ("M", "20")]:
        searches.append(
            await catalog.save(
                101,
                Filters(
                    brands=["Stone Island"],
                    sizes=[size],
                    native_prices={"EUR": {"maximum": maximum}},
                    sources={"vinted_fr": ["FR"]},
                ),
                "save-" + size,
            )
        )
    await store.control.start(101, "start-all")
    baseline = Parser(items=[lot("999")])
    await Monitor(store, baseline).once()
    assert len(baseline.calls) == 1 and not await rows(store, Delivery)
    await due(store, Job)
    parser = Parser(
        items=[
            lot("10001"),
            lot("10002", size=Attribute(raw="XL", normalized="xl")),
            lot("10003", size=Attribute(raw="M", normalized="m")),
            lot(
                "10004",
                size=Attribute(raw="M", normalized="m"),
                price=Money(amount="19", currency="EUR"),
            ),
        ]
    )
    await Monitor(store, parser).once()
    assert len(parser.calls) == 1
    deliveries = {row.item["source_item_id"]: row for row in await rows(store, Delivery)}
    assert set(deliveries) == {"10001", "10002", "10004"}
    for identity, search in zip(("10001", "10002", "10004"), searches, strict=True):
        assert [c["token"]["search_id"] for c in deliveries[identity].candidates] == [search.id]
    assert len({job.query_key for job in await rows(store, Job)}) == 1


async def test_slow_two_regions_do_not_block_the_next_region_slots():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Store:
        registry = SimpleNamespace(
            sources={
                "vinted": SimpleNamespace(
                    provider="vinted",
                    regions=["GB", "FR", "IT", "DE"],
                )
            }
        )

        async def ingest(self):
            pass

        async def claim(self, region):
            return [SimpleNamespace(query={"region_id": region}, page=1, scheduling={})]

        async def finish(self, *args, **kwargs):
            return True

    class SlowParser:
        async def parse(self, request):
            calls.append((request.region_id, perf_counter()))
            if len(calls) >= 4:
                started.set()
            await release.wait()
            return SimpleNamespace(
                region_id=request.region_id,
                items=[],
                cards_seen=0,
                cards_skipped=0,
                completeness="complete",
            )

    task = asyncio.create_task(Monitor(Store(), SlowParser(), interval=0.04).run())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert [r for r, _ in calls] == ["GB", "FR", "IT", "DE"]
        gaps = [b[1] - a[1] for a, b in zip(calls, calls[1:], strict=False)]
        assert all(0.009 <= gap < 0.039 for gap in gaps)
        await asyncio.sleep(0.1)
        assert len(calls) == 4  # Never stack another request into an occupied regional lane.
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_fresh_lane_bypasses_backlog_while_fifo_lane_preserves_it(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot("10001"), lot("10002")])).once()
    async with store.sessions.begin() as session:
        for row in await session.scalars(select(Delivery)):
            row.created_at = utcnow() - timedelta(
                seconds=120 if row.item["source_item_id"] == "10001" else 1
            )
            row.media_progress = {"dispatch_ready": True}
    fresh = await Notifier(store, Bot()).claim(lane="ready", fresh_first=True)
    old = await Notifier(store, Bot()).claim(lane="ready")
    assert fresh.item["source_item_id"] == "10002"
    assert old.item["source_item_id"] == "10001"


async def test_timed_head_refresh_survives_restart_and_keeps_deep_continuation(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    await due(store, Job)
    await monitor.once()
    async with store.sessions.begin() as session:
        job = await session.scalar(select(Job))
        assert job.page == 2
        job.scan_state = {
            **job.scan_state,
            "head_checked_at": (utcnow() - timedelta(seconds=30)).isoformat(),
        }
        job.scheduling = {"head_attempt_at": (utcnow() - timedelta(seconds=30)).isoformat()}
        checkpoint = job.checkpoint
    await due(store, Job)
    claimed = (await store.claim())[0]
    assert claimed.page == 1 and claimed.scan_state["refresh_page"] == 2
    await store.recover_executions()
    await monitor.once()
    job = (await rows(store, Job))[0]
    assert job.page == 2 and job.checkpoint == checkpoint
    assert job.scan_state["resume_after_refresh"]
    await due(store, Job)
    await monitor.once()
    assert parser.calls[-1].page == 2
    for _ in range(150):
        await due(store, Job)
        await monitor.once()
        if (await rows(store, Job))[0].coverage == "catchup_complete":
            break
    assert (await rows(store, Job))[0].coverage == "catchup_complete"
    assert {d.item["source_item_id"] for d in await rows(store, Delivery)} == parser.expected | {
        "108",
        "109",
    }


@pytest.mark.parametrize(
    "step,phase,page",
    [
        ("forward", "scan", 1),
        ("forward", "verify", 2),
        ("overlap", "scan", 2),
        ("confirm_head", "verify", 2),
        ("forward", "baseline", 2),
    ],
)
async def test_cache_never_replaces_head_overlap_or_verification(step, phase, page):
    from vpars_parser.models import ParseRequest

    request = ParseRequest(region_id="FR", page=page)
    result = await Parser().parse(request)
    result.identities_complete = True
    now = utcnow()
    cache = PageCache()
    cache.put(request, result, [SimpleNamespace(id="a")], now)
    peer = SimpleNamespace(
        id="b",
        query=request.model_dump(),
        page=page,
        failures=0,
        baseline_ready=True,
        last_checked=now - timedelta(seconds=1),
        scan_state={"phase": phase, "step": step},
    )
    assert cache.get([peer], now) is None


async def test_recent_deep_cache_is_consumed_once_per_peer_and_expires():
    from vpars_parser.models import ParseRequest

    request = ParseRequest(region_id="FR", page=2)
    result = await Parser().parse(request)
    result.identities_complete = True
    now = utcnow()
    cache = PageCache()
    cache.put(request, result, [SimpleNamespace(id="a")], now)
    peer = SimpleNamespace(
        id="b",
        query=request.model_dump(),
        page=2,
        failures=0,
        baseline_ready=True,
        last_checked=now - timedelta(seconds=1),
        scan_state={"phase": "scan", "step": "forward"},
    )
    cached = cache.get([peer], now)
    assert cached == result and cached is not result
    assert cache.get([peer], now) is None
    peer.id = "c"
    assert cache.get([peer], now + timedelta(seconds=31)) is None


async def test_priority_head_serves_filters_with_different_deep_cursors(environment):
    store, catalog = environment
    for size in ("L", "XL"):
        await catalog.save(
            101,
            Filters(brands=["Stone Island"], sizes=[size], sources={"vinted_fr": ["FR"]}),
            "save-" + size,
        )
    await store.control.start(101, "start-all")
    parser = MovingCatalog()
    monitor = Monitor(store, parser)
    for _ in range(2):
        await due(store, Job)
        await monitor.once()
    parser.publish()
    await due(store, Job)
    await monitor.once()
    async with store.sessions.begin() as session:
        jobs = list(await session.scalars(select(Job).order_by(Job.id)))
        for job, page in zip(jobs, (4, 7), strict=True):
            job.page = page
            job.scan_state = {**job.scan_state, "step": "forward"}
            job.scheduling = {"head_attempt_at": (utcnow() - timedelta(seconds=40)).isoformat()}
        checkpoints = {j.id: j.checkpoint for j in jobs}
    parser.calls.clear()
    parser.ids.insert(0, "99999")
    await due(store, Job)
    await monitor.once()
    assert [call.page for call in parser.calls] == [1]
    assert parser.calls[0].timeout_seconds == 20
    jobs = sorted(await rows(store, Job), key=lambda j: j.id)
    assert [j.page for j in jobs] == [4, 7]
    assert {j.id: j.checkpoint for j in jobs} == checkpoints
    assert all(j.scan_state["step"] == "forward" for j in jobs)
    parser.calls.clear()
    await due(store, Job)
    await monitor.once()
    assert parser.calls[0].page > 1 and parser.calls[0].timeout_seconds == 24


def test_eight_query_heads_stay_within_48_seconds_and_deep_work_is_fair():
    from vpars_worker.scheduling import choose

    now = utcnow()
    groups = {}
    heads = {}
    deep = {}
    for region in ("GB", "FR", "IT", "DE"):
        groups[region] = []
        for query in ("one", "two"):
            key = region + query
            heads[key], deep[key] = [], []
            groups[region].append(
                SimpleNamespace(
                    id=key,
                    query_key=key,
                    next_due=now,
                    page=5,
                    baseline_ready=True,
                    scan_state={"phase": "scan", "step": "forward", "started_at": now.isoformat()},
                    scheduling={},
                )
            )
    streaks = dict.fromkeys(groups, 0)
    for slot in range(240):
        tick = now + timedelta(seconds=slot * 4)
        region = list(groups)[slot % 4]
        jobs = choose(groups[region], tick, streaks[region])
        assert len(jobs) == 1
        job = jobs[0]
        if job.page == 1:
            heads[job.id].append(slot * 4)
            job.scheduling = {**job.scheduling, "head_attempt_at": tick.isoformat()}
            job.page = job.scan_state.pop("refresh_page")
            job.scan_state["step"] = job.scan_state.pop("refresh_step")
            streaks[region] += 1
        else:
            deep[job.id].append(slot * 4)
            job.scheduling = {**job.scheduling, "deep_attempt_at": tick.isoformat()}
            streaks[region] = 0
    assert all(
        max(b - a for a, b in zip(times, times[1:], strict=False)) <= 48 for times in heads.values()
    )
    assert all(len(times) >= 5 for times in deep.values())


@pytest.mark.parametrize("region,brand", [("FR", "Nike"), ("DE", "Gucci"),
                                        ("IT", "Adidas"), ("GB", "Amiri")])
def test_initial_baseline_continues_after_one_head_for_all_matching_users(region, brand):
    from vpars_worker.scheduling import choose

    now = utcnow()
    mature = SimpleNamespace(id="mature", query_key=region + ":other", next_due=now,
        page=4, baseline_ready=True, scan_state={"phase": "scan", "step": "forward",
        "started_at": (now - timedelta(seconds=90)).isoformat()}, scheduling={})
    baseline = [SimpleNamespace(id=str(owner), user_id=owner, query_key=region + ":" + brand,
        next_due=now, page=2, baseline_ready=False, scan_state={"phase": "baseline"},
        checkpoint={"original": owner}, scheduling={}) for owner in (101, 202)]
    assert choose([mature, *baseline], now, head_streak=1) == baseline
    assert all(j.page == 2 and not j.baseline_ready for j in baseline)
    assert [j.checkpoint for j in baseline] == [{"original": 101}, {"original": 202}]
    assert mature.page == 4  # Choosing a continuation cannot mutate another head.
    for peer in baseline:
        peer.scheduling = {"deep_due_at": (now + timedelta(seconds=30)).isoformat()}
    assert choose([mature, *baseline], now, head_streak=4) == [mature]
    assert all(j.page == 2 for j in baseline)  # Durable retry cooldown still applies.


def test_baseline_continuations_cannot_starve_due_head_checks():
    from vpars_worker.scheduling import choose

    now = utcnow()
    head = SimpleNamespace(id="head", query_key="head", next_due=now, page=1,
        baseline_ready=True, scan_state={}, scheduling={})
    baseline = SimpleNamespace(id="baseline", query_key="baseline", next_due=now,
        page=2, baseline_ready=False, scan_state={"phase": "baseline"}, scheduling={})
    assert choose([head, baseline], now, head_streak=0) == [head]
    assert choose([head, baseline], now, head_streak=1) == [baseline]


async def test_deep_failure_backoff_survives_restart_without_delaying_head(
    environment, monkeypatch
):
    import vpars_worker.monitor as module
    from vpars_worker.monitor import Store

    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    await due(store, Job)
    await monitor.once()
    await due(store, Job)
    claimed = (await store.claim())[0]
    before = claimed.checkpoint
    from vpars_bot.db import SearchRun

    async with store.sessions() as session:
        run = await session.get(SearchRun, (claimed.token["search_id"], claimed.token["source_id"]))
        active_until = run.active_until
    await store.fail(claimed, "UnexpectedContent")
    async with store.sessions() as session:
        run = await session.get(SearchRun, (claimed.token["search_id"], claimed.token["source_id"]))
        assert run.state == "active" and run.active_until == active_until
    state = (await rows(store, Job))[0]
    assert state.page == 2 and state.scheduling["deep_failures"] == 1
    store = Store(store.sessions, store.registry)
    assert not await store.claim()  # Neither a premature retry nor a fake end.
    tick = utcnow() + timedelta(seconds=25)
    monkeypatch.setattr(module, "utcnow", lambda: tick)
    claimed = (await store.claim())[0]
    assert claimed.page == 1 and claimed.scan_state["refresh_page"] == 2
    result = await parser.parse(ParseRequest(**claimed.query))
    await store.finish(claimed, result, interval=4, max_pages=3)
    state = (await rows(store, Job))[0]
    assert state.page == 2 and state.checkpoint == before
    assert state.scheduling["deep_failures"] == 1
    tick += timedelta(seconds=6)  # Deep retry is due; the refreshed head is not yet due.
    claimed = (await store.claim())[0]
    assert claimed.page == 2
