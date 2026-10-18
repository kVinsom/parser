import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from byparr_runtime import BrowserSession, complete_item_document, install_catalog_readiness
from catalog_document import DocumentGate, complete_catalog
from vinted_regions import get_region


def catalog(ids=(101, 102), *, page=1, state_changes=None):
    state = {"items": [{"id": identity} for identity in ids],
             "pagination": {"current_page": page, "per_page": 96}, "uiState": "SUCCESS",
             "error": "$undefined", "replicaWarning": "$undefined"}
    state.update(state_changes or {})
    cards = "".join(
        f'<div data-testid="product-item-id-{i}">'
        f'<a data-testid="product-item-id-{i}--overlay-link" href="/items/{i}-shirt">Shirt</a>'
        f'<p data-testid="product-item-id-{i}--price-text">10,00 €</p>'
        f'<img data-testid="product-item-id-{i}--image--img" src="https://images.vinted.net/{i}"></div>'
        for i in ids)
    # Split within a JSON value, as real streamed responses do.
    payload = "1:" + json.dumps(state) + "\n"
    scripts = "".join('<script>self.__next_f.push(' + json.dumps([1, part]) + ')</script>'
                      for part in (payload[:30], payload[30:]))
    return ('<html><title>Catalog | Vinted</title><body>' + cards
            + '<a data-testid="catalog-pagination--next-page">Next</a>' + scripts + '</body></html>')


@pytest.mark.parametrize("code,brand", [("DE", "Nike"), ("FR", "Adidas"), ("IT", "Stone Island"), ("UK", "Amiri")])
def test_complete_stream_across_regions_and_pages(code, brand):
    region = get_region(code)
    for page in (1, 2, 7):
        assert complete_catalog(catalog(page=page), region.catalog_url(brand, page, True), region)


@pytest.mark.parametrize("change", [
    lambda b: b[:-7],
    lambda b: b.replace('product-item-id-102"', 'unknown-card"'),
    lambda b: b.replace('102--price-text', '102--unknown'),
    lambda b: b.replace('102--image--img', '102--unknown'),
    lambda b: b.replace('102--overlay-link', '102--unknown'),
    lambda b: b.replace('catalog-pagination--next-page', 'unknown-pagination'),
    lambda b: b.replace('</body>', '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe></body>'),
])
def test_incomplete_or_blocked_html_never_shortcuts(change):
    region = get_region("FR")
    assert not complete_catalog(change(catalog()), region.catalog_url(), region)


@pytest.mark.parametrize("state", [
    {"uiState": "LOADING"}, {"error": {"message": "failed"}}, {"replicaWarning": True},
    {"items": []}, {"items": [{"id": 101}]}, {"items": [{"id": 101}, {"id": 101}]},
    {"pagination": {"current_page": 2, "per_page": 96}},
    {"pagination": {"current_page": 1, "per_page": 1}},
])
def test_source_state_must_confirm_exact_card_inventory(state):
    region = get_region("FR")
    assert not complete_catalog(catalog(state_changes=state), region.catalog_url(), region)


class Page:
    def __init__(self, url):
        self.url = url
        self.route = AsyncMock()
        self.goto = AsyncMock()
        self.content = AsyncMock(return_value=catalog())
        self.wait_for_load_state = AsyncMock()


