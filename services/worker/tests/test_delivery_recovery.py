from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import SearchRun, Subscription, utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor


async def choose_query(store, text):
    async with store.sessions.begin() as session:
        for job in await session.scalars(select(Job)):
            job.next_due = utcnow() + timedelta(
                seconds=-1 if job.query["search_text"] == text else 600
            )


async def observed_by(store, text):
    await choose_query(store, text)
    await Monitor(store, Parser(items=[lot("10001")])).once(region="FR")


async def prepare_exclusion(environment, *, both_exclude=False):
    store, catalog = environment
    for key, keywords, excluded in (
        ("narrow", ["Stone Island"], ["hole"]),
        ("wide", ["Hoodie"], ["hole"] if both_exclude else []),
    ):
        await catalog.save(
            101,
            Filters(
                keywords=keywords,
                keywords_all=True,
                excluded_keywords=excluded,
                sources={"vinted_fr": ["FR"]},
            ),
            key,
        )
    await store.control.start(101, "start-both")
    await store.ingest()
    for text in ("Stone Island", "Hoodie"):
        await choose_query(store, text)
        await Monitor(store, Parser(items=[lot("999")])).once(region="FR")
    await observed_by(store, "Stone Island")

    class Details:
        calls = 0

        async def details(self, item):
            self.calls += 1
            return SimpleNamespace(
                photo_urls=item.photo_urls,
                photos_complete=True,
                published_at=item.published_at,
                description="small hole",
                fetched_at=utcnow(),
                publication_evidence="exact",
                seller=None,
            )

    notifier = Notifier(store, Bot(), parser=Details())
    assert await notifier.once("prepare")  # Load the description once.
    return notifier


@pytest.mark.parametrize("both_exclude", [False, True])
async def test_late_candidate_rechecks_exclusions_without_reloading_or_resetting_age(
    environment, both_exclude
):
    store, _ = environment
    notifier = await prepare_exclusion(environment, both_exclude=both_exclude)
    assert await notifier.once("ready")
    original = (await rows(store, Delivery))[0]
    assert original.state == "excluded" and not notifier.bot.calls

    # The same search must not continuously resurrect its own rejected lot.
    await observed_by(store, "Stone Island")
    assert (await rows(store, Delivery))[0].state == "excluded"
    assert not await notifier.once()

    await observed_by(store, "Hoodie")
    resumed = (await rows(store, Delivery))[0]
    assert resumed.state == "pending" and len(resumed.candidates) == 2
    assert resumed.item == original.item and resumed.media_progress == original.media_progress
    assert resumed.created_at == original.created_at and resumed.last_error is None
    assert await notifier.once("ready")
    delivered = (await rows(store, Delivery))[0]
    assert delivered.state == ("excluded" if both_exclude else "sent")
    assert len(notifier.bot.calls) == (0 if both_exclude else 1)
    assert notifier.parser.calls == 1
    await observed_by(store, "Hoodie")
    assert not await notifier.once()
    assert len(notifier.bot.calls) == (0 if both_exclude else 1)


async def test_candidate_arriving_during_exclusion_settlement_is_not_stranded(
    environment, monkeypatch
):
    store, _ = environment
    notifier = await prepare_exclusion(environment)
    settle = notifier.settle

    async def concurrent_candidate(claimed, state, **kwargs):
        assert state == "excluded"
        await observed_by(store, "Hoodie")
        await settle(claimed, state, **kwargs)

    monkeypatch.setattr(notifier, "settle", concurrent_candidate)
    assert await notifier.once("ready")
    saved = (await rows(store, Delivery))[0]
    assert saved.state == "pending" and len(saved.candidates) == 2
    monkeypatch.setattr(notifier, "settle", settle)
    assert await notifier.once("ready")
    assert (await rows(store, Delivery))[0].state == "sent"
    assert len(notifier.bot.calls) == 1 and notifier.parser.calls == 1


@pytest.mark.parametrize("revocation", ["disabled", "baseline"])
@pytest.mark.parametrize("stop_after_match", [False, True])
async def test_candidate_arriving_during_cancellation_is_not_stranded(
    environment, monkeypatch, revocation, stop_after_match
):
    store, catalog = environment
    notifier = await prepare_exclusion(environment)
    original = (await rows(store, Delivery))[0]
    search_id = original.candidates[0]["token"]["search_id"]
    if revocation == "disabled":
        await catalog.toggle_filter(101, search_id, "disable-first-match")
    else:
        async with store.sessions.begin() as session:
            job = await session.get(Job, original.candidates[0]["job_id"])
            job.baseline_ready = False
    settle = notifier.settle

    async def concurrent_candidate(claimed, state, **kwargs):
        assert state == ("cancelled" if revocation == "disabled" else "expired")
        await observed_by(store, "Hoodie")
        if stop_after_match:
            await store.control.stop(101, "stop-after-second-match")
        await settle(claimed, state, **kwargs)

    monkeypatch.setattr(notifier, "settle", concurrent_candidate)
    await notifier.once("ready")
    saved = (await rows(store, Delivery))[0]
    assert saved.state == "pending" and len(saved.candidates) == 2
    assert saved.item == original.item and saved.created_at == original.created_at
    monkeypatch.setattr(notifier, "settle", settle)
    await notifier.once("ready")
    assert (await rows(store, Delivery))[0].state == ("cancelled" if stop_after_match else "sent")
    assert len(notifier.bot.calls) == (0 if stop_after_match else 1)


