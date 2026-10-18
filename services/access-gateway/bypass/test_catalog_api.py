import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from catalog_api import CatalogAPI, api_html, api_target
from catalog_document import complete_catalog
from vinted_regions import get_region


class Context:
    pass


def data(code="FR", page=1):
    return {"items": [{"id": i, "url": f"/items/{i}-shirt", "title": "Shirt",
            "price": {"amount": "10.00", "currency_code": "GBP" if code == "UK" else "EUR"},
            "item_box": {"item_id": i, "accessibility_label": "Shirt, Brand: Adidas, Size: L"},
            "photo": {"url": "https://images1.vinted.net/original.jpg", "thumbnails": [
                {"type": "thumb310x430", "url": f"https://images1.vinted.net/{i}.webp"}]}}
            for i in (123, 456)],
            "pagination": {"current_page": page, "per_page": 96, "total_pages": 10}}


@pytest.mark.parametrize("code,brand", [("DE", "Nike"), ("FR", "Adidas"), ("IT", "Stone Island"), ("UK", "Amiri")])
@pytest.mark.parametrize("page", [1, 3, 10])
def test_full_inventory_fields_and_pagination_across_regions(code, brand, page):
    region = get_region(code)
    url = region.catalog_url(brand, page, True)
    html = api_html(data(code, page), url, region, 96)
    assert complete_catalog(html, url, region)
    assert "123.webp" in html and "456.webp" in html
    assert f'aria-disabled="{str(page == 10).lower()}"' in html
    assert f"api.{region.domain}/svc-catalogue/items?" in api_target(url, region, 96)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(items=[]), lambda d: d.update(replica_warning=True),
    lambda d: d.update(errors=[]), lambda d: d.update(code=401),
    lambda d: d["items"].append(copy.deepcopy(d["items"][0])),
    lambda d: d["items"][0]["item_box"].update(item_id=789),
    lambda d: d["items"][0].update(discount={"amount": "5.00"}),
    lambda d: d["items"][0]["price"].update(currency_code="GBP"),
    lambda d: d["pagination"].update(per_page=24),
    lambda d: d["pagination"].update(current_page=2),
])
def test_partial_ambiguous_and_warning_data_cannot_become_success(mutate):
    payload = data()
    mutate(payload)
    with pytest.raises((ValueError, KeyError, TypeError)):
        api_html(payload, get_region("FR").catalog_url("Adidas", 1, True), get_region("FR"), 96)


@pytest.mark.parametrize("code,brand", [("FR", "Nike"), ("DE", "Gucci"), ("IT", "Adidas"), ("UK", "Amiri")])
def test_optional_redundant_box_id_and_thumbnail_list_do_not_force_html(code, brand):
    region = get_region(code)
    payload = data(code)
    for item in payload["items"]:
        item["item_box"].pop("item_id")
        item["photo"].pop("thumbnails")
    html = api_html(payload, region.catalog_url(brand, 1, True), region, 96)
    assert complete_catalog(html, region.catalog_url(brand, 1, True), region)
    assert "original.jpg" in html


@pytest.mark.parametrize("code,brand", [("FR", "Nike"), ("DE", "Gucci"), ("IT", "Adidas"), ("UK", "Amiri")])
@pytest.mark.parametrize("missing", [True, False])
def test_source_card_without_photo_remains_in_exact_inventory(code, brand, missing):
    region, payload = get_region(code), data(code)
    if missing:
        payload["items"][0].pop("photo")
    else:
        payload["items"][0]["photo"] = None
    url = region.catalog_url(brand, 1, True)
    html = api_html(payload, url, region, 96)
    assert complete_catalog(html, url, region)
    assert 'data-testid="product-item-id-123"' in html
    assert 'product-item-id-123--overlay-link' in html and 'product-item-id-123--price-text' in html
    assert 'product-item-id-123--image--img' not in html
    assert 'product-item-id-456--image--img' in html
    # Explicit no-photo data cannot excuse a different, truncated card.
    assert not complete_catalog(html.replace('456--image--img', '456--missing'), url, region)
    assert not complete_catalog(html.replace('123--price-text', '123--missing'), url, region)