def route(url="https://marketplace-web-assets.vinted.com/_next/static/app.js", kind="script"):
    return SimpleNamespace(request=SimpleNamespace(url=url, resource_type=kind),
                           fallback=AsyncMock(), abort=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_resources_wait_for_verdict_then_abort_or_continue(accepted):
    gate = DocumentGate()
    page = Page("https://www.vinted.fr/catalog")
    await gate.attach(page)
    handler = page.route.call_args.args[1]
    resource = route()
    pending = asyncio.create_task(handler(resource))
    await asyncio.sleep(0)
    resource.fallback.assert_not_awaited()
    resource.abort.assert_not_awaited()
    gate.release(accepted)
    await pending
    assert resource.abort.await_count == int(accepted)
    assert resource.fallback.await_count == int(not accepted)


@pytest.mark.asyncio
async def test_document_and_access_checks_never_wait_and_timeout_fails_open():
    gate = DocumentGate(hold_seconds=0.01)
    page = Page("https://www.vinted.fr/catalog")
    await gate.attach(page)
    await gate.attach(page)
    page.route.assert_awaited_once()
    handler = page.route.call_args.args[1]
    for resource in (route(kind="document"), route("https://www.vinted.fr/cdn-cgi/check.js"),
                     route("https://geo.captcha-delivery.com/captcha/"), route()):
        await asyncio.wait_for(handler(resource), timeout=0.1)
        resource.fallback.assert_awaited_once()
        resource.abort.assert_not_awaited()


def environment(response_body, status=200):
    url = "https://www.vinted.fr/catalog"
    page = Page(url)
    response = SimpleNamespace(url=url, status=status, body=AsyncMock(return_value=response_body),
                               header_value=AsyncMock(return_value="text/html; charset=utf-8"))
    page.goto.return_value = response
    endpoints = SimpleNamespace(_navigate_and_solve=AsyncMock(), remaining_ms=Mock(return_value=10000),
        challenge_present=AsyncMock(return_value=False), solve_challenge=AsyncMock(),
        _wait_for_networkidle=AsyncMock())
    install_catalog_readiness(endpoints, document_catalog=True)
    return endpoints, page, response, BrowserSession(page, None), SimpleNamespace(url=url, block_media=True)


@pytest.mark.asyncio
async def test_complete_document_avoids_settling_and_returns_original_body():
    body = catalog().encode()
    endpoints, page, response, dep, request = environment(body)
    result = await endpoints._navigate_and_solve(dep, request, object())
    assert result == (False, body.decode(), response)
    page.goto.assert_awaited_once_with(request.url, wait_until="commit", timeout=10000)
    page.content.assert_not_awaited()
    endpoints._wait_for_networkidle.assert_not_awaited()


def item_document(brand="Nike", identity="123"):
    item = {"item_id": identity, "description": brand + " Good condition",
            "photos": [{"url": "https://images1.vinted.net/photo.webp"}],
            "seller_id": "45", "feedback_count": 10, "feedback_reputation": 0.9}
    script = json.dumps([1, "1:" + json.dumps(item) + "\n"])
    return (f'<html><title>{brand} | Vinted</title><body>'
            '<img data-testid="item-photo-1" src="https://images1.vinted.net/photo.webp">'
            '<div data-testid="item-attributes-upload_date">'
            '<span itemprop="upload_date">Just now</span></div>'
            f'<script>self.__next_f.push({script})</script></body></html>')


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["FR", "IT", "DE", "UK"])
@pytest.mark.parametrize("brand", ["Nike", "Gucci"])
async def test_ready_item_document_skips_application_and_keeps_all_source_metadata(code, brand):
    body = item_document(brand)
    endpoints, page, response, dep, request = environment(body.encode())
    request.url = page.url = response.url = get_region(code).base_url + "/items/123-shirt"
    result = await endpoints._navigate_and_solve(dep, request, object())
    assert result == (False, body, response)
    page.goto.assert_awaited_once_with(request.url, wait_until="commit", timeout=10000)
    page.content.assert_not_awaited()
    page.wait_for_load_state.assert_not_awaited()
    endpoints._wait_for_networkidle.assert_not_awaited()
    handler = page.route.call_args.args[1]
    for kind, url in (("script", "https://marketplace-web-assets.vinted.com/_next/static/item.js"),
                      ("fetch", get_region(code).base_url + "/api/v2/items/123"),
                      ("image", "https://images1.vinted.net/photo.webp")):
        resource = route(url, kind)
        await handler(resource)
        resource.abort.assert_awaited_once()
        resource.fallback.assert_not_awaited()


@pytest.mark.parametrize("change", [
    lambda body: body[:-7],
    lambda body: body.replace(r'\"item_id\": \"123\"', r'\"item_id\": \"456\"'),
    lambda body: body.replace(r'\"description\":', r'\"unknown_description\":'),
    lambda body: body.replace(r'\"photos\":', r'\"unknown_photos\":'),
    lambda body: body.replace('</body>', '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe></body>'),
])
def test_unknown_or_incomplete_item_document_keeps_normal_rendering(change):
    assert not complete_item_document(change(item_document()), "https://www.vinted.fr/items/123", get_region("FR"), "123")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 429, 503])
