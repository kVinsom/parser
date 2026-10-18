import pytest
from conftest import first_card, html
from selectolax.parser import HTMLParser

from vpars_parser.errors import ParserError
from vpars_parser.models import ParseRequest
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings


async def test_all_or_alternatives_preserved_for_worker(gateway):
    request = ParseRequest(
        region_id="UK",
        filters={
            "models": ["Air Max 90", "Air Force 1"],
            "keywords": ["red shoe", "blue shoe"],
            "sizes": ["M", "L"],
            "brands": ["Nike", "Adidas"],
            "conditions": ["Very good", "Good"],
            "price": {"minimum": "0.01", "maximum": "100.00", "currency": "GBP"},
        },
    )
    result = await ParserService(gateway, Settings()).parse(request)
    assert gateway.calls[0].search_text == ""  # Broad source catalog preserves OR alternatives.
    assert result.applied_filters == []
    checks = {f.field: f for f in result.residual_filters}
    for field in ("models", "keywords", "sizes", "brands", "conditions"):
        assert checks[field].values == getattr(request.filters, field)
    assert checks["models"].operation == "any_alternative_all_words_casefold"
    assert result.filter_logic == "AND_between_fields_OR_within_field"
    assert len(result.items) == 8  # Matching belongs to worker, no hidden local truncation.


@pytest.mark.parametrize("region", ["UK", "FR", "IT", "DE"])
async def test_all_keywords_have_an_explicit_card_matching_contract(gateway, region):
    result = await ParserService(gateway, Settings()).parse(
        ParseRequest(
            region_id=region,
            filters={
                "keywords": ["Nike", "hoodie", "S", "L", "Good", "Very Good"],
                "keywords_all": True,
            },
        )
    )
    assert result.filter_logic == "AND_text_OR_sizes_OR_conditions"
    (check,) = result.residual_filters
    assert check.operation == "grouped_keywords_card_casefold"
    assert check.item_field == "searchable_card"
    assert check.values == ["Nike", "hoodie", "S", "L", "Good", "Very Good"]
    assert check.normalized_values == ["nike", "hoodie", "s", "l", "good", "very good"]


async def test_explicit_search_applied_and_page_is_single_call(gateway):
    result = await ParserService(gateway, Settings()).parse(
        ParseRequest(region_id="FR", search_text="nike", page=2)
    )
    assert gateway.calls[0].search_text == "nike" and gateway.calls[0].page == 2
    assert result.applied_filters[0].values == ["nike"]
    assert result.pagination.has_more == "unknown"  # Fixture page=1 is not continuation proof.
    assert len(gateway.calls) == 1


@pytest.mark.parametrize(
    "filters,field",
    [
        ({"colors": ["Black", "Grey"]}, "colors"),
        ({"categories": ["Shoes"]}, "categories"),
        ({"conditions": ["Invented"]}, "conditions"),
        ({"price": {"maximum": "30", "currency": "UAH"}}, "price.currency"),
    ],
)
async def test_unsupported_before_any_gateway_call(gateway, filters, field):
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(
            ParseRequest(region_id="UK", filters=filters)
        )
    assert raised.value.detail.code == "UnsupportedFilter" and field in raised.value.detail.fields
    assert gateway.calls == [] and gateway.region_calls == 0


async def test_active_filter_missing_for_all_items_is_unsupported(gateway):
    tree = HTMLParser(first_card())
    tree.css_first("a").attrs["title"] = "A plain item, 4.00 £, 4.90 £"
    gateway.changes["html"] = tree.html.encode()
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(
            ParseRequest(region_id="UK", filters={"sizes": ["M"]})
        )
    assert raised.value.detail.code == "UnsupportedFilter"
    assert raised.value.detail.fields == ["sizes"]


async def test_missing_on_one_item_is_not_confirmed_match(gateway):
    tree = HTMLParser(html())
    tree.css_first("a").attrs["title"] = "A plain item, 4.00 £, 4.90 £"
    gateway.changes["html"] = tree.html.encode()
    result = await ParserService(gateway, Settings()).parse(
        ParseRequest(region_id="UK", filters={"sizes": ["M"]})
    )
    assert result.items[0].size is None
    assert any(w.item_id == "10001" and w.code == "FilterValueUnknown" for w in result.warnings)
    assert result.residual_filters[0].missing_value == "not_confirmed"


async def test_requested_composite_size_uses_same_normalization_as_items(gateway):
    result = await ParserService(gateway, Settings()).parse(
        ParseRequest(region_id="FR", filters={"sizes": ["M / 38 / 10", "L"]})
    )
    assert result.residual_filters[0].values == ["M / 38 / 10", "L"]
    assert result.residual_filters[0].normalized_values == ["m", "l"]


async def test_unmapped_size_filter_is_explicitly_unsupported(gateway):
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(
            ParseRequest(region_id="FR", filters={"sizes": ["Up to 1 month"]})
        )
    assert raised.value.detail.fields == ["sizes"] and not gateway.calls


@pytest.mark.parametrize("region", ["GB", "FR", "IT", "DE"])
async def test_qualified_sizes_are_preserved_without_conversion(gateway, region):
    result = await ParserService(gateway, Settings()).parse(
        ParseRequest(region_id=region, filters={"sizes": ["EU 42", "eur42.5", "UK 7.5"]})
    )
    sizes = result.residual_filters[0]
    assert sizes.values == ["EU 42", "eur42.5", "UK 7.5"]
    assert sizes.normalized_values == ["eu 42", "eu 42.5", "uk 7.5"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("42,5", "42.5"),
        ("2XL", "xxl"),
        ("XXL / 44", "xxl"),
        ("4XL", "xxxxl"),
        ("W32", "w32"),
        ("w 32", "w32"),
        ("W32/L34", "w32"),
        ("W32 L34", "w32"),
        ("W32L34", "w32"),
    ],
)
def test_size_notations_share_the_same_normalization(raw, expected):
    from vpars_parser.sources.vinted import attribute

    assert attribute("size", raw).normalized == expected


async def test_waist_size_filter_is_supported(gateway):
    result = await ParserService(gateway, Settings()).parse(
        ParseRequest(region_id="FR", filters={"sizes": ["W32", "w 34"]})
    )
    assert result.residual_filters[0].normalized_values == ["w32", "w34"]
