import pytest
from test_monitoring import Bot, Parser, due, lot, rich_text, rows
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute, FilterCheck, Money, ParseRequest

from vpars_worker.db import Delivery, Job
from vpars_worker.matching import matches, mismatch_reason, validate_result
from vpars_worker.monitor import Monitor


def checks(values):
    return [
        FilterCheck(
            field="keywords",
            values=values,
            normalized_values=[v.casefold() for v in values],
            item_field="searchable_card",
            operation="grouped_keywords_card_casefold",
        )
    ]


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({}, True),
        ({"title": "Hoodie"}, True),
        ({"brand": Attribute(raw="Nike", normalized="nike"), "title": "Hoodie"}, False),
        ({"size": Attribute(raw="XL", normalized="xl")}, False),
        ({"size": None}, False),
        ({"title": "Stone Island hoodiest"}, False),
        ({"availability": "sold"}, False),
    ],
)
def test_every_phrase_matches_whole_words_and_brand_size_metadata(updates, expected):
    assert matches(lot(**updates), checks(["Stone Island", "hoodie", "L"])) == expected


def test_keywords_cannot_match_price_or_another_size_system():
    item = lot(
        title="Nike Air Max 90",
        brand=Attribute(raw="Nike", normalized="nike"),
        size=Attribute(raw="42", normalized="42"),
    )
    assert matches(item, checks(["Nike", "Air Max 90", "EU 42"]))
    assert not matches(item, checks(["Nike", "25"]))
    assert not matches(item.model_copy(update={"region_id": "UK"}), checks(["Nike", "EU 42"]))


def test_keyword_rejection_records_the_actual_searchable_card():
    item = lot(title="Stone Island hoodie", source_attributes={"model": "Ghost"})
    reason = mismatch_reason(item, checks(["Nike", "hoodie"]))
    assert reason["code"] == "FilterMismatch"
    assert reason["field"] == "keywords"
    assert reason["actual"]["title"] == item.title
    assert reason["actual"]["brand"] == item.brand.model_dump(mode="json")
    assert reason["actual"]["model"] == "Ghost"


@pytest.mark.parametrize(
    "title,raw,normalized,expected",
    [
        ("Jeans", "W32", "w32", True),
        ("Jeans", "W32 / L34", None, True),
        ("Jeans W32 L34", "M", "m", True),
        ("Jeans W32L34", None, None, True),
        ("Jeans W32", "W34", "w34", False),
        ("Jeans W32 or W34", "M", "m", False),
        ("Jeans", "M", "m", False),
        ("Jeans", "32", "32", False),
        ("Jeans model AW32", "M", "m", False),
    ],
)
def test_jeans_keywords_match_exact_waist(title, raw, normalized, expected):
    item = lot(title=title, size=Attribute(raw=raw, normalized=normalized) if raw else None)
    assert matches(item, checks(["Jeans", "W32"])) is expected
    assert matches(item, checks(["Jeans", "w 32"])) is expected
    assert not matches(item, checks(["Jeans", "W30"]))


async def test_waist_keyword_filter_delivers_jeans_with_w_size(environment):
    store, catalog = environment
    await catalog.save(
        101,
        Filters(
            keywords=["Gucci", "Jeans", "W32", "Very Good"],
            keywords_all=True,
            sources={"vinted_fr": ["FR"]},
            follow_enabled_sources=True,
        ),
        "waist-save",
    )
    await store.control.start(101, "waist-start")
    await Monitor(store, Parser(items=[lot("999", published_at=None)])).once(region="FR")
    await due(store, Job)
    common = {
        "brand": Attribute(raw="Gucci", normalized="gucci"),
        "condition": Attribute(raw="Very Good", normalized="very good"),
        "size": Attribute(raw="M", normalized="m"),
    }
    parser = Parser(
        items=[
            lot("10001", title="Gucci Jeans W32 L34", **common),
            lot("10002", title="Gucci Jeans W34 L32", **common),
            lot("10003", title="Gucci Jeans", **common),
        ]
    )
    await Monitor(store, parser).once(region="FR")
    assert parser.calls[0].search_text == "Gucci"
    assert [row.item["source_item_id"] for row in await rows(store, Delivery)] == ["10001"]
    bot = Bot()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1
    assert "Size: W32" in rich_text(bot.calls[0][2])


def test_model_metadata_is_searchable_but_unrelated_source_metadata_is_not():
    item = lot(title="Hoodie", source_attributes={"model": "Ghost", "price_label": "discount"})
    assert matches(item, checks(["Stone Island", "Ghost", "L"]))
    assert not matches(item, checks(["Stone Island", "discount"]))


async def test_keyword_filter_reaches_parser_and_only_complete_matches_are_sent(environment):
    store, catalog = environment
    await catalog.save(
        101,
        Filters(
            keywords=["Stone Island", "hoodie", "L"],
            keywords_all=True,
            sources={"vinted_fr": ["FR"]},
            follow_enabled_sources=True,
            native_prices={
                "EUR": {"minimum": "20", "maximum": "30"},
                "GBP": {"minimum": "10", "maximum": "15"},
            },
        ),
        "keyword-save",
    )
    await store.control.start(101, "keyword-start")
    await Monitor(store, Parser(items=[lot("999", published_at=None)])).once(region="FR")
    await due(store, Job)
    parser = Parser(
        items=[
            lot("10001"),
            lot("10002", size=Attribute(raw="XL", normalized="xl")),
            lot("10003", price=Money(amount="31", currency="EUR")),
            lot("10004", price=Money(amount="19", currency="EUR")),
        ]
    )
    await Monitor(store, parser).once(region="FR")
    assert parser.calls[0].filters.keywords_all
    assert parser.calls[0].search_text == "Stone Island"
    assert parser.calls[0].filters.price.model_dump(mode="json") == {
        "minimum": "20",
        "maximum": "30",
        "currency": "EUR",
    }
    deliveries = await rows(store, Delivery)
    assert len(deliveries) == 1 and deliveries[0].item["source_item_id"] == "10001"
    assert deliveries[0].candidates[0]["search_name"] == "Stone Island, hoodie, L"
    bot = Bot()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1


@pytest.mark.parametrize("tamper", ["operation", "values", "legacy_operation"])
async def test_parser_cannot_downgrade_new_filter_to_or(tamper):
    request = ParseRequest(
        region_id="FR", filters={"keywords": ["Nike", "L"], "keywords_all": True}
    )
    result = await Parser().parse(request)
    if tamper == "operation":
        result.residual_filters[0].operation = "any_alternative_all_words_casefold"
        result.residual_filters[0].item_field = "title"
    elif tamper == "legacy_operation":
        result.residual_filters[0].operation = "all_keywords_card_casefold"
    else:
        result.residual_filters[0].normalized_values = ["nike"]
    with pytest.raises(ValueError):
        validate_result(request, result)