async def test_shell_or_refusal_keeps_same_navigation_without_retry(status):
    endpoints, page, response, dep, request = environment(b"<html>shell</html>", status)
    result = await endpoints._navigate_and_solve(dep, request, object())
    assert result[2].status == status
    page.goto.assert_awaited_once()
    assert endpoints._wait_for_networkidle.await_count == int(status == 200)
    resource = route()
    await page.route.call_args.args[1](resource)
    resource.fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreadable_body_renders_and_cancelled_read_releases_routes():
    endpoints, page, response, dep, request = environment(b"")
    response.body.side_effect = RuntimeError("browser body unavailable")
    await endpoints._navigate_and_solve(dep, request, object())
    page.goto.assert_awaited_once()
    endpoints._wait_for_networkidle.assert_awaited_once()
    response.body.side_effect = asyncio.Event().wait
    pending = asyncio.create_task(endpoints._navigate_and_solve(dep, request, object()))
    await asyncio.sleep(0.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    resource = route()
    await asyncio.wait_for(page.route.call_args.args[1](resource), timeout=0.1)
    resource.fallback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"block_media": False}, {"return_only_cookies": True}])
async def test_explicit_media_or_cookie_requests_keep_existing_browser_behavior(options):
    endpoints, page, _, dep, request = environment(catalog().encode())
    for key, value in options.items():
        setattr(request, key, value)
    await endpoints._navigate_and_solve(dep, request, object())
    page.goto.assert_awaited_once_with(request.url, wait_until="domcontentloaded", timeout=10000)
    page.route.assert_not_awaited()
    endpoints._wait_for_networkidle.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_complete_stream_keeps_application_scripts_paused():
    endpoints, page, response, dep, request = environment(catalog().encode())
    started = asyncio.Event()

    async def slow_body():
        started.set()
        await asyncio.sleep(2.05)  # Previously fell back at 2 seconds.
        return catalog().encode()

    response.body.side_effect = slow_body
    navigation = asyncio.create_task(endpoints._navigate_and_solve(dep, request, object()))
    await started.wait()
    resource = route()
    resource_task = asyncio.create_task(page.route.call_args.args[1](resource))
    result = await navigation
    await resource_task
    assert result[1] == catalog()
    resource.abort.assert_awaited_once()
    resource.fallback.assert_not_awaited()
    endpoints._wait_for_networkidle.assert_not_awaited()


@pytest.mark.asyncio
async def test_body_and_resource_wait_use_one_deadline_and_reserve_fallback_time():
    region = get_region("FR")
    gate = DocumentGate(hold_seconds=8, remaining=lambda: 80)
    # 25% of a small request budget is reserved for the same-page fallback.
    # Subtracting monotonic timestamps can differ by a few picoseconds.
    assert 0.04 < gate.wait_seconds() <= 0.06 + 1e-9
    deadline = gate.deadline
    await asyncio.sleep(0.03)
    assert gate.deadline == deadline
    assert gate.wait_seconds() < 0.04
    page = Page(region.catalog_url())
    await gate.attach(page)
    resource = route()
    task = asyncio.create_task(page.route.call_args.args[1](resource))
    response = SimpleNamespace(status=200, url=page.url,
        header_value=AsyncMock(return_value="text/html"), body=AsyncMock(side_effect=asyncio.Event().wait))
    assert await gate.read(response, page.url, region) is None
    gate.release()
    await task
    resource.fallback.assert_awaited_once()
    resource.abort.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,brand", [("FR", "Nike"), ("DE", "Adidas"),
                                      ("IT", "Stone Island"), ("UK", "Amiri")])
@pytest.mark.parametrize("eligible", [True, False])
async def test_choose_one_compact_transport_before_html(code, brand, eligible):
    region = get_region(code)
    url = region.catalog_url(brand, 1, True)
    page = Page(url)
    page.main_frame = object()
    api = SimpleNamespace(can_handle=Mock(return_value=eligible), handle=AsyncMock(return_value=None))
    compact = SimpleNamespace(handle=AsyncMock(return_value=None))
    gate = DocumentGate(api=api, compact=compact, context=object(), url=url, region=region,
                        remaining=lambda: 10000)
    await gate.attach(page)
    document = route(url, "document")
    document.request.frame = page.main_frame
    await page.route.call_args.args[1](document)
    assert api.handle.await_count == int(eligible)
    assert compact.handle.await_count == int(not eligible)
    document.fallback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("handled", [True, False])
