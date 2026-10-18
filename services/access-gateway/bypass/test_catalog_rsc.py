import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from catalog_document import complete_catalog
from catalog_rsc import CompactCatalog, Routing, catalog_html
from vinted_regions import get_region


def flight(code="FR", text="Adidas", page=1, *, mutate=None, build="build-1"):
    currency, symbol = ("GBP", "£") if code == "UK" else ("EUR", "€")
    item = {"id": 123, "productItem": {"id": 123, "url": "/items/123-shirt",
        "price": {"amount": "10.00", "currencyCode": currency}, "priceWithDiscount": None,
        "thumbnailUrl": "https://images1.vinted.net/photo.webp",
        "itemBox": {"itemId": "123", "accessibilityLabel": f"Shirt <&>, Brand: {text}, Size: L, 10.00 {symbol}, 11.20 {symbol}"}}}
    state = {"items": [item], "pagination": {"current_page": page, "per_page": 96, "total_pages": 10},
             "uiState": "SUCCESS", "error": "$undefined", "replicaWarning": "$undefined"}
    if mutate:
        mutate(state)
    url = get_region(code).catalog_url(text, page, True)
    root = {"b": build, "q": "?" + url.partition("?")[2],
            "f": [[["", {"children": ["__PAGE__?old-query", {}]}]]], "state": state}
    return "0:" + json.dumps(root) + "\n", url


def html_stream(stream):
    return '<script>self.__next_f.push(' + json.dumps([1, stream]) + ')</script>'


@pytest.mark.parametrize("code,brand", [("DE", "Nike"), ("FR", "Adidas"), ("IT", "Stone Island"), ("UK", "Amiri")])
def test_source_fields_project_to_verified_complete_document(code, brand):
    for page in (1, 2, 9):
        stream, url = flight(code, brand, page)
        html = catalog_html(stream, url, get_region(code), "build-1")
        assert complete_catalog(html, url, get_region(code))
        assert "&lt;&amp;&gt;" in html
        assert f"page={page + 1}" in html


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(items=[]), lambda s: s.update(replicaWarning=True),
    lambda s: s.update(error={"code": 42}), lambda s: s.update(uiState="LOADING"),
    lambda s: s["items"].append(s["items"][0]),
    lambda s: s["items"][0]["productItem"].update(id=124),
    lambda s: s["items"][0]["productItem"].update(priceWithDiscount={"amount": "9.00"}),
    lambda s: s["items"][0]["productItem"].update(url="https://example.com/items/123"),
    lambda s: s["items"][0]["productItem"]["price"].update(currencyCode="USD"),
    lambda s: s["pagination"].update(current_page=2),
])
def test_ambiguous_or_changed_data_rejects_whole_compact_page(mutate):
    stream, url = flight(mutate=mutate)
    with pytest.raises(ValueError):
        catalog_html(stream, url, get_region("FR"), "build-1")


def test_query_build_truncation_and_server_errors_do_not_become_empty_results():
    stream, url = flight()
    for data, target, build in ((stream[:-1], url, "build-1"),
                                (stream + '9:E{"error":"failed"}\n', url, "build-1"),
                                (stream, url.replace("Adidas", "Nike"), "build-1"),
                                (stream, url, "build-2")):
        with pytest.raises(ValueError):
            catalog_html(data, target, get_region("FR"), build)


def response(status=200, body=b"", headers=None):
    return SimpleNamespace(status=status, body=AsyncMock(return_value=body), dispose=AsyncMock(),
        headers=headers or {"content-type": "text/x-component"})


def setup(reply):
    stream, url = flight()
    client = CompactCatalog(clock=lambda: 100)
    client.learn(html_stream(stream), get_region("FR"))
    route = SimpleNamespace(request=SimpleNamespace(headers={"user-agent": "browser"}),
        fetch=AsyncMock(return_value=reply), fulfill=AsyncMock())
    return client, route, stream, url


