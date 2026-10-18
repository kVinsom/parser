import asyncio
import json
from datetime import UTC, datetime

import pytest

from vpars_parser.errors import ParserError
from vpars_parser.models import ItemDetailsRequest, ParseRequest
from vpars_parser.ports import GatewayPage
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings
from vpars_parser.sources.vinted_details import parse_details, validate_item_page


def fixture(*, count=5, stamp=None, malformed=False):
    # Sanitized shape observed on public FR item 10001853847 on 2026-09-24.
    # Identity and images are replaced, React Flight field structure is retained.
    item = {
        "item_id": "10001",
        "photos": [
            {
                "id": str(i),
                "url": f"https://images1.vinted.net/{i}.webp",
                "thumbnails": [{"url": "https://images1.vinted.net/thumbnail.webp"}],
            }
            for i in range(count)
        ],
    }
    if stamp:
        item["created_at"] = stamp
    if malformed:
        item["photos"][1] = {"id": "1"}
    data = {
        "data": item,
        "recommendations": {
            "item_id": "99999",
            "photos": [{"url": "https://images1.vinted.net/wrong.webp"}],
        },
    }
    line = "1:" + json.dumps(data) + "\n"
    parts = [line[:17], line[17:]]
    return "".join(
        "<script>self.__next_f.push(" + json.dumps([1, part]) + ");</script>" for part in parts
    ) + (
        '<div data-testid="item-attributes-upload_date">'
        '<div itemprop="upload_date">Il y a 5 minutes</div></div>'
    )


def request():
    return ItemDetailsRequest(region_id="FR", source_item_id="10001")


def page(html=None, **kwargs):
    result = dict(
        region="FR",
        url="https://www.vinted.fr/items/10001",
        final_url="https://www.vinted.fr/items/10001-shoes",
        outcome="PAGE_LOADED",
        html=(html or fixture()).encode(),
        upstream_status=200,
    )
    return GatewayPage(**(result | kwargs))


def test_gallery_split_scripts_identity_and_no_invented_timestamp():
    result = parse_details(fixture(), request(), datetime.now(UTC))
    assert result.photo_urls == [f"https://images1.vinted.net/{i}.webp" for i in range(5)]
    assert result.photos_complete
    assert result.published_at is None
    assert result.publication_evidence == "relative:Il y a 5 minutes"


def test_live_italian_uploaded_markup_has_no_invented_exact_date():
    # Observed anonymously on item 10209349866 on 2026-10-02.
    html = (
        '<div class="details-list__item u-flexbox" data-testid="item-attributes-upload_date">'
        '<div class="details-list__item-value"><span>Caricato</span></div>'
        '<div class="details-list__item-value" itemprop="upload_date">'
        "<span>16 ore fa</span></div></div>"
        "<div>Ultima visita: Proprio adesso</div>"
    )
    result = parse_details(html, request(), datetime.now(UTC))
    assert result.publication_evidence == "relative:16 ore fa"
    assert result.published_at is None


@pytest.mark.parametrize(
    "html",
    [
        '<div data-testid="item-attributes-upload_date"><div itemprop="upload_date"> </div></div>',
        "<h1>Just a moment ago</h1><div>Last seen just now</div>",
    ],
)
def test_empty_uploaded_or_unrelated_text_does_not_prove_age(html):
    assert parse_details(html, request(), datetime.now(UTC)).publication_evidence == "unknown"


def test_conflicting_page_dates_cannot_fall_back_to_young_uploaded_label():
    html = fixture(stamp="2026-10-02T10:00:00Z").replace("Il y a 5 minutes", "Just now")
    html += (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Product",
                "url": "https://www.vinted.fr/items/10001",
                "datePublished": "2026-10-02T09:00:00Z",
            }
        )
        + "</script>"
    )
    result = parse_details(html, request(), datetime.now(UTC))
    assert result.published_at is None
    assert result.publication_evidence == "conflicting_dates"


def test_description_belongs_to_requested_item_not_recommendations():
    data = {"item_id": "10001", "description": "Small hole on sleeve"}
    wrong = {"item_id": "99999", "description": "Wrong description"}
    script = (
        "<script>self.__next_f.push("
        + json.dumps([1, "2:" + json.dumps([data, wrong]) + "\n"])
        + ");</script>"
    )
    result = parse_details(fixture() + script, request(), datetime.now(UTC))
    assert result.description == "Small hole on sleeve"


def test_api_description_and_empty_description_are_preserved():
    from vpars_parser.sources.vinted_details import parse_api_details

    for description in ("Small hole", "", None):
        payload = {
            "code": 0,
            "item": {
                "id": 10001,
                "description": description,
                "photos": [{"url": "https://images1.vinted.net/a.webp"}],
            },
        }
        assert (
            parse_api_details(json.dumps(payload), request(), datetime.now(UTC)).description
            == description
        )


@pytest.mark.parametrize(
    "stamp,known",
    [("2026-09-24T10:00:00Z", True), ("2026-09-24T10:00:00", False), ("just now", False)],
)
def test_only_explicit_zoned_publication_time(stamp, known):
    assert (
        parse_details(fixture(stamp=stamp), request(), datetime.now(UTC)).published_at is not None
    ) == known


def test_missing_gallery_or_invalid_photo_preserves_available_metadata():
    for html in ("<html>other item</html>", fixture(malformed=True)):
        result = parse_details(html, request(), datetime.now(UTC))
        assert not result.photos_complete
    assert result.publication_evidence == "relative:Il y a 5 minutes"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.vinted.fr/items/100010-other",
        "https://www.vinted.de/items/10001",
        "https://www.vinted.fr/catalog",
    ],
)
def test_item_redirect_cannot_select_other_lot(url):
    with pytest.raises(ParserError):
        validate_item_page(page(final_url=url), request())


async def test_item_service_cancels_network_work_when_client_leaves(gateway):
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch_item(query):
        started.set()
        await release.wait()
        return page()

    gateway.fetch_item = fetch_item
    service = ParserService(gateway, Settings(max_inflight=1))
    caller = asyncio.create_task(service.details(request()))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not service.active and not service.detail_active
    release.set()
    assert (await service.details(request())).photos_complete
    await service.close()
    assert not service.active


async def test_newest_query_is_verified(gateway):
    service = ParserService(gateway, Settings())
    result = await service.parse(ParseRequest(region_id="FR", newest_first=True))
    assert result.items
    assert gateway.calls[0].newest_first
    assert result.sort_order == "newest_first_requested"
    assert not service.registry.get("vinted").capabilities([]).newest_first_guaranteed
    gateway.changes["url"] = "https://www.vinted.fr/catalog"
    with pytest.raises(ParserError) as caught:
        await service.parse(ParseRequest(region_id="FR", newest_first=True))
    assert caught.value.detail.code == "GatewayContractError"


async def test_invalid_card_attributes_do_not_hide_identity_from_baseline(gateway):
    from conftest import html

    body = html("FR").replace(
        'data-testid="product-item-id-10001--price-text"', 'data-testid="price-changed"'
    )
    gateway.changes["html"] = body.encode()
    result = await ParserService(gateway, Settings()).parse(ParseRequest(region_id="FR"))
    assert "10001" not in {item.source_item_id for item in result.items}
    assert "10001" in result.observed_item_ids
    assert result.identities_complete and result.completeness == "partial"
