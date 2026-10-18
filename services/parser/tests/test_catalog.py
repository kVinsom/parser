import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from conftest import FIXTURES, first_card, html
from selectolax.parser import HTMLParser

from vpars_parser.errors import ParserError
from vpars_parser.models import ParseRequest
from vpars_parser.ports import GatewayQuery
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings
from vpars_parser.sources.vinted import VintedAdapter, parse_money


@pytest.mark.parametrize(
    "region,price,currency,brand",
    [
        ("UK", "4.00", "GBP", "Thomas & Co"),
        ("FR", "3.00", "EUR", "sans marque"),
        ("IT", "5.00", "EUR", "POP IT"),
        ("DE", "7.00", "EUR", "molis&co"),
    ],
)
async def test_real_redacted_catalogs(gateway, region, price, currency, brand):
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id=region))
    count = next(
        r["cards"]
        for r in json.loads((FIXTURES / "manifest.json").read_text())
        if r["region"] == region
    )
    assert len(result.items) == count
    assert result.completeness == "complete"
    item = result.items[0]
    assert item.title and item.source_item_id == "10001"
    assert item.source_namespace == f"vinted:{region}"
    assert item.price.amount == Decimal(price)
    assert item.price.currency == currency
    assert item.brand.raw == brand
    assert item.photo_urls == ["https://images1.vinted.net/t/redacted/10001.webp"]
    assert item.colors is None and item.category is None and item.availability == "unknown"
    assert result.pagination.has_more == "true" and result.pagination.next_page == 2
    assert json.loads(result.model_dump_json())["items"][0]["price"]["amount"] == price
    assert len(gateway.calls) == 1
    assert result.timings.parse_ms >= 0 and result.timings.normalization_ms >= 0


async def test_gb_alias_and_identity(gateway):
    service = ParserService(gateway, Settings())
    uk = await service.parse(ParseRequest(region_id=" gb "))
    fr = await service.parse(ParseRequest(region_id="FR"))
    assert uk.region_id == "UK" and gateway.calls[0].region == "UK"
    assert uk.items[0].source_item_id == fr.items[0].source_item_id
    assert uk.items[0].source_namespace != fr.items[0].source_namespace


async def test_identity_inventory_preserves_catalog_order_for_checkpoint_boundary(gateway):
    card = first_card()
    gateway.changes["html"] = (
        card.replace("10001", "90000")
        + card.replace("10001", "20")
        + card.replace("10001", "30000")
        + card.replace("10001", "20")
    ).encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.identities_ordered and result.identities_complete
    assert result.observed_item_ids == ["90000", "20", "30000"]


async def test_settled_promoted_wardrobes_do_not_contaminate_search_inventory(gateway):
    gateway.changes["html"] = (
        first_card() + '<div data-testid="closet-item-80000">'
        '<div data-testid="item-80000"><a href="/items/80000">Promoted seller item</a>'
        "</div></div>"
    ).encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.completeness == "complete" and result.cards_skipped == 0
    assert result.observed_item_ids == ["10001"]
    assert "SupplementaryItemsIgnored" in {warning.code for warning in result.warnings}


async def test_unknown_outside_link_still_blocks_checkpoint(gateway):
    gateway.changes["html"] = (first_card() + '<a href="/items/80000">Unknown layout</a>').encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.completeness == "partial" and result.cards_skipped == 1
    assert "80000" in result.observed_item_ids


@pytest.mark.parametrize(
    "raw,region,amount",
    [
        ("£1,234.56", "UK", "1234.56"),
        ("1 234,56 €", "FR", "1234.56"),
        ("1.234,56 €", "DE", "1234.56"),
        ("0,01 €", "IT", "0.01"),
    ],
)
def test_decimal_prices(raw, region, amount):
    assert parse_money(raw, region).amount == Decimal(amount)


@pytest.mark.parametrize("raw", ["12", "$12.00", "£NaN", "£-1.00", "£1,23.00", "£12.00 £13.00"])
def test_ambiguous_price_is_not_guessed(raw):
    with pytest.raises(ValueError):
        parse_money(raw, "UK")


async def test_missing_optional_fields_are_unknown(gateway):
    tree = HTMLParser(first_card())
    link = tree.css_first("a")
    link.attrs["title"] = "Plain item, 4.00 £, 4.90 £"
    tree.css_first("img").decompose()
    gateway.changes["html"] = tree.html.encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.completeness == "complete"
    item = result.items[0]
    assert item.brand is item.condition is item.size is None
    assert item.photo_urls == []


