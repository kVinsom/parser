import pytest
from test_monitoring import lot
from vpars_bot.domain import Filters, SourceRegistry, parse_sizes
from vpars_parser.models import Attribute, ParseRequest
from vpars_parser.models import Filters as ParserFilters
from vpars_parser.sources.vinted_query import attribute, build_query
from vpars_shared.sizes import normalized_size

from vpars_worker.matching import keyword_matches, matches


@pytest.mark.parametrize(
    "requested,raw,expected",
    [
        ("2XL", "XXL / 44 / 16", "xxl"),
        ("М", "M/38/10", "m"),
        ("42.5", "42,50", "42.5"),
        ("One size", "Taille unique", "one size"),
        ("ＯＳ", "Taglia unica", "one size"),
        ("One-size", "Einheitsgröße", "one size"),
        ("small", "S", "s"),
        ("W 32", "W32/L34", "w32"),
    ],
)
@pytest.mark.parametrize(
    "region,brand", [("FR", "Amiri"), ("IT", "Stone Island"), ("DE", "Nike"), ("UK", "Adidas")]
)
def test_saved_and_new_size_filters_share_catalog_and_keyword_semantics(
    requested, raw, expected, region, brand
):
    bot = Filters(sizes=[requested], sources={"vinted": [region]})
    plan = build_query(ParseRequest(region_id=region, filters=ParserFilters(sizes=bot.sizes)), 10)
    assert plan.residual[0].normalized_values == [expected]
    assert attribute("size", raw).normalized == expected
    item = lot(region_id=region, title=brand, size=Attribute(raw=raw, normalized=None))
    assert matches(item, plan.residual)
    assert keyword_matches(item, [requested])


@pytest.mark.parametrize("value", ["12 months", "3-4 years", "38/10", "EU 42/UK 8", "..", "42.5.5"])
async def test_unsupported_legacy_sizes_are_readable_but_reported_before_start(value):
    from vpars_bot.domain import Source

    registry = SourceRegistry({"vinted": Source(label="Vinted", regions=["FR"])})
    filters = Filters(sizes=[value], sources={"vinted": ["FR"]})
    assert normalized_size(value) is None
    assert "Розмір не розпізнано" in " ".join(await registry.issues(filters))


def test_comma_alternatives_and_decimal_shoe_size_are_distinct():
    assert parse_sizes("42,43") == ["42", "43"]
    assert parse_sizes("EU 42,5; XL; W32/L34") == ["EU 42.5", "XL", "W32"]
