"""Mixed saved filters must keep independent matches across a shared catalog."""

from datetime import timedelta

import pytest
from test_delivery_recovery import choose_query
from test_monitoring import Bot, Parser, lot, rows
from vpars_bot.db import Search, utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute, Money

from vpars_worker.db import Delivery, Job, SeenItem, SentLot
from vpars_worker.monitor import Monitor, Store


@pytest.mark.parametrize("brands", [("Stone Island", "Amiri"), ("Nike", "Adidas")])
@pytest.mark.parametrize("region", ["FR", "GB"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("restart", [False, True])
async def test_mixed_prices_sizes_exclusions_and_users_keep_independent_matches(
    environment, brands, region, reverse, restart
):
    store, catalog = environment
    await catalog.register(202, 202, "second", "Second", 2, start=True)
    source = "vinted_" + region.lower()
    currency = "GBP" if region == "GB" else "EUR"
    item_region = "UK" if region == "GB" else region
    base_price = 10 if currency == "GBP" else 20
    narrow = Filters(
        brands=[brands[0]],
        models=["Hoodie"],
        sizes=["L", "XL"],
        excluded_keywords=["hole"],
        native_prices={currency: {"minimum": str(base_price), "maximum": str(base_price + 20)}},
        sources={source: [region]},
    )
    broad = Filters(
        brands=[brands[0]],
        native_prices={currency: {"maximum": str(base_price + 10)}},
        sources={source: [region]},
    )
    other = Filters(brands=[brands[1]], sizes=["M"], sources={source: [region]})
    specifications = [
        (101, "narrow", narrow),
        (101, "broad", broad),
        (202, "narrow", narrow),
        (202, "other", other),
    ]
    searches = {}
    for user, name, filters in reversed(specifications) if reverse else specifications:
        searches[user, name] = await catalog.save(user, filters, name)
    for user in (101, 202):
        await store.control.start(user, "start")
    await store.ingest()

    def item(identity, brand=brands[0], **updates):
        return lot(
            identity,
            **{
                "title": f"{brand} Hoodie",
                "brand": Attribute(raw=brand, normalized=brand.casefold()),
                "size": Attribute(raw="L", normalized="l"),
                "description": "Clean",
                "region_id": item_region,
                "source_namespace": f"vinted:{item_region}",
                "price": Money(amount=str(base_price), currency=currency),
                **updates,
            },
        )

    parser = Parser(items=[item("999")])
    monitor = Monitor(store, parser)
    for brand in brands:
        await choose_query(store, brand)
        assert await monitor.once(region)
    original = {job.id: (job.token, job.checkpoint) for job in await rows(store, Job)}
    saved_filters = {search.id: search.filters for search in await rows(store, Search)}
    if restart:
        store = Store(store.sessions, store.registry)
        await store.refresh_queries()
        assert {job.id: (job.token, job.checkpoint) for job in await rows(store, Job)} == original
        monitor = Monitor(store, parser)

    parser.calls.clear()
    parser.items = [
        item("10001"),  # Both overlapping searches.
        item("10002", size=None),  # Unknown size is allowed only by the broad search.
        item("10003", price=Money(amount=str(base_price + 15), currency=currency)),
        item("10004", description="small hole"),  # Exclusion belongs only to narrow.
        item(
            "10005",
            title=f"{brands[0]} Trousers",
            price=Money(amount=str(base_price + 15), currency=currency),
        ),
        item("10006", brands[1], size=Attribute(raw="M", normalized="m")),
        item("10007", availability="sold"),
        item("10008", published_at=utcnow() - timedelta(hours=1)),
        item("10009", price=Money(amount=str(base_price), currency="USD")),
    ]
    for brand in brands:
        await choose_query(store, brand)
        assert await monitor.once(region)
    assert len(parser.calls) == 2  # All three first-brand filters share one request.
    delivered_by_search = {
        (delivery.user_id, delivery.item["source_item_id"]): {
            candidate["token"]["search_id"] for candidate in delivery.candidates
        }
        for delivery in await rows(store, Delivery)
    }
    narrow_id, broad_id = searches[101, "narrow"].id, searches[101, "broad"].id
    assert delivered_by_search == {
        (101, "10001"): {narrow_id, broad_id},
        (101, "10002"): {broad_id},
        (101, "10003"): {narrow_id},
        (101, "10004"): {broad_id},
        (202, "10001"): {searches[202, "narrow"].id},
        (202, "10003"): {searches[202, "narrow"].id},
        (202, "10006"): {searches[202, "other"].id},
    }

    # Disabling one overlapping match cannot cancel the other match or another user.
    assert not await catalog.toggle_filter(101, narrow_id, "disable-narrow")
    notifier = Notifier(store, Bot())
    for _ in range(8):
        await notifier.once(fresh_first=True)
    expected = {(101, identity) for identity in ("10001", "10002", "10004")} | {
        (202, identity) for identity in ("10001", "10003", "10006")
    }
    assert {(sent.user_id, sent.source_item_id) for sent in await rows(store, SentLot)} == expected
    assert len(notifier.bot.calls) == len(expected)
    assert {search.id: search.filters for search in await rows(store, Search)} == saved_filters

    # Re-observing the same listings must not duplicate delivery through another filter.
    for brand in brands:
        await choose_query(store, brand)
        await monitor.once(region)
    assert not await notifier.once(fresh_first=True)
    assert len(notifier.bot.calls) == len(expected)


@pytest.mark.parametrize("brand", ["Amiri", "Nike"])
@pytest.mark.parametrize("user", [101, 202])
async def test_required_word_also_excluded_cannot_cancel_another_matching_filter(
    environment, brand, user
):
    store, catalog = environment
    if user != 101:
        await catalog.register(user, user, "second", "Second", 2, start=True)
    conflict = await catalog.save(
        user,
        Filters(
            keywords=[brand, "Vintage"],
            keywords_all=True,
            excluded_keywords=["Vintage"],
            sources={"vinted_fr": ["FR"]},
        ),
        "conflicting",
    )
    allowed = await catalog.save(
        user, Filters(brands=[brand], sources={"vinted_fr": ["FR"]}), "allowed"
    )
    await store.control.start(user, "start")
    parser = Parser(items=[lot("999")])
    monitor = Monitor(store, parser)
    assert await monitor.once("FR")
    await choose_query(store, brand)
    parser.items = [
        lot(
            title=f"{brand} Vintage",
            description="Clean",
            brand=Attribute(raw=brand, normalized=brand.casefold()),
        )
    ]
    assert await monitor.once("FR")
    delivery = (await rows(store, Delivery))[0]
    assert {candidate["token"]["search_id"] for candidate in delivery.candidates} == {allowed.id}
    jobs = {job.id: job.token["search_id"] for job in await rows(store, Job)}
    rejected = next(
        seen
        for seen in await rows(store, SeenItem)
        if seen.item_id == "10001" and jobs[seen.job_id] == conflict.id
    )
    assert rejected.decision["code"] == "ExcludedKeyword"
    notifier = Notifier(store, Bot())
    assert await notifier.once()
    assert len(notifier.bot.calls) == 1 and notifier.bot.calls[0][1] == user
    assert (await catalog.get(user, conflict.id)).filters == conflict.filters