async def test_handled_api_success_or_refusal_never_starts_another_transport(handled):
    region = get_region("FR")
    page = Page(region.catalog_url("Nike", 1, True))
    page.main_frame = object()
    api = SimpleNamespace(can_handle=Mock(return_value=True), handle=AsyncMock(return_value=handled))
    compact = SimpleNamespace(handle=AsyncMock())
    gate = DocumentGate(api=api, compact=compact, context=object(), url=page.url, region=region)
    await gate.attach(page)
    document = route(page.url, "document")
    document.request.frame = page.main_frame
    await page.route.call_args.args[1](document)
    document.fallback.assert_not_awaited()
    compact.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_stream_does_not_release_scripts_at_eight_seconds_and_keeps_fallback_reserve(monkeypatch):
    import catalog_document

    gate = DocumentGate(remaining=lambda: 20000)
    clock = [100.0]
    with monkeypatch.context() as scoped:
        scoped.setattr(catalog_document, "asyncio", SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock[0])))
        assert gate.wait_seconds() == 18
        clock[0] += 9
        assert gate.wait_seconds() == 9 and not gate.ready.is_set()
        clock[0] += 9
        assert gate.wait_seconds() == 0
    gate.release()
    assert gate.ready.is_set() and not gate.accepted


def fetched_gate(body, status=200, headers=None):
    url='https://www.vinted.fr/catalog?search_text=Nike&order=newest_first'
    page=Page(url)
    page.main_frame=object()
    doc=route(url,'document'); doc.request.frame=page.main_frame
    response=SimpleNamespace(status=status,headers=headers or {'content-type':'text/html','content-encoding':'gzip','content-length':'999'},
                             body=AsyncMock(return_value=body),dispose=AsyncMock())
    doc.fetch=AsyncMock(return_value=response); doc.fulfill=AsyncMock()
    gate=DocumentGate(url=url,region=get_region('FR'),remaining=lambda:19000,fetch_document=True)
    return gate,page,doc,response


@pytest.mark.asyncio
async def test_full_original_html_is_returned_without_scheduling_its_application():
    body=catalog().encode()
    gate,page,doc,response=fetched_gate(body)
    await gate.attach(page)
    await page.route.call_args.args[1](doc)
    assert gate.accepted and gate.ready.is_set()
    assert await gate.read(None,page.url,get_region('FR'))==body.decode()
    doc.fetch.assert_awaited_once_with(max_redirects=0,max_retries=0,timeout=19000)
    doc.fallback.assert_not_awaited(); doc.abort.assert_not_awaited()
    browser=doc.fulfill.call_args.kwargs
    assert browser['status']==200 and b'<script' not in browser['body']
    assert b'product-item' not in browser['body']
    assert 'content-encoding' not in browser['headers'] and 'content-length' not in browser['headers']
    response.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[200,301,403,429,503])
async def test_unknown_document_or_refusal_keeps_the_one_original_response(status):
    body=b'<html><title>Unknown or challenge</title></html>'
    headers={'content-type':'text/html','retry-after':'60','location':'/catalog','content-encoding':'gzip'}
    gate,page,doc,response=fetched_gate(body,status,headers)
    await gate.attach(page); await page.route.call_args.args[1](doc)
    assert gate.document is None and not gate.accepted and gate.ready.is_set()
    doc.fetch.assert_awaited_once(); doc.fallback.assert_not_awaited(); doc.abort.assert_not_awaited()
    assert doc.fulfill.call_args.kwargs=={'status':status,'body':body,'headers':{'content-type':'text/html','retry-after':'60','location':'/catalog'}}
    response.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_original_get_is_never_repeated():
    gate,page,doc,response=fetched_gate(b'')
    doc.fetch.side_effect=TimeoutError()
    await gate.attach(page); await page.route.call_args.args[1](doc)
    assert isinstance(gate.document_error,TimeoutError)
    assert not gate.accepted and gate.ready.is_set()
    doc.fetch.assert_awaited_once(); doc.abort.assert_awaited_once()
    doc.fulfill.assert_not_awaited(); doc.fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_encoding_keeps_original_document_in_same_navigation():
    body=b'<html>\xff</html>'
    gate,page,doc,response=fetched_gate(body)
    await gate.attach(page); await page.route.call_args.args[1](doc)
    assert gate.document is None and not gate.accepted
    assert doc.fulfill.call_args.kwargs['body']==body
    doc.fetch.assert_awaited_once(); doc.abort.assert_not_awaited()


