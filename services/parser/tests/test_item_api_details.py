import json
from datetime import UTC, datetime

import pytest

from vpars_parser.errors import ParserError
from vpars_parser.models import ItemDetailsRequest
from vpars_parser.ports import GatewayPage
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings
from vpars_parser.sources.vinted_details import parse_api_details, validate_item_page

NOW = datetime(2026, 9, 25, tzinfo=UTC)
REQUEST = ItemDetailsRequest(region_id="FR", source_item_id="123")


def data(**changes):
    item = {
        "id": 123,
        "is_draft": False,
        "is_processing": False,
        "is_hidden": False,
        "is_closed": False,
        "photos": [
            {"url": "https://images1.vinted.net/a.webp"},
            {"url": "https://images1.vinted.net/b.webp"},
        ],
        "created_at_ts": "2026-09-25T02:00:00+02:00",
    }
    return json.dumps({"code": 0, "item": item | changes}).encode()


@pytest.mark.parametrize(
    "stamp", ["2026-09-25T00:00:00Z", "2026-09-25T02:00:00+02:00", NOW.timestamp()]
)
def test_source_creation_date_is_utc_and_keeps_full_gallery(stamp):
    result = parse_api_details(data(created_at_ts=stamp), REQUEST, NOW)
    assert result.published_at == NOW
    assert result.publication_evidence == "api:created_at_ts"
    assert len(result.photo_urls) == 2 and result.photos_complete


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        True,
        float("nan"),
        float("inf"),
        10**30,
        NOW.timestamp() * 1000,
        "2026-09-25T00:00:00",
        "just now",
    ],
)
def test_ambiguous_or_missing_date_does_not_make_a_fresh_item(stamp):
    result = parse_api_details(data(created_at_ts=stamp), REQUEST, NOW)
    assert result.published_at is None


@pytest.mark.parametrize("field", ["is_draft", "is_processing", "is_hidden", "is_closed"])
@pytest.mark.parametrize("value", [True, None, "false"])
def test_unpublished_or_unknown_state_does_not_pass_freshness(field, value):
    assert parse_api_details(data(**{field: value}), REQUEST, NOW).published_at is None


def test_photo_seller_and_bump_timestamps_are_not_listing_dates():
    result = parse_api_details(
        data(
            created_at_ts=None,
            updated_at=NOW.isoformat(),
            last_push_up_at=NOW.isoformat(),
            user={"created_at_ts": NOW.isoformat()},
            photos=[
                {
                    "url": "https://images1.vinted.net/a.webp",
                    "high_resolution": {"timestamp": NOW.timestamp()},
                }
            ],
        ),
        REQUEST,
        NOW,
    )
    assert result.published_at is None


def test_disagreeing_dates_and_wrong_identity_fail_closed():
    result = parse_api_details(data(published_at="2026-09-25T00:00:01Z"), REQUEST, NOW)
    assert result.published_at is None and result.publication_evidence == "conflicting_dates"
    with pytest.raises(ParserError, match="another item"):
        parse_api_details(data(id=124), REQUEST, NOW)
    result = parse_api_details(data(photos=[{"url": "file:///secret"}]), REQUEST, NOW)
    assert not result.photos_complete and not result.photo_urls
    assert result.published_at == NOW


async def test_api_response_reaches_details_service_without_html(gateway):
    url = "https://www.vinted.fr/api/v2/items/123/details"
    page = GatewayPage("FR", url, url, "PAGE_LOADED", b"", 200, data())

    async def fetch_item(request):
        return page

    gateway.fetch_item = fetch_item
    result = await ParserService(gateway, Settings()).details(REQUEST)
    assert result.published_at == NOW and len(result.photo_urls) == 2
    for url in (
        "https://www.vinted.de/api/v2/items/123/details",
        "https://www.vinted.fr/api/v2/items/124/details",
        "https://www.vinted.fr/api/v2/items/123/details?other=1",
    ):
        with pytest.raises(ParserError):
            validate_item_page(
                GatewayPage("FR", page.url, url, "PAGE_LOADED", b"", 200, data()), REQUEST
            )


@pytest.mark.parametrize("field", ["is_draft", "is_processing", "is_hidden", "is_closed"])
def test_missing_state_flag_preserves_explicit_publication_but_not_creation(field):
    payload = json.loads(data())
    del payload["item"][field]
    assert parse_api_details(json.dumps(payload), REQUEST, NOW).published_at is None
    payload["item"]["published_at"] = NOW.isoformat()
    assert parse_api_details(json.dumps(payload), REQUEST, NOW).published_at == NOW
    payload["item"][field] = True
    assert parse_api_details(json.dumps(payload), REQUEST, NOW).published_at is None


async def test_unknown_api_age_uses_html_uploaded_and_preserves_api_metadata(gateway):
    api_url = "https://www.vinted.fr/api/v2/items/123/details"
    html_url = "https://www.vinted.fr/items/123"
    calls = []

    async def fetch_item(request):
        calls.append(request.force_html)
        if request.force_html:
            return GatewayPage(
                "FR",
                html_url,
                html_url,
                "PAGE_LOADED",
                (
                    b'<div data-testid="item-attributes-upload_date">'
                    b'<div itemprop="upload_date">Il y a quelques secondes</div></div>'
                ),
                200,
            )
        return GatewayPage(
            "FR",
            api_url,
            api_url,
            "PAGE_LOADED",
            b"",
            200,
            data(created_at_ts=None, description="Good", user={"id": 50}),
        )

    gateway.fetch_item = fetch_item
    result = await ParserService(gateway, Settings()).details(REQUEST)
    assert calls == [False, True]
    assert result.publication_evidence == "relative:Il y a quelques secondes"
    assert result.published_at is None  # No fabricated exact timestamp.
    assert result.description == "Good" and result.photos_complete and len(result.photo_urls) == 2


async def test_html_fallback_429_is_not_converted_to_age_unknown(gateway):
    url = "https://www.vinted.fr/api/v2/items/123/details"
    calls = []

    async def fetch_item(request):
        calls.append(request.force_html)
        if request.force_html:
            return GatewayPage("FR", url, url, "RATE_LIMITED", b"", 429, retry_after=900)
        return GatewayPage("FR", url, url, "PAGE_LOADED", b"", 200, data(created_at_ts=None))

    gateway.fetch_item = fetch_item
    with pytest.raises(ParserError) as error:
        await ParserService(gateway, Settings()).details(REQUEST)
    assert error.value.detail.code == "SourceRateLimited"
    assert error.value.detail.retry_after == 900 and calls == [False, True]


async def test_old_gateway_cannot_loop_api_instead_of_html(gateway):
    url = "https://www.vinted.fr/api/v2/items/123/details"
    calls = []

    async def fetch_item(request):
        calls.append(request.force_html)
        return GatewayPage("FR", url, url, "PAGE_LOADED", b"", 200, data(created_at_ts=None))

    gateway.fetch_item = fetch_item
    with pytest.raises(ParserError, match="HTML requirement"):
        await ParserService(gateway, Settings()).details(REQUEST)
    assert calls == [False, True]


def test_html_fallback_cannot_erase_conflicting_dates():
    from vpars_parser.service import merge_item_details

    api = parse_api_details(data(), REQUEST, NOW)
    html = api.model_copy(update={"published_at": NOW.replace(second=1)})
    result = merge_item_details(api, html)
    assert result.published_at is None and result.publication_evidence == "conflicting_dates"
