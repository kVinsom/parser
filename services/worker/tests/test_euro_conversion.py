from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rows
from vpars_bot.db import Search, SearchRun, User
from vpars_bot.domain import Filters
from vpars_bot.exchange import ExchangeRates
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute, Money

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor


@pytest.mark.parametrize("user,brand", [(101, "Nike"), (202, "Adidas")])
async def test_active_legacy_search_updates_rate_and_rechecks_pending_delivery(
    environment, user, brand
):
    store, catalog = environment
    if user != 101:
        await catalog.register(user, user, "second", "Second", 1, start=True)
    filters = Filters(
        brands=[brand],
        sources={"vinted_gb": ["GB"]},
        native_prices={"EUR": {"maximum": "100"}, "GBP": {"maximum": "1"}},
    )
    search = await catalog.save(user, filters, "save")
    await store.control.start(user, "start")
    await store.ingest()
    async with store.sessions.begin() as session:
        job = await session.scalar(select(Job))
        # Simulate a running job derived by the old native-currency implementation.
        job.query = {
            **job.query,
            "filters": {**job.query["filters"], "price": {"currency": "GBP", "maximum": "1"}},
        }
        before_filters = deepcopy((await session.get(Search, search.id)).filters)
        before_epoch = (await session.get(User, user)).delivery_epoch
        before_generation = (await session.get(SearchRun, (search.id, "vinted_gb"))).generation

    def item(identity, price):
        return lot(
            identity,
            title=brand,
            brand=Attribute(raw=brand, normalized=brand.lower()),
            region_id="UK",
            source_namespace="vinted:UK",
            price=Money(amount=price, currency="GBP"),
        )

    parser = Parser(items=[item("baseline", "50")])
    monitor = Monitor(store, parser)
    await monitor.once()
    assert parser.calls[-1].filters.price.maximum == 85
    await due(store, Job)
    parser.items = [item("10001", "84"), item("10002", "86")]
    await monitor.once()
    deliveries = await rows(store, Delivery)
    assert [row.item["source_item_id"] for row in deliveries] == ["10001"]
    notifier = Notifier(store, Bot())
    assert (await notifier.decide(deliveries[0]))[0] == "ready"

    store.registry.exchange = ExchangeRates()
    assert (await notifier.decide(deliveries[0]))[0] == "waiting"
    await due(store, Job)
    assert await store.claim() == []
    assert (await rows(store, Job))[0].last_error == "ExchangeRateUnavailable"

    store.registry.exchange.load(
        {"date": datetime.now(UTC).date().isoformat(), "rates": {"GBP": "0.8"}}
    )
    assert await notifier.decide(deliveries[0]) == ("expired", "PriceOutsideRange")
    await due(store, Job)
    parser.items = [item("10003", "79"), item("10004", "81")]
    await monitor.once()
    assert parser.calls[-1].filters.price.maximum == 80
    assert {row.item["source_item_id"] for row in await rows(store, Delivery)} == {"10001", "10003"}
    async with store.sessions() as session:
        assert (await session.get(Search, search.id)).filters == before_filters
        assert (await session.get(User, user)).delivery_epoch == before_epoch
        run = await session.get(SearchRun, (search.id, "vinted_gb"))
        assert run.generation == before_generation and run.last_error is None
