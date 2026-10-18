from datetime import timedelta

import pytest
from sqlalchemy import select
from test_keyword_matching import checks
from test_monitoring import Bot, Parser, due, lot, rows
from vpars_bot.db import User, utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute, Money

from vpars_worker.db import Delivery, Job
from vpars_worker.matching import matches, size_matches
from vpars_worker.monitor import Monitor, Store
from vpars_worker.query import source_text


@pytest.mark.parametrize("brand", ["Amiri", "Gucci", "Levi's"])
@pytest.mark.parametrize("size", ["30", "31", "32", "33", "34"])
def test_numeric_keywords_are_alternative_sizes_including_verified_waists(brand, size):
    item = lot(
        title=f"{brand} Jeans",
        brand=Attribute(raw=brand, normalized=brand.casefold()),
        size=Attribute(raw=f"IT 42 | W{size}", normalized=f"w{size}"),
    )
    assert matches(item, checks([brand, "Jeans", "32", "33", "34", "31", "30"]))
    assert size_matches(item, ["30", "31", "32", "33", "34"])
    assert not matches(item, checks([brand, "Jeans", "28", "29"]))


@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("condition", ["good", "very good"])
def test_size_and_condition_groups_accept_one_value_each(size, condition):
    item = lot(
        size=Attribute(raw=size.upper(), normalized=size),
        condition=Attribute(raw=condition, normalized=condition),
    )
    assert matches(item, checks(["Stone Island", "Hoodie", "S", "M", "L", "Good", "Very Good"]))


@pytest.mark.parametrize(
    "updates",
    [
        {"size": Attribute(raw="XL", normalized="xl")},
        {"size": None, "title": "Stone Island Hoodie S M L"},
        {"condition": Attribute(raw="Satisfactory", normalized="satisfactory")},
        {"condition": None, "title": "Stone Island Hoodie Very Good"},
        {"brand": None, "title": "Nike Hoodie"},
        {"title": "Stone Island trousers"},
        {"availability": "sold"},
    ],
)
def test_groups_do_not_weaken_other_keywords_or_accept_unverified_attributes(updates):
    item = lot(condition=Attribute(raw="Good", normalized="good")).model_copy(update=updates)
    assert not matches(item, checks(["Stone Island", "Hoodie", "S", "M", "L", "Good", "Very Good"]))


@pytest.mark.parametrize("raw", ["Bon état", "Buone", "Gut"])
def test_localized_condition_keywords_use_verified_normalized_condition(raw):
    item = lot(condition=Attribute(raw=raw, normalized="good"))
    assert matches(item, checks(["Stone Island", "Good", "Very Good"]))
    assert matches(item, checks(["Stone Island", "Bon état", "Ottime"]))
    assert not matches(item, checks(["Stone Island", "New with tags", "New without tags"]))


def test_one_size_is_an_attribute_and_explicit_size_systems_remain_distinct():
    item = lot(size=Attribute(raw="Taille unique", normalized="one size"))
    assert matches(item, checks(["Stone Island", "One size", "L"]))
    assert source_text({"keywords": ["One size", "L", "Good"], "keywords_all": True}) == ""
    item = lot(size=Attribute(raw="W32 / L34", normalized="w32"))
    assert not matches(item, checks(["Stone Island", "33", "34"]))
    assert not matches(item, checks(["Stone Island", "EU 32", "UK 32"]))
    assert not matches(item.model_copy(update={"size": None}), checks(["Stone Island", "25"]))
    assert not matches(
        item.model_copy(update={"size": Attribute(raw="32", normalized="32")}),
        checks(["Stone Island", "W32"]),
    )


@pytest.mark.parametrize("brand", ["Amiri", "Gucci"])
@pytest.mark.parametrize("admin_user", [101, 202])
async def test_existing_filters_apply_equally_to_users_after_restart(
    environment, brand, admin_user
):
    store, catalog = environment
    await catalog.register(202, 202, "second", "Second", 2, start=True)
    async with store.sessions.begin() as session:
        for user in await session.scalars(select(User)):
            user.is_admin = user.id == admin_user
    filters = Filters(
        keywords=[brand, "Jeans", "30", "32", "33", "Good", "Very Good"],
        keywords_all=True,
        excluded_keywords=["hole"],
        native_prices={"EUR": {"minimum": "20", "maximum": "40"}},
        sources={"vinted_fr": ["FR"]},
    )
    for user in (101, 202):
        await catalog.save(user, filters, f"save-{user}")
        await store.control.start(user, f"start-{user}")
    await Monitor(store, Parser(items=[lot("999")])).once()
    before = {j.id: (j.token, j.checkpoint) for j in await rows(store, Job)}
    store = Store(store.sessions, store.registry)
    await store.refresh_queries()
    assert {j.id: (j.token, j.checkpoint) for j in await rows(store, Job)} == before
    await due(store, Job)
    common = {
        "title": f"{brand} Jeans",
        "brand": Attribute(raw=brand, normalized=brand.casefold()),
        "size": Attribute(raw="FR 42 | W33", normalized="w33"),
        "condition": Attribute(raw="Bon état", normalized="good"),
        "description": "Clean",
    }
    parser = Parser(
        items=[
            lot("10001", **common),
            lot(
                "10002",
                **{
                    **common,
                    "size": Attribute(raw="W32", normalized="w32"),
                    "condition": Attribute(raw="Très bon état", normalized="very good"),
                },
            ),
            lot("10003", **common, price=Money(amount="41", currency="EUR")),
            lot("10004", **{**common, "description": "small hole"}),
            lot("10005", **common, published_at=utcnow() - timedelta(hours=1)),
            lot("10006", **{**common, "size": Attribute(raw="W36", normalized="w36")}),
        ]
    )
    monitor = Monitor(store, parser)
    await monitor.once()
    assert len(parser.calls) == 1
    assert {(d.user_id, d.item["source_item_id"]) for d in await rows(store, Delivery)} == {
        (user, identity) for user in (101, 202) for identity in ("10001", "10002")
    }
    bot = Bot()
    notifier = Notifier(store, bot)
    while await notifier.once():
        pass
    assert sorted(call[1] for call in bot.calls) == [101, 101, 202, 202]
    await due(store, Job)
    await monitor.once()
    assert not await notifier.once()  # Repeated catalog responses cannot duplicate messages.
    await store.control.stop(admin_user, "stop-admin")
    await due(store, Job)
    parser.items = [lot("10007", **common)]
    await monitor.once()
    assert await notifier.once()
    assert len(bot.calls) == 5
    assert bot.calls[-1][1] == ({101, 202} - {admin_user}).pop()
