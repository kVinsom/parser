from decimal import ROUND_HALF_UP, Decimal

import pytest
from test_monitoring import Parser, due, lot, rows
from vpars_bot.domain import Filters
from vpars_bot.keyword_dialog import build_filters, input_value, new_filter_draft
from vpars_parser.models import Attribute, Money

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor


@pytest.mark.parametrize(
    "region,currency", [("FR", "EUR"), ("DE", "EUR"), ("IT", "EUR"), ("GB", "GBP")]
)
@pytest.mark.parametrize("brand", ["Amiri", "Nike"])
async def test_1000_is_preserved_and_high_prices_match_without_changing_other_users(
    environment, region, currency, brand
):
    store, catalog = environment
    draft = new_filter_draft(store.registry)
    for number, value in enumerate((brand, "", "10 1000")):
        draft = input_value(draft, value, number, skip=value == "")
    saved = build_filters(draft, store.registry)
    source = "vinted_" + region.lower()
    wide = Filters.model_validate(
        {**saved.model_dump(), "sources": {source: [region]}, "follow_enabled_sources": False}
    )
    narrow = Filters.model_validate(
        {
            **wide.model_dump(),
            "price": None,
            "native_prices": {currency: {"minimum": "10", "maximum": "100"}},
        }
    )
    await catalog.register(202, 202, "second", "Second", 1, start=True)
    for user, filters in ((101, wide), (202, narrow)):
        stored = await catalog.save(user, filters, "save")
        if user == 101:
            assert stored.filters["price"]["maximum"] == "1000"
        else:
            assert stored.filters["native_prices"][currency]["maximum"] == "100"
        await store.control.start(user, "start")
    normalized = "UK" if region == "GB" else region

    def converted(amount):
        rate = Decimal("0.85") if currency == "GBP" else Decimal(1)
        return (Decimal(amount) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def item(identity, amount):
        return lot(
            identity,
            title=brand,
            brand=Attribute(raw=brand, normalized=brand.lower()),
            region_id=normalized,
            source_namespace="vinted:" + normalized,
            price=Money(amount=converted(amount), currency=currency),
        )

    parser = Parser(items=[item("baseline", "500")])
    monitor = Monitor(store, parser)
    await monitor.once()
    await due(store, Job)
    prices = ["5", "100", "500", "999.99", "1000", "1000.01"]
    parser.items = [item(str(i), price) for i, price in enumerate(prices, 10001)]
    await monitor.once()
    deliveries = await rows(store, Delivery)
    assert {Decimal(d.item["price"]["amount"]) for d in deliveries if d.user_id == 101} == {
        converted(value) for value in ("100", "500", "999.99", "1000")
    }
    assert {Decimal(d.item["price"]["amount"]) for d in deliveries if d.user_id == 202} == {
        converted("100")
    }
    assert len(parser.calls) == 2  # One shared catalog per poll, prices remain personal.
