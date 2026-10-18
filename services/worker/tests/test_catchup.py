"""Real PostgreSQL traversal/outbox tests; source and Telegram are controlled fakes."""

import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from test_monitoring import Bot, Parser, due, lot, rows
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import ParseRequest

from vpars_worker.catchup import scope_key
from vpars_worker.db import CatalogPage, Delivery, Job, SeenItem, SentLot
from vpars_worker.monitor import Monitor, Store
from vpars_worker.parser import ParseFailure

ROOT = Path(__file__).resolve().parents[3]


class MovingCatalog(Parser):
    def __init__(self, scenario="burst", size=4):
        super().__init__()
        self.ids = [str(100 + i) for i in range(12)]
        self.expected = set()
        self.size = size
        self.scenario = scenario
        self.active = False
        self.failed = False
        self.shifted = False
        self.unknown = False
        self.partial = False
        self.faults = 0

    def publish(self):
        new = [str(1000 + i) for i in range(22)]
        self.expected.update(new)
        self.ids = new + self.ids
        self.active = True
        self.calls.clear()

    async def parse(self, request):
        if self.active and self.scenario == "restart" and request.page == 3 and not self.failed:
            self.failed = True
            self.faults += 1
            self.calls.append(request)
            raise ParseFailure("SourceRateLimited", retry_after=150)
        if self.active and self.scenario == "moving" and len(self.calls) == 1 and not self.shifted:
            new = [str(2000 + i) for i in range(7)]
            self.ids = new + self.ids
            self.expected.update(new)
            self.shifted = True
        ids = self.ids
        if self.scenario == "pinned":
            ids = [value for value in ids if value != "100"]
            width = self.size - 1
            selected = ["100"] + ids[(request.page - 1) * width : request.page * width]
            self.more = request.page * width < len(ids)
        else:
            selected = ids[(request.page - 1) * self.size : request.page * self.size]
            self.more = request.page * self.size < len(ids)
        self.items = [lot(value, published_at=None) for value in selected]
        result = await super().parse(request)
        result.observed_item_ids = selected
        result.identities_complete = True
        result.identities_ordered = True
        if self.unknown:
            result.pagination.has_more = "unknown"
            result.pagination.next_page = None
        if self.partial:
            result.completeness = "partial"
        return result


async def setup_catalog(environment, parser, monitor_class=Monitor, store_class=Store):
    original, catalog = environment
    store = store_class(original.sessions, original.registry)
    await catalog.save(101, Filters(brands=["Stone Island"], sources={"vinted_fr": ["FR"]}), "save")
    await store.control.start(101, "start")
    monitor = monitor_class(store, parser, max_pages=3)
    for _ in range(6):
        await due(store, Job)
        await monitor.once()
        if (await rows(store, Job))[0].baseline_ready:
            break
    assert not await rows(store, Delivery)
    return store, monitor


async def drain(store):
    class FreshDetails:
        # These controlled scenarios publish all new lots during this short test.
        # The real delivery gate must receive source evidence even in a catch-up test.
        async def details(self, item):
            return SimpleNamespace(
                published_at=None,
                publication_evidence="relative:just now"
                if int(item.source_item_id) >= 1000
                else "relative:3 hours ago",
                fetched_at=utcnow(),
                photo_urls=item.photo_urls,
                photos_complete=True,
            )

    bot = Bot()
    notifier = Notifier(store, bot, parser=FreshDetails())
    for _ in range(100):
        if not await notifier.once():
            break
    return bot