async def test_late_candidate_does_not_renew_expired_publication_age(environment, monkeypatch):
    import vpars_notifier.service as service

    store, _ = environment
    notifier = await prepare_exclusion(environment)
    assert await notifier.once("ready")
    original = (await rows(store, Delivery))[0]
    await observed_by(store, "Hoodie")
    assert (await rows(store, Delivery))[0].item == original.item
    tick = utcnow() + timedelta(seconds=61)
    monkeypatch.setattr(service, "utcnow", lambda: tick)
    notifier.next_expiry_check = 0
    assert not await notifier.once("ready")  # Queue expiry handles it before claim.
    assert (await rows(store, Delivery))[0].state == "expired"
    assert not notifier.bot.calls


@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize(
    "error,retry_after",
    [
        ("GatewayTimeout", None),
        ("GatewayServiceError", None),
        ("SourceRateLimited", 150),
        ("AccessBlocked", None),
        ("UnexpectedContent", None),
        ("ParserBusy", None),
    ],
)
async def test_catalog_errors_keep_verified_delivery_lease_and_unfinished_scan(
    environment, error, retry_after, deep
):
    store, _ = environment
    await start(environment)
    parser = Parser(items=[lot("10001")])
    parser.more = deep
    await Monitor(store, parser).once(region="FR")
    before = (await rows(store, Job))[0]
    original = (await rows(store, SearchRun))[0]
    assert before.page == (2 if deep else 1)
    await due(store, Job)
    claimed = (await store.claim("FR"))[0]
    await store.fail(claimed, error, retry_after=retry_after)
    saved = (await rows(store, Job))[0]
    run = (await rows(store, SearchRun))[0]
    assert run.state == "active" and run.active_until == original.active_until
    assert run.last_error == error
    assert saved.checkpoint == before.checkpoint and saved.scan_state
    assert saved.coverage == "catchup_error"
    if error in {"SourceRateLimited", "AccessBlocked"}:
        assert saved.next_due >= utcnow() + timedelta(seconds=(retry_after or 120) - 2)

    # Repeated failures may redirect the cursor to a head probe. They must
    # neither revoke nor extend the previous successful response's lease.
    if deep and error == "GatewayTimeout":
        await due(store, Job)
        claimed = (await store.claim("FR"))[0]
        assert claimed.page == 2
        await store.fail(claimed, error)
        saved = (await rows(store, Job))[0]
        assert saved.page == 1 and saved.scan_state["resume_page"] == 2
        run = (await rows(store, SearchRun))[0]
        assert run.active_until == original.active_until and run.state == "active"

    notifier = Notifier(store, Bot())
    assert await notifier.once()
    assert len(notifier.bot.calls) == 1
    assert (await rows(store, Delivery))[0].state == "sent"
    assert (await rows(store, Job))[0].coverage == "catchup_error"


@pytest.mark.parametrize("revocation", ["stop", "subscription", "lease"])
async def test_kept_delivery_lease_still_respects_revocation(environment, revocation):
    store, _ = environment
    await start(environment)
    parser = Parser()
    parser.more = True
    await Monitor(store, parser).once(region="FR")
    await due(store, Job)
    claimed = (await store.claim("FR"))[0]
    await store.fail(claimed, "GatewayTimeout")
    if revocation == "stop":
        await store.control.stop(101, "stop-after-error")
    else:
        async with store.sessions.begin() as session:
            if revocation == "subscription":
                (await session.get(Subscription, 101)).expires_at = utcnow() - timedelta(seconds=1)
            else:
                run = await session.get(
                    SearchRun, (claimed.token["search_id"], claimed.token["source_id"])
                )
                run.active_until = utcnow() - timedelta(seconds=1)
    notifier = Notifier(store, Bot())
    assert await notifier.once()
    assert not notifier.bot.calls
    assert (await rows(store, Delivery))[0].state != "sent"


async def test_first_catalog_failure_does_not_acknowledge_search(environment):
    store, catalog = environment
    await catalog.save(101, Filters(keywords=["Hoodie"], sources={"vinted_fr": ["FR"]}), "new")
    await store.control.start(101, "start-new")
    parser = Parser()
    parser.error = "GatewayTimeout"
    await Monitor(store, parser).once(region="FR")
    run = (await rows(store, SearchRun))[0]
    assert run.state == "requested" and run.active_until is None
    assert not await rows(store, Delivery)