def projected_gate(transport, code="FR", brand="Adidas", page_number=1):
    from catalog_api import CatalogAPI
    from catalog_rsc import CompactCatalog
    from test_catalog_api import Context, data
    from test_catalog_rsc import flight, html_stream

    region = get_region(code)
    url = region.catalog_url(brand, page_number, True)
    context = Context()
    if transport == "api":
        client = CatalogAPI()
        client.sessions[context] = {code: (96, "en-GB" if code == "UK" else code.lower() + "-" + code)}
        body, content_type = json.dumps(data(code, page_number)), "application/json"
    else:
        client = CompactCatalog()
        body, _ = flight(code, brand, page_number)
        client.learn(html_stream(body), region)
        content_type = "text/x-component"
    browser = Page(url)
    browser.main_frame = object()
    document = route(url, "document")
    document.request.frame = browser.main_frame
    document.request.headers = {"user-agent": "browser"}
    reply = SimpleNamespace(status=200, headers={"content-type": content_type},
                            body=AsyncMock(return_value=body.encode()), dispose=AsyncMock())
    document.fetch = AsyncMock(return_value=reply)
    document.fulfill = AsyncMock()
    gate = DocumentGate(api=client if transport == "api" else None,
                        compact=client if transport == "compact" else None,
                        context=context, url=url, region=region, remaining=lambda: 9000)
    return gate, browser, document, reply


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["api", "compact"])
@pytest.mark.parametrize("code,brand", [("FR", "Nike"), ("DE", "Gucci"),
                                       ("IT", "Adidas"), ("UK", "Amiri")])
@pytest.mark.parametrize("page_number", [1, 2])
async def test_verified_projection_returns_all_data_without_browser_subresources(
    transport, code, brand, page_number
):
    from catalog_document import INERT_DOCUMENT

    gate, browser, document, reply = projected_gate(transport, code, brand, page_number)
    await gate.attach(browser)
    await browser.route.call_args.args[1](document)
    source = await gate.read(None, browser.url, get_region(code))
    assert gate.accepted and gate.ready.is_set()
    assert complete_catalog(source, browser.url, get_region(code))
    assert 'product-item-id-123--overlay-link' in source
    assert 'catalog-pagination--next-page' in source and f"page={page_number + 1}" in source
    if transport == "api":
        assert 'product-item-id-456--overlay-link' in source
    assert '<img' in source and '<script' in source
    assert document.fulfill.call_args.kwargs['body'] == INERT_DOCUMENT
    document.fetch.assert_awaited_once()
    document.fallback.assert_not_awaited()
    reply.dispose.assert_awaited_once()
    resource = route()
    await browser.route.call_args.args[1](resource)
    resource.abort.assert_awaited_once()
    resource.fallback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["api", "compact"])
@pytest.mark.parametrize("status", [200, 401, 403, 429, 503])
async def test_refusal_or_challenge_never_uses_inert_projection(transport, status):
    gate, browser, document, reply = projected_gate(transport)
    reply.status = status
    reply.headers = {"content-type": "text/html", "retry-after": "90"}
    reply.body.return_value = b'<html>original challenge or refusal</html>'
    await gate.attach(browser)
    await browser.route.call_args.args[1](document)
    assert gate.document is None and not gate.accepted
    assert document.fulfill.call_args.kwargs == {
        "status": status, "headers": reply.headers, "body": reply.body.return_value
    }
    document.fetch.assert_awaited_once()
    document.fallback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["api", "compact"])
async def test_independent_completeness_rejection_keeps_original_fallback(transport):
    gate, browser, document, reply = projected_gate(transport)
    gate.validator = Mock(return_value=False)
    await gate.attach(browser)
    await browser.route.call_args.args[1](document)
    assert gate.document is None and not gate.accepted
    gate.validator.assert_called_once()
    document.fulfill.assert_not_awaited()
    document.fallback.assert_awaited_once()
    document.fetch.assert_awaited_once()