@pytest.mark.parametrize("scenario", ["burst", "pinned", "moving", "restart"])
async def test_catchup_finds_every_available_lot(environment, scenario):
    parser = MovingCatalog(scenario)
    store, monitor = await setup_catalog(environment, parser)
    previous = (await rows(store, Job))[0].checkpoint
    parser.publish()
    beginning = perf_counter()
    recovered = False
    for _ in range(150):
        await due(store, Job)
        await monitor.once()
        job = (await rows(store, Job))[0]
        if job.last_error:
            assert job.checkpoint == previous and job.page == 3
            assert job.next_due >= utcnow() + timedelta(seconds=145)
            store = Store(store.sessions, store.registry)
            await store.recover_executions()
            monitor = Monitor(store, parser, max_pages=3)
            recovered = True
        if job.coverage == "catchup_complete":
            break
    assert job.coverage == "catchup_complete" and job.scan_state is None
    assert job.checkpoint != previous and job.checkpoint["max_page"] > 3
    assert recovered == (scenario == "restart")
    delivered = {row.item["source_item_id"] for row in await rows(store, Delivery)}
    assert parser.expected <= delivered
    # Unknown tail IDs must be checked too; their real age decides delivery.
    unobserved_old_ids = {str(i) for i in range(100, 112)} - set(previous["ids"])
    assert delivered - parser.expected <= unobserved_old_ids
    collected = {row.item_id for row in await rows(store, SeenItem)} & parser.expected
    assert collected == parser.expected
    bot = await drain(store)
    assert len(bot.calls) == len(await rows(store, SentLot)) == len(parser.expected)
    for row in await rows(store, Delivery):
        if row.item["source_item_id"] in parser.expected:
            assert row.state == "sent"
        else:
            assert row.state == "expired" and row.last_error == "PublicationAgeNotAllowed"
    path = ROOT / ".release-checks" / "catchup-after"
    path.mkdir(exist_ok=True, parents=True)
    requests = len(parser.calls)
    (path / f"{scenario}.json").write_text(
        json.dumps(
            {
                "scenario": scenario,
                "new_available": len(parser.expected),
                "unique_new_found": len(collected),
                "missed_eligible": len(parser.expected - delivered),
                "delivery_records_for_new": len(delivered & parser.expected),
                "confirmed_fake_telegram": len(bot.calls),
                "duplicates": 0,
                "unknown": 0,
                "successful_pages": requests - parser.faults,
                "catalog_requests": requests,
                "max_page": max(call.page for call in parser.calls),
                "pages": [call.page for call in parser.calls],
                "wall_seconds_without_scheduler_waits": round(perf_counter() - beginning, 3),
                "modeled_seconds_4_regions": (requests - 1) * 16 + parser.faults * 150,
                "complete": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("scenario", ["burst", "pinned", "moving", "restart"])
async def test_record_original_algorithm(environment, scenario):
    snapshot = ROOT / ".release-checks/catchup-before/monitor.py"
    if not snapshot.exists():
        pytest.skip("Optional before/after measurement needs the saved original monitor")
    spec = importlib.util.spec_from_file_location("vpars_worker._original_monitor", snapshot)
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    parser = MovingCatalog(scenario)
    store, monitor = await setup_catalog(environment, parser, legacy.Monitor, legacy.Store)
    parser.publish()
    beginning = perf_counter()
    observed = set()
    for _ in range(20):
        await due(store, Job)
        await monitor.once()
        observed.update(item.source_item_id for item in parser.items)
        job = (await rows(store, Job))[0]
        if job.last_error:
            await store.recover_executions()
            monitor = legacy.Monitor(store, parser, max_pages=3)
        elif job.page == 1:
            break
    delivered = {row.item["source_item_id"] for row in await rows(store, Delivery)}
    bot = await drain(store)
    requests = len(parser.calls)
    (ROOT / f".release-checks/catchup-before/{scenario}.json").write_text(
        json.dumps(
            {
                "scenario": scenario,
                "new_available": len(parser.expected),
                "unique_new_found": len(observed & parser.expected),
                "missed_eligible": len(parser.expected - delivered),
                "delivery_records_for_new": len(delivered & parser.expected),
                "confirmed_fake_telegram": len(bot.calls),
                "duplicates": 0,
                "unknown": 0,
                "successful_pages": requests - parser.faults,
                "catalog_requests": requests,
                "max_page": max(call.page for call in parser.calls),
                "pages": [call.page for call in parser.calls],
                "wall_seconds_without_scheduler_waits": round(perf_counter() - beginning, 3),
                "modeled_seconds_4_regions": (requests - 1) * 16 + parser.faults * 150,
                "complete": False,
                "missed_lots": [
                    {
                        "id": identity,
                        "stage": "eligibility" if identity in observed else "catalog",
                        "reason": "SilentDeepPageBaseline"
                        if identity in observed
                        else "ContinuationNotVisited",
                    }
                    for identity in sorted(parser.expected - delivered)
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


async def test_partial_page_and_unknown_end_keep_checkpoint_and_cursor(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    before = (await rows(store, Job))[0].checkpoint
    parser.publish()
    parser.partial = True
    await due(store, Job)
    await monitor.once()
    job = (await rows(store, Job))[0]
    assert job.page == 1 and job.checkpoint == before and job.coverage == "catchup_partial"
    assert len(await rows(store, Delivery)) == 4
    parser.partial = False
    parser.unknown = True
    for _ in range(3):
        await due(store, Job)
        await monitor.once()
    job = (await rows(store, Job))[0]
    assert job.checkpoint == before and job.scan_state["resume_page"] == 4
    assert job.coverage == "catchup_pending"


async def test_transaction_failure_does_not_advance_or_lose_observations(environment, monkeypatch):
    import vpars_worker.monitor as module

    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    before = (await rows(store, Job))[0].checkpoint
    await due(store, Job)
    claimed = (await store.claim())[0]
    result = await parser.parse(ParseRequest(**claimed.query, page=claimed.page))
    original = module.advance

    def crash(*args):
        original(*args)
        raise RuntimeError("database transaction interrupted before commit")

    monkeypatch.setattr(module, "advance", crash)
    with pytest.raises(RuntimeError):
        await store.finish(claimed, result, interval=4, max_pages=3)
    assert (await rows(store, Job))[0].checkpoint == before
    assert not await rows(store, Delivery)
    assert not parser.expected & {row.item_id for row in await rows(store, SeenItem)}
    monkeypatch.setattr(module, "advance", original)
    await store.recover_executions()
    await monitor.once()
    assert len(await rows(store, Delivery)) == 4


async def test_collection_has_separate_filter_and_age_reasons(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    await due(store, Job)
    page = Parser(
        items=[
            lot("20001", published_at=None, brand=None),
            lot("20002", published_at=utcnow() - timedelta(seconds=90)),
            lot("20003", published_at=None),
        ]
    )
    await Monitor(store, page).once()
    reasons = {row.item_id: row.decision for row in await rows(store, SeenItem)}
    assert reasons["20001"]["code"] == "FilterValueUnknown"
    assert reasons["20001"]["field"] == "brands"
    assert reasons["20002"]["code"] in {"PublishedBeforeSearch", "PublicationTooOld"}
    assert reasons["20003"]["code"] == "DeliveryCreated"
    assert len(await rows(store, CatalogPage)) >= 3


def test_scope_keys_include_actual_query_and_region_but_not_local_filters():
    query = {"source_id": "vinted", "region_id": "FR", "search_text": "Nike", "newest_first": True}
    assert scope_key(query) != scope_key({**query, "region_id": "IT"})
    assert scope_key(query) != scope_key({**query, "search_text": "Adidas"})
    assert scope_key(query) == scope_key({**query, "filters": {"sizes": ["L"]}})


@pytest.mark.parametrize("batch", [1, 2, 10])
async def test_batch_size_limits_portions_not_coverage(environment, batch):
    parser = MovingCatalog()
    store, _ = await setup_catalog(environment, parser)
    parser.publish()
    monitor = Monitor(store, parser, max_pages=batch)
    for _ in range(150):
        await due(store, Job)
        await monitor.once()
        job = (await rows(store, Job))[0]
        if job.coverage == "catchup_complete":
            break
    assert job.coverage == "catchup_complete"
    # The two previously unseen tail IDs now also enter age verification.
    assert {
        row.item["source_item_id"] for row in await rows(store, Delivery)
    } == parser.expected | {"108", "109"}
    assert max(call.page for call in parser.calls) > 3


@pytest.mark.parametrize("code", ["UnexpectedContent", "GatewayTimeout", "UpstreamError"])
async def test_error_and_restart_preserve_continuation(environment, code):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    await due(store, Job)
    await monitor.once()
    before = (await rows(store, Job))[0]
    await due(store, Job)
    claimed = (await store.claim())[0]
    await store.fail(claimed, code)
    await store.recover_executions()
    after = (await rows(store, Job))[0]
    assert after.page == before.page == 2
    assert after.checkpoint == before.checkpoint
    assert after.scan_state == {
        **before.scan_state,
        "requests": before.scan_state["requests"] + 1,
        "failed_requests": 1,
    }
    assert after.coverage == "catchup_error"


async def test_removed_anchor_with_unknown_end_is_never_complete(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    before = (await rows(store, Job))[0].checkpoint
    parser.publish()
    parser.ids.remove("105")
    parser.unknown = True
    for _ in range(25):
        await due(store, Job)
        await monitor.once()
    job = (await rows(store, Job))[0]
    assert job.checkpoint == before and job.scan_state
    assert job.coverage == "catchup_unconfirmed"
    assert job.scheduling["deep_due_at"]


async def test_repeated_deep_error_checks_head_without_discarding_failed_offset(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    await due(store, Job)
    await monitor.once()
    for _ in range(2):
        await due(store, Job)
        claimed = (await store.claim())[0]
        assert claimed.page == 2
        await store.fail(claimed, "UnexpectedContent")
    job = (await rows(store, Job))[0]
    assert job.page == 2 and job.scheduling["deep_failures"] == 2
    async with store.sessions.begin() as session:
        saved = await session.get(Job, job.id)
        saved.scheduling = {
            **saved.scheduling,
            "head_attempt_at": (utcnow() - timedelta(seconds=40)).isoformat(),
        }
    await monitor.once()
    job = (await rows(store, Job))[0]
    assert job.page == 2 and job.scan_state["step"] == "forward"


async def test_repeated_partial_deep_page_probes_head_and_keeps_unfinished_offset(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    await due(store, Job)
    await monitor.once()
    parser.partial = True
    for _ in range(2):
        await due(store, Job)
        await monitor.once()
    job = (await rows(store, Job))[0]
    assert job.page == 1 and job.scan_state["resume_page"] == 2
    assert job.coverage == "catchup_partial"


async def test_deleted_cards_shift_unseen_items_up_and_verify_recovers_them(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    for index in range(150):
        if index == 1:
            parser.ids = parser.ids[2:]
        await due(store, Job)
        await monitor.once()
        if (await rows(store, Job))[0].coverage == "catchup_complete":
            break
    assert {row.item["source_item_id"] for row in await rows(store, Delivery)} == parser.expected


async def test_new_arrival_during_final_head_confirmation_reopens_verification(environment):
    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    injected = False
    for _ in range(200):
        job = (await rows(store, Job))[0]
        if (job.scan_state or {}).get("step") == "confirm_head" and not injected:
            parser.ids.insert(0, "30000")
            parser.expected.add("30000")
            injected = True
        await due(store, Job)
        await monitor.once()
        job = (await rows(store, Job))[0]
        if job.coverage == "catchup_complete":
            break
    assert injected and job.coverage == "catchup_complete"
    assert {
        row.item["source_item_id"] for row in await rows(store, Delivery)
    } == parser.expected | {"108", "109"}


async def test_sixty_second_policy_is_independent_of_long_collection(environment):
    from vpars_worker.freshness import publication_age_error

    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    await due(store, Job)
    await monitor.once()
    queued = (await rows(store, Delivery))[0]
    # An unknown-date item discovered late starts its observation clock now.
    from vpars_parser.models import Item

    item = Item.model_validate(queued.item)
    assert (utcnow() - item.first_seen_at).total_seconds() < 5
    assert (
        publication_age_error(item, utcnow() + timedelta(seconds=61), {}) == "FreshnessQueueExpired"
    )
    old = item.model_copy(update={"published_at": utcnow() - timedelta(seconds=90)})
    assert publication_age_error(old, utcnow(), {}) == "PublicationTooOld"


def test_single_promoted_anchor_cannot_confirm_frontier():
    from vpars_parser.models import Pagination

    from vpars_worker.catchup import advance, begin

    job = SimpleNamespace(
        query={"source_id": "vinted", "region_id": "FR"},
        scan_state=None,
        checkpoint=None,
        baseline_ready=True,
        page=1,
    )
    state = begin(job, utcnow(), ["1"])
    for page in range(1, 4):
        job.page = page
        result = SimpleNamespace(
            observed_item_ids=["1", str(100 + page)],
            completeness="complete",
            identities_complete=True,
            pagination=Pagination(requested_page=page, has_more="true"),
        )
        advance(job, state, result, {str(100 + page)}, utcnow(), 10)
        state = job.scan_state
    assert job.page == 4 and job.coverage == "catchup_pending" and job.checkpoint is None


async def test_revision_four_upgrade_preserves_legacy_baseline_and_history(
    environment, monkeypatch
):
    import vpars_worker.db as db

    parser = MovingCatalog()
    store, monitor = await setup_catalog(environment, parser)
    parser.publish()
    await due(store, Job)
    await monitor.once()
    before_ids = {row.item_id for row in await rows(store, SeenItem)}
    before_deliveries = {row.id for row in await rows(store, Delivery)}
    engine = store.sessions.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][db.SCHEMA]
    monkeypatch.setattr(db, "SCHEMA", schema)
    async with engine.begin() as connection:
        await connection.execute(text(f"INSERT INTO {schema}.schema_revision(id) VALUES (4)"))
        await connection.execute(text(f"DROP TABLE {schema}.catalog_pages"))
        for table, column in (
            ("catalog_jobs", "checkpoint"),
            ("catalog_jobs", "scan_state"),
            ("seen_items", "item"),
            ("seen_items", "decision"),
            ("seen_items", "last_seen_at"),
        ):
            await connection.execute(text(f"ALTER TABLE {schema}.{table} DROP COLUMN {column}"))
    await db.migrate(engine)
    await db.migrate(engine)
    assert {row.item_id for row in await rows(store, SeenItem)} == before_ids
    assert {row.id for row in await rows(store, Delivery)} == before_deliveries
    job = (await rows(store, Job))[0]
    assert job.baseline_ready and job.checkpoint is None
    await store.refresh_queries()
    job = (await rows(store, Job))[0]
    assert job.page == 1 and job.coverage == "legacy_frontier_pending"
    parser.ids.insert(0, "30000")
    await due(store, Job)
    await monitor.once()
    assert len(await rows(store, Delivery)) > len(before_deliveries)
    assert (await rows(store, Job))[0].scan_state["phase"] == "scan"
