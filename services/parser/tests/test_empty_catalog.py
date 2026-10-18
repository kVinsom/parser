from pathlib import Path

import pytest
from conftest import first_card

from vpars_parser.errors import ParserError
from vpars_parser.models import Filters, ParseRequest
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings

EMPTY = (Path(__file__).parent / "fixtures/FR_empty.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("page", [1, 2, 11])
async def test_explicit_empty_catalog_is_complete_without_inventing_cards(gateway, page):
    gateway.changes["html"] = EMPTY.encode()
    result = await ParserService(gateway, Settings()).parse(
        ParseRequest(region_id="FR", page=page, filters=Filters(brands=["Stone Island"]))
    )
    assert result.items == result.observed_item_ids == []
    assert result.cards_seen == result.cards_skipped == 0
    assert result.completeness == "complete" and result.identities_complete
    assert result.empty_confirmed and result.model_dump(mode="json")["empty_confirmed"]
    assert result.pagination.has_more == "false" and result.pagination.next_page is None
    assert result.residual_filters[0].field == "brands"


@pytest.mark.parametrize(
    "body",
    [
        "<title>Vinted</title><div>No results</div>",
        EMPTY.replace('data-testid="search-empty-state--body"', 'data-testid="loading"'),
        EMPTY.replace("Désolé, aucun résultat", " "),
        EMPTY + first_card("FR"),
        EMPTY + '<a href="/items/123-unrecognized">Item</a>',
        EMPTY
        + '<a data-testid="catalog-pagination--next-page" aria-disabled="false" '
        'href="?page=3">Next</a>',
    ],
)
async def test_incomplete_or_conflicting_empty_state_is_not_a_terminal_page(gateway, body):
    gateway.changes["html"] = body.encode()
    with pytest.raises(ParserError) as error:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id="FR", page=2))
    assert error.value.detail.code == "SchemaChanged"