@pytest.mark.asyncio
async def test_redirect_key_is_reused_and_never_fetches_html_on_success():
    stream, url = flight()
    client, route, _, _ = setup(None)
    redirect = response(307, headers={"location": url + "&_rsc=public-key"})
    good = response(body=stream.encode())
    route.fetch.side_effect = [redirect, good]
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is True
    assert route.fetch.await_count == 2
    assert client.routes["FR"].cache_key == "public-key"
    route.fetch.reset_mock(side_effect=True)
    route.fetch.return_value = good
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is True
    route.fetch.assert_awaited_once()
    assert route.fetch.call_args.kwargs["url"].endswith("&_rsc=public-key")
    assert route.fetch.call_args.kwargs["headers"]["user-agent"] == "browser"
    assert route.fetch.call_args.kwargs["headers"]["accept-encoding"] == "br, gzip"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_refusal_preserves_status_retry_after_and_does_not_fallback(status):
    reply = response(status, b"refused", {"retry-after": "900", "content-encoding": "gzip"})
    client, route, _, url = setup(reply)
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is False
    route.fetch.assert_awaited_once()
    assert route.fulfill.call_args.kwargs == {
        "status": status, "body": b"refused", "headers": {"retry-after": "900"}}
    reply.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_html_challenge_is_rendered_once_in_same_navigation():
    reply = response(200, b"challenge", {"content-type": "text/html"})
    client, route, _, url = setup(reply)
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is False
    route.fetch.assert_awaited_once()
    assert route.fulfill.call_args.kwargs["body"] == b"challenge"


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["https://example.com/catalog?_rsc=x", "/admin/catalog?_rsc=x",
                                       "/catalog?search_text=other&_rsc=x"])
async def test_unexpected_redirect_disables_shortcut_without_following(location):
    client, route, _, url = setup(response(307, headers={"location": location}))
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is None
    assert client.disabled_until["FR"] == 400
    route.fetch.assert_awaited_once()
    route.fulfill.assert_not_awaited()
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is None
    route.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_fetch_does_not_initiate_fallback_or_poison_route_cache():
    client, route, _, url = setup(None)
    route.fetch.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client.handle(route, url, get_region("FR"), lambda: 5000)
    assert not client.disabled_until
    route.fulfill.assert_not_awaited()


def test_only_routing_metadata_is_cached_and_new_build_drops_old_redirect_key():
    stream, _ = flight()
    client = CompactCatalog(clock=lambda: 100)
    client.learn(html_stream(stream), get_region("FR"))
    entry = client.routes["FR"]
    assert isinstance(entry, Routing)
    assert "Adidas" not in entry.header and "old-query" not in entry.header
    entry.cache_key = "old-key"
    stream, _ = flight(build="build-2")
    client.learn(html_stream(stream), get_region("FR"))
    assert client.routes["FR"].cache_key is None
    assert set(client.routes) == {"FR"}


@pytest.mark.asyncio
async def test_new_verified_build_recovers_without_five_minutes_of_html():
    client, route, old, url = setup(None)
    new, _ = flight(build="build-2")
    route.fetch.return_value = response(body=new.encode())
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is None
    assert "FR" in client.disabled_until
    client.disable("another-query")
    # Relearning the old build does not erase its protocol failure.
    client.learn(html_stream(old), get_region("FR"))
    assert "FR" in client.disabled_until
    client.learn(html_stream(new), get_region("FR"))
    assert "FR" not in client.disabled_until
    assert "another-query" in client.disabled_until
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is True


@pytest.mark.asyncio
async def test_one_bad_query_does_not_disable_other_brands_or_future_searches():
    bad, url = flight(mutate=lambda state: state.update(replicaWarning=True))
    client, route, _, _ = setup(response(body=bad.encode()))
    assert await client.handle(route, url, get_region("FR"), lambda: 5000) is None
    assert url in client.disabled_until and "FR" not in client.disabled_until
    good, other_url = flight(text="Nike")
    route.fetch.return_value = response(body=good.encode())
    assert await client.handle(route, other_url, get_region("FR"), lambda: 5000) is True
    for number in range(300):
        client.disable(f"query-{number}")
    assert len(client.disabled_until) == 256