@pytest.mark.parametrize(
    "title,raw,expected",
    [
        ("Gucci Jeans", "W32", "w32"),
        ("Gucci Jeans", "w 32 / l 34", "w32"),
        ("Gucci Jeans W32 L34", "M / 38 / 10", "w32"),
        ("Gucci Jeans W32L34", None, "w32"),
        ("Gucci Jeans W34", "W32", "w32"),
        ("Gucci Jeans W32 or W34", "M", "m"),
        ("Gucci Jeans", "M", "m"),
        ("Gucci Jeans", "32", "32"),
        ("Gucci Jeans model AW32", "M", "m"),
        ("Gucci Jeans W32/34", "M", "m"),
    ],
)
async def test_jeans_keep_explicit_waist_size_without_guessing(gateway, title, raw, expected):
    tree = HTMLParser(first_card())
    size_label = f", Size: {raw}" if raw else ""
    tree.css_first("a").attrs["title"] = f"{title}{size_label}, 4.00 £, 4.90 £"
    gateway.changes["html"] = tree.html.encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.items[0].size.normalized == expected
    if raw:
        assert result.items[0].size.raw == raw


@pytest.mark.parametrize("selector", ["a", '[data-testid$="--price-text"]'])
async def test_damaged_required_fields_partial(gateway, selector):
    tree = HTMLParser(html())
    tree.css_first(selector).decompose()
    gateway.changes["html"] = tree.html.encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.completeness == "partial" and result.cards_skipped == 1
    assert len(result.items) == 7


@pytest.mark.parametrize(
    "body",
    [
        "<html>captcha</html>",
        "<html>No items</html>",
        '<a href="/items/10001-x">Only diagnostic link</a>',
        first_card().replace("£4.00", "$4.00"),
    ],
)
async def test_unknown_empty_and_all_invalid_are_errors(gateway, body):
    gateway.changes["html"] = body.encode()
    with pytest.raises(ParserError, match="empty catalog") as raised:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert raised.value.detail.code == "SchemaChanged"


async def test_duplicates_merge_once_per_response_and_repeat_has_no_personal_state(gateway):
    gateway.changes["html"] = (first_card() * 2).encode()
    service = ParserService(gateway, Settings())
    first = await service.parse(ParseRequest(region_id="UK"))
    second = await service.parse(ParseRequest(region_id="UK"))
    assert len(first.items) == len(second.items) == 1
    assert first.cards_seen == 2 and first.cards_skipped == 0
    assert first.completeness == "complete"


async def test_duplicate_conflict_is_partial(gateway):
    gateway.changes["html"] = (
        first_card() + first_card().replace("Stripe sun hat", "Another item")
    ).encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert len(result.items) == 1 and result.completeness == "partial"
    assert any(w.code == "DuplicateConflict" for w in result.warnings)


async def test_unrecognized_card_prevents_false_complete(gateway):
    gateway.changes["html"] = (
        first_card() + '<a href="/items/9999-changed">Changed card</a>'
    ).encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.completeness == "partial" and result.cards_skipped == 1


@pytest.mark.parametrize(
    "next_link",
    [
        "",
        '<a data-testid="catalog-pagination--next-page" aria-disabled="true" '
        'href="/catalog?page=2"></a>',
        '<a data-testid="catalog-pagination--next-page" aria-disabled="false" '
        'href="https://evil.example/catalog?page=2"></a>',
        '<a data-testid="catalog-pagination--next-page" aria-disabled="false" '
        'href="/catalog?page=1"></a>',
        '<a data-testid="catalog-pagination--next-page" aria-disabled="false" '
        'href="https://[broken"></a>',
    ],
)
async def test_pagination_requires_proof(gateway, next_link):
    gateway.changes["html"] = (first_card() + next_link).encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.pagination.has_more == "unknown" and result.pagination.next_page is None
    assert len(gateway.calls) == 1


def test_localized_condition_and_size_mapping():
    adapter = VintedAdapter()
    fr = adapter.parse_page(html("FR"), GatewayQuery("FR", "", 1, 60, "test"))
    item = adapter.normalize_item(fr.records[3], "FR", datetime.now(UTC))
    assert item.size.raw == "M / 38 / 10" and item.size.normalized == "m"
    assert item.condition.raw == "Très bon état" and item.condition.normalized == "very good"
    de = adapter.parse_page(html("DE"), GatewayQuery("DE", "", 1, 60, "test"))
    neu = [adapter.normalize_item(r, "DE", datetime.now(UTC)) for r in de.records]
    assert any(i.condition.raw == "Neu" and i.condition.normalized is None for i in neu)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/items/10001-x",
        "https://www.vinted.fr/items/10001-x",
        "/items/999-wrong",
    ],
)
async def test_item_url_identity_is_validated(gateway, url):
    tree = HTMLParser(first_card())
    tree.css_first("a").attrs["href"] = url
    gateway.changes["html"] = tree.html.encode()
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert raised.value.detail.code == "SchemaChanged"


async def test_three_conflicting_duplicates_do_not_restore_unknown_attribute(gateway):
    card = first_card()
    gateway.changes["html"] = (card + card.replace("Size: One size", "Size: M") + card).encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert result.items[0].size is None and result.completeness == "partial"
