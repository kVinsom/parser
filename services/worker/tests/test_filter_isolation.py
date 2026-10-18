"""Independent selection and any-matching-filter delivery across users and brands."""

import pytest
from test_delivery_recovery import choose_query
from test_monitoring import Bot, Parser, due, lot, rows
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute

from vpars_worker.db import Delivery, Job, SentLot
from vpars_worker.monitor import Monitor, Store
from vpars_worker.parser import ParseFailure


async def overlapping_filters(environment, user, brand, *, narrow_first=True):
    store, catalog = environment
    if user != 101:
        await catalog.register(user, user, "second", "Second", 1, start=True)
    filters = []
    for key, exclusions in (("narrow", ["hole"]), ("wide", [])):
        filters.append(
            await catalog.save(
                user,
                Filters(
                    brands=[brand], excluded_keywords=exclusions, sources={"vinted_fr": ["FR"]}
                ),
                key,
            )
        )
    await store.control.start(user, "start-both")
    parser = Parser(
        items=[lot("999", title=brand, brand=Attribute(raw=brand, normalized=brand.casefold()))]
    )
    monitor = Monitor(store, parser)
    await monitor.once(region="FR")
    await due(store, Job)
    parser.items = [
        lot(title=brand, brand=Attribute(raw=brand, normalized=brand.casefold()), description=None)
    ]
    await monitor.once(region="FR")
    row = (await rows(store, Delivery))[0]
    async with store.sessions.begin() as session:
        saved = await session.get(Delivery, row.id)
        saved.candidates = sorted(
            saved.candidates,
            key=lambda c: c["token"]["search_id"] == filters[0].id,
            reverse=narrow_first,
        )
    return filters


@pytest.mark.parametrize("user,brand", [(101, "Amiri"), (202, "Stone Island")])
@pytest.mark.parametrize("narrow_first", [False, True])
async def test_optional_exclusion_filter_cannot_delay_an_unrestricted_match(
    environment, user, brand, narrow_first
):
    store, _ = environment
    await overlapping_filters(environment, user, brand, narrow_first=narrow_first)

    class LimitedDetails:
        async def details(self, item):
            raise ParseFailure("SourceRateLimited", retry_after=120)

    notifier = Notifier(store, Bot(), parser=LimitedDetails())
    assert await notifier.once("prepare")
    row = (await rows(store, Delivery))[0]
    assert row.media_progress.get("dispatch_ready") is True
    assert await notifier.once("ready")
    assert len(notifier.bot.calls) == 1
    assert notifier.bot.calls[0][1] == user
    assert (await rows(store, Delivery))[0].state == "sent"


@pytest.mark.parametrize("user,brand", [(101, "Amiri"), (202, "Stone Island")])
async def test_disabling_unrestricted_filter_still_requires_other_filters_exclusions(
    environment, user, brand
):
    store, catalog = environment
    _, wide = await overlapping_filters(environment, user, brand)
    await catalog.toggle_filter(user, wide.id, "disable-wide")
    notifier = Notifier(store, Bot())
    _, recipient = await notifier.decide((await rows(store, Delivery))[0])
    assert recipient[1]["needs_description"] is True
    await notifier.once()
    assert not notifier.bot.calls


@pytest.mark.parametrize("user,brand", [(101, "Amiri"), (202, "Stone Island")])
async def test_toggle_during_message_preparation_rechecks_new_description_requirement(
    environment, monkeypatch, user, brand
):
    import vpars_notifier.service as service

    store, catalog = environment
    _, wide = await overlapping_filters(environment, user, brand, narrow_first=False)
    prepare = service.prepare_lot

    async def disable_after_selection(item):
        await catalog.toggle_filter(user, wide.id, "disable-during-formatting")
        return await prepare(item)

    monkeypatch.setattr(service, "prepare_lot", disable_after_selection)
    notifier = Notifier(store, Bot())
    assert await notifier.once()
    assert not notifier.bot.calls
    row = (await rows(store, Delivery))[0]
    assert row.state == "pending" and not row.media_progress["dispatch_ready"]


async def test_toggle_and_restart_one_brand_preserves_other_brand_and_other_user(environment):
    store, catalog = environment
    await catalog.register(202, 202, "second", "Second", 1, start=True)
    searches = {}
    for user, brand in ((101, "Amiri"), (101, "Stone Island"), (202, "Amiri")):
        searches[user, brand] = await catalog.save(
            user,
            Filters(brands=[brand], sources={"vinted_fr": ["FR"]}),
            brand,
        )
    await store.control.start(101, "start")
    await store.control.start(202, "start")
    parser = Parser(items=[lot("999")])
    monitor = Monitor(store, parser)
    await monitor.once("FR")
    await monitor.once("FR")
    await due(store, Job)
    parser.items = [
        lot("10001", title="Amiri Jeans", brand=Attribute(raw="Amiri", normalized="amiri")),
        lot("10002", title="Stone Island Hoodie"),
    ]
    await monitor.once("FR")
    await monitor.once("FR")
    original = {j.id: j.token for j in await rows(store, Job) if j.query["search_text"] == "Amiri"}
    stone = searches[101, "Stone Island"]
    assert not await catalog.toggle_filter(101, stone.id, "disable-stone")
    notifier = Notifier(store, Bot())
    for _ in range(3):
        await notifier.once()
    sent = await rows(store, SentLot)
    assert {(s.user_id, s.source_item_id) for s in sent} == {(101, "10001"), (202, "10001")}
    assert await catalog.toggle_filter(101, stone.id, "enable-stone")
    await store.control.start(101, "start-stone")
    await store.ingest()
    current = {j.id: j.token for j in await rows(store, Job) if j.query["search_text"] == "Amiri"}
    assert current == original
    for user in (101, 202):
        assert (await store.control.status(user))[searches[user, "Amiri"].id] == "active"