@pytest.mark.parametrize("photo", ["", False, [], {}, {"url": "invalid"}])
def test_unknown_photo_shape_cannot_become_a_verified_catalog(photo):
    payload = data()
    payload["items"][0]["photo"] = photo
    region = get_region("FR")
    with pytest.raises((ValueError, KeyError, TypeError, AttributeError)):
        api_html(payload, region.catalog_url("Nike", 1, True), region, 96)


@pytest.mark.asyncio
async def test_missing_required_data_has_precise_safe_diagnostic_without_accepting_partial_catalog(caplog):
    payload = data()
    del payload["items"][0]["item_box"]["accessibility_label"]
    client, context, route, _, url = setup(body=json.dumps(payload).encode())
    with caplog.at_level("INFO", logger="uvicorn.error"):
        assert await client.handle(route, url, get_region("FR"), lambda: 5000, context) is None
    assert "stage=validate reason=MissingField:accessibility_label" in caplog.text
    route.fulfill.assert_not_awaited()


@pytest.mark.parametrize("url", [None, "https://other.test/catalog?order=newest_first",
    "https://www.vinted.fr/catalog", "https://www.vinted.fr/catalog?order=newest_first&brand_ids[]=1",
    "https://www.vinted.fr/catalog?order=newest_first&page=1&page=2",
    "https://www.vinted.fr/catalog?order=newest_first&page=-1"])
def test_unknown_query_semantics_never_lose_filters(url):
    assert api_target(url, get_region("FR"), 96) is None


def setup(status=200, body=None, headers=None):
    client, context = CatalogAPI(clock=lambda: 100), Context()
    client.sessions[context] = {"FR": (96, "fr-FR")}
    response = SimpleNamespace(status=status, body=AsyncMock(return_value=body or json.dumps(data()).encode()),
        headers=headers or {"content-type": "application/json", "content-encoding": "br"}, dispose=AsyncMock())
    route = SimpleNamespace(request=SimpleNamespace(headers={"cookie": "private", "user-agent": "browser"}),
        fetch=AsyncMock(return_value=response), fulfill=AsyncMock())
    return client, context, route, response, get_region("FR").catalog_url("Adidas", 1, True)


@pytest.mark.asyncio
async def test_cold_context_does_not_call_api_and_ready_context_uses_brotli_without_cookie_copy():
    client, context, route, reply, url = setup()
    assert await client.handle(route, url, get_region("FR"), lambda: 9000, Context()) is None
    route.fetch.assert_not_awaited()
    assert await client.handle(route, url, get_region("FR"), lambda: 9000, context) is True
    assert route.fetch.call_args.kwargs["headers"]["accept-encoding"] == "br, gzip"
    assert "cookie" not in route.fetch.call_args.kwargs["headers"]
    assert route.fetch.call_args.kwargs["max_retries"] == 0
    assert route.fetch.call_args.kwargs["timeout"] == 8000
    assert "content-encoding" not in route.fulfill.call_args.kwargs["headers"]
    reply.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_denials_preserve_backoff_and_session_without_fallback(status):
    client, context, route, reply, url = setup(status, b"denied", {"retry-after": "90"})
    assert await client.handle(route, url, get_region("FR"), lambda: 5000, context) is False
    route.fetch.assert_awaited_once()
    assert route.fulfill.call_args.kwargs == {"status": status, "body": b"denied", "headers": {"retry-after": "90"}}
    assert client.ready(context, get_region("FR"))
    assert not client.disabled_until


@pytest.mark.asyncio
async def test_unsupported_endpoint_is_bounded_and_cancel_does_not_poison_cache():
    client, context, route, reply, url = setup(404)
    assert await client.handle(route, url, get_region("FR"), lambda: 5000, context) is None
    assert await client.handle(route, url, get_region("FR"), lambda: 5000, context) is None
    route.fetch.assert_awaited_once()
    client.disabled_until.clear()
    route.fetch.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client.handle(route, url, get_region("FR"), lambda: 5000, context)
    assert not client.disabled_until