@pytest.mark.parametrize("during_failure", [False, True])
async def test_new_unrestricted_match_wakes_a_delivery_waiting_for_other_filters_details(
    environment, monkeypatch, during_failure
):
    store, catalog = environment
    for keyword, exclusions in (("Stone Island", ["hole"]), ("Hoodie", [])):
        await catalog.save(
            101,
            Filters(
                keywords=[keyword], excluded_keywords=exclusions, sources={"vinted_fr": ["FR"]}
            ),
            keyword,
        )
    await store.control.start(101, "start")
    await store.ingest()
    for keyword in ("Stone Island", "Hoodie"):
        await choose_query(store, keyword)
        await Monitor(store, Parser(items=[lot("999")])).once("FR")
    item = lot(description=None)
    await choose_query(store, "Stone Island")
    await Monitor(store, Parser(items=[item])).once("FR")

    class LimitedDetails:
        async def details(self, item):
            raise ParseFailure("SourceRateLimited", retry_after=120)

    notifier = Notifier(store, Bot(), parser=LimitedDetails())
    settle = notifier.settle

    async def arriving_during_failure(claimed, state, **kwargs):
        await choose_query(store, "Hoodie")
        await Monitor(store, Parser(items=[item])).once("FR")
        await settle(claimed, state, **kwargs)

    if during_failure:
        monkeypatch.setattr(notifier, "settle", arriving_during_failure)
    await notifier.once("prepare")
    monkeypatch.setattr(notifier, "settle", settle)
    original = (await rows(store, Delivery))[0]
    assert original.state == "pending" and original.last_error == "SourceRateLimited"
    if not during_failure:
        await choose_query(store, "Hoodie")
        await Monitor(store, Parser(items=[item])).once("FR")
    assert await notifier.once("prepare")
    assert await notifier.once("ready")
    saved = (await rows(store, Delivery))[0]
    assert saved.state == "sent" and len(notifier.bot.calls) == 1
    assert saved.created_at == original.created_at
    assert saved.item["first_seen_at"] == original.item["first_seen_at"]


@pytest.mark.parametrize("brands", [("Stone Island", "Amiri"), ("Nike", "Adidas")])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("user", [101, 202])
@pytest.mark.parametrize("restart", [False, True])
async def test_selecting_then_starting_another_filter_delivers_both_for_both_users(
    environment, brands, reverse, user, restart
):
    """Exercise sequential selection with the saved keyword/size/exclusion shapes."""
    store, catalog = environment
    await catalog.register(202, 202, "second", "Second", 1, start=True)
    broad, jeans = brands
    filters = (
        Filters(
            keywords=[broad, "Good", "Very Good"],
            keywords_all=True,
            excluded_keywords=["Fake", "Hole"],
            sources={"vinted_fr": ["FR"]},
        ),
        Filters(
            keywords=[jeans, "Jeans", "32", "33", "34", "31", "30"],
            keywords_all=True,
            sources={"vinted_fr": ["FR"]},
        ),
    )
    searches = {}
    for recipient in (101, 202):
        for brand, values in zip(brands, filters, strict=True):
            searches[recipient, brand] = await catalog.save(recipient, values, brand)
    first, second = brands[::-1] if reverse else brands
    later = searches[user, second]
    await catalog.toggle_filter(user, later.id, "disable-later")
    for recipient in (101, 202):
        await store.control.start(recipient, "start-initial")
    parser = Parser(items=[lot("999")])
    monitor = Monitor(store, parser)
    assert await monitor.once("FR")
    assert await monitor.once("FR")
    originals = {j.id: (j.token, j.checkpoint) for j in await rows(store, Job)}
    assert len(originals) == 3

    assert await catalog.toggle_filter(user, later.id, "enable-later")
    # Selection and Start are separate actions; preserve that existing contract.
    await store.ingest()
    assert len(await rows(store, Job)) == 3
    await store.control.start(user, "start-with-later")
    assert await monitor.once("FR")  # Establish only the added filter's baseline.
    assert parser.calls[-1].search_text == second
    current = {j.id: (j.token, j.checkpoint) for j in await rows(store, Job)}
    assert len(current) == 4
    assert {key: current[key] for key in originals} == originals
    assert not await rows(store, Delivery)
    if restart:
        store = Store(store.sessions, store.registry)
        await store.refresh_queries()
        monitor = Monitor(store, parser)
    await due(store, Job)
    parser.calls.clear()
    parser.items = [
        lot(
            identity,
            title=f"{brand} {kind}",
            brand=Attribute(raw=brand, normalized=brand.casefold()),
            size=Attribute(raw=size, normalized=size.casefold()),
            condition=Attribute(raw="Good", normalized="good"),
            description="Clean",
        )
        for identity, brand, kind, size in (
            ("20001", broad, "Hoodie", "L"),
            ("20002", jeans, "Jeans", "W32"),
        )
    ]
    assert await monitor.once("FR")
    assert await monitor.once("FR")
    assert {call.search_text for call in parser.calls} == set(brands)
    notifier = Notifier(store, Bot())
    for _ in range(4):
        assert await notifier.once()
    assert {(s.user_id, s.source_item_id) for s in await rows(store, SentLot)} == {
        (recipient, identity) for recipient in (101, 202) for identity in ("20001", "20002")
    }
    assert len(notifier.bot.calls) == 4
    for recipient in (101, 202):
        assert set((await store.control.status(recipient)).values()) == {"active"}
        for brand, values in zip(brands, filters, strict=True):
            saved = await catalog.get(recipient, searches[recipient, brand].id)
            assert saved.filters == values.model_dump(mode="json")
