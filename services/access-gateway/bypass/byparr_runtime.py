"""Byparr 3.0.4 entry point: return verified Vinted pages when ready.

Mounted read-only by Compose. Other URLs and incomplete/challenge pages keep
Byparr's ordinary settling path. Anonymous catalog sessions are scoped by region;
explicit proxy overrides and other requests retain isolated contexts.
"""

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

from vinted import CatalogResult, classify, is_region_url, retry_after_seconds
from vinted_regions import REGIONS

REQUEST_TIMINGS = ContextVar("vinted_request_timings", default=None)
REQUEST_RETRY_AFTER = ContextVar("vinted_request_retry_after", default=None)
REQUEST_STATUS = ContextVar("vinted_request_status", default=None)
REQUEST_CATALOG_TRANSPORT = ContextVar("vinted_catalog_transport", default=None)


@dataclass
class BrowserSession:
    # Keep the active page visible to response extraction after local recovery.
    page: object
    context: object


async def browser_request_budget(request):
    """GET /health has no JSON body; malformed POST bodies remain endpoint validation."""
    if request.method != "POST":
        return 60000
    try:
        payload = await request.json()
    except ValueError:
        return 60000
    budget = payload.get("maxTimeout", payload.get("max_timeout", 60000)) if isinstance(payload, dict) else 60000
    if type(budget) is not int:
        return 60000
    # Match the pinned Byparr schema: small values are seconds, larger values
    # milliseconds. Otherwise its default 60 seconds became a 1-second deadline.
    milliseconds = budget * 1000 if budget < 1000 else (budget // 1000) * 1000
    return min(300000, max(1000, milliseconds))


async def navigate_new_tab(dep, request, endpoints, timer, *, wait_until="domcontentloaded", prepare=None):
    """Retry one local initial-tab race, never an HTTP refusal or challenge."""
    url = request.url
    for attempt in range(2):
        page = dep.page
        try:
            if prepare is not None:
                await prepare(page)
            return await page.goto(url, wait_until=wait_until,
                                   timeout=endpoints.remaining_ms(timer))
        except Exception as exc:
            message = str(exc)
            initial_navigation = (
                ("loadURI" in message and "browsingContext is undefined" in message)
                or ("is interrupted by another navigation to" in message
                    and any(f'"{target}"' in message for target in ("about:blank", "about:newtab")))
            )
            if (attempt or not initial_navigation or page.url not in (url, "about:blank", "about:newtab")
                    or endpoints.remaining_ms(timer) < 1000):
                raise
            if isinstance(dep, BrowserSession):
                # Firefox can lose the browsingContext permanently. Retrying
                # goto on that same object cannot repair it. Replace only the
                # tab, retaining context/proxy/cookies and the original deadline.
                try:
                    await asyncio.wait_for(page.close(), timeout=1)
                except Exception:
                    pass  # Dependency cleanup still closes the entire context.
                dep.page = await dep.context.new_page()
                await endpoints.setup_routes(request, dep)
                logging.getLogger("uvicorn.error").info("Replaced failed initial browser tab once")
            else:
                # Compatibility with standalone Byparr diagnostics.
                logging.getLogger("uvicorn.error").info("Retrying initial browser-tab navigation once")
                await asyncio.sleep(0.25)


def navigation_timings(entry):
    """Main-document request only; never confuse missing browser data with zero."""
    if not isinstance(entry, dict):
        return {}
    values = [entry.get(key) for key in ("requestStart", "responseStart", "responseEnd")]
    if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
        return {}
    start, first, end = values
    if not 0 < start <= first <= end:
        return {}
    return {"browser_document_request_ms": end - start, "browser_ttfb_ms": first - start}


def install_navigation_timing(endpoints):
    navigate = endpoints._navigate_and_solve

    async def measured(dep, request, timer):
        timings = REQUEST_TIMINGS.get()
        started = time.monotonic()
        try:
            # Some browser operations (content/challenge checks) have no native
            # timeout. The source request budget must cover the whole navigation.
            async with asyncio.timeout(max(0.001, endpoints.remaining_ms(timer) / 1000)):
                result = await navigate(dep, request, timer)
        finally:
            if timings is not None:
                timings["browser_navigation_ms"] = (time.monotonic() - started) * 1000
        region = next((r for r in REGIONS.values() if is_region_url(request.url, r)), None)
        status = REQUEST_STATUS.get()
        if region and status is not None and result[2] is not None:
            code = getattr(result[2], "status", None)
            # Byparr's response model hardcodes 200. Preserve an actual refusal
            # so downstream cooldowns do not mistake it for an ordinary page.
            if type(code) is int and 400 <= code <= 599:
                status["code"] = code
        retry = REQUEST_RETRY_AFTER.get()
        if region and retry is not None and result[2] is not None and result[2].status in (429, 503):
            try:
                value = await asyncio.wait_for(result[2].header_value("retry-after"), timeout=1)
                if delay := retry_after_seconds(value):
                    retry["seconds"] = delay
            except Exception:
                pass
        if timings is not None and region and not result[0] and is_region_url(dep.page.url, region):
            try:
                # Numeric data only. The read adds a small diagnostic round trip,
                # included in Byparr HTTP time, outside browser_navigation_ms.
                entry = await asyncio.wait_for(dep.page.evaluate("""() => {
                    const p = performance.getEntriesByType('navigation')[0];
                    return p ? {requestStart: p.requestStart, responseStart: p.responseStart,
                                responseEnd: p.responseEnd} : null;
                }"""), timeout=1)
                timings.update(navigation_timings(entry))
            except Exception:
                pass
        return result

    endpoints._navigate_and_solve = measured


def install_catalog_readiness(endpoints, *, document_catalog=False, compact_catalog=False, api_catalog=False):
    original = endpoints._navigate_and_solve
    compact = None
    api = None
    if document_catalog and api_catalog:
        from catalog_api import CatalogAPI
        api = CatalogAPI()
    if document_catalog and compact_catalog:
        from catalog_rsc import CompactCatalog
        compact = CompactCatalog()

    async def navigate(dep, request, timer):
        region = next((region for region in REGIONS.values()
                       if is_region_url(request.url, region)), None)
        path = urlsplit(request.url).path.rstrip("/")
        item = re.fullmatch(r"/items/([1-9][0-9]{0,19})(?:-[^/]*)?", path)
        if region is None or (path != "/catalog" and item is None):
            return await original(dep, request, timer)

        if (document_catalog and getattr(request, "block_media", False)
                and not getattr(request, "return_only_cookies", False)):
            # Bootstrap once with ordinary HTML in this context. A cold API
            # request would be refused and must never trigger identity retries.
            from catalog_api import api_target
            from catalog_document import DocumentGate
            eligible_api = api if item is None and api is not None and api_target(request.url, region, 96) else None
            gate = DocumentGate(compact=compact if item is None and (not eligible_api or api.ready(dep.context, region)) else None,
                                api=eligible_api, context=dep.context, url=request.url, region=region,
                                fetch_document=True,
                                remaining=lambda: endpoints.remaining_ms(timer),
                                validator=(lambda body, url, source_region:
                                           complete_item_document(body, url, source_region, item[1]))
                                if item else None)
            raw = None
            try:
                response = await navigate_new_tab(dep, request, endpoints, timer,
                                                  wait_until="commit", prepare=gate.attach)
                raw = await gate.read(response, request.url, region)
            except Exception:
                if gate.document_error is not None:
                    raise gate.document_error
                raise
            finally:
                gate.release(accepted=raw is not None)
            transport = REQUEST_CATALOG_TRANSPORT.get()
            if transport is not None:
                transport["mode"] = ("items" if gate.used_api else "compact" if gate.used_compact else "document") if raw is not None else "rendered-fallback"
            if raw is not None:
                if item is None and api is not None and not gate.used_api and not gate.used_compact:
                    api.learn(raw, dep.context, region)
                if item is None and compact is not None and not gate.used_compact and not gate.used_api:
                    compact.learn(raw, region)
                return False, raw, response
            # Continue the same navigation; never spend a second catalog request.
            await dep.page.wait_for_load_state("domcontentloaded", timeout=endpoints.remaining_ms(timer))
        else:
            response = await navigate_new_tab(dep, request, endpoints, timer)
        if response is not None and 400 <= response.status <= 599:
            return False, await dep.page.content(), response
        if await endpoints.challenge_present(dep.page):
            await endpoints.solve_challenge(dep.page, timer)
            await endpoints._wait_for_networkidle(dep, timer)
            return True, None, response

        body = await dep.page.content()
        status = response.status if response is not None else 0
        item_id = item[1] if item else None
        result = classify(CatalogResult(region.code, request.url), region, dep.page.url, status, body,
                          item_id=item_id)
        # A single catalog link proves neither a complete identity inventory nor
        # finished streaming. Checkpoint traversal must see the settled catalog.
        # The item shortcut has separate gallery/description/review evidence.
        if result.outcome == "page_loaded" and item_id is not None and item_data_ready(body, item_id):
            return False, body, response

        await dep.page.wait_for_load_state("load", timeout=endpoints.remaining_ms(timer))
        if await endpoints.challenge_present(dep.page):
            await endpoints.solve_challenge(dep.page, timer)
            await endpoints._wait_for_networkidle(dep, timer)
            return True, None, response
        if item_id is not None:
            # Metadata can finish between DOMContentLoaded and load. Use exactly
            # the same proof as above before waiting on unrelated network activity.
            body = await dep.page.content()
            result = classify(CatalogResult(region.code, request.url), region, dep.page.url,
                              status, body, item_id=item_id)
            if result.outcome == "page_loaded" and item_data_ready(body, item_id):
                return False, body, response
        await endpoints._wait_for_networkidle(dep, timer)
        # Refresh after settling; Byparr's original path captures HTML before it.
        return False, await dep.page.content(), response

    endpoints._navigate_and_solve = navigate


def complete_item_document(body, url, region, item_id):
    """Use a finished public item document only when all existing details are ready."""
    if not isinstance(body, str) or not body.rstrip().lower().endswith("</html>"):
        return False
    result = classify(CatalogResult(region.code, url), region, url, 200, body, item_id=item_id)
    return result.outcome == "page_loaded" and item_data_ready(body, item_id)


def item_data_ready(body, item_id):
    """Require gallery, description, reviews and publication evidence before returning early.

    Read JSON only, never execute embedded scripts. A visible cover alone is not
    enough: streaming page data may still be incomplete. Unknown layouts retain
    the normal settling path and downstream parser validation.
    """
    chunks = []
    for script in re.findall(r"<script\b[^>]*>(.*?)</script\s*>", body, re.S | re.I):
        script = script.strip().removesuffix(";")
        prefix = "self.__next_f.push("
        if not script.startswith(prefix) or not script.endswith(")"):
            continue
        try:
            chunk = json.loads(script[len(prefix):-1])
        except (ValueError, RecursionError):
            continue
        if isinstance(chunk, list) and len(chunk) == 2 and chunk[0] == 1 and isinstance(chunk[1], str):
            chunks.append(chunk[1])
    gallery = description = reviews = False
    publication = bool(re.search(r'data-testid=["\']item-attributes-upload_date["\']', body))
    for line in "".join(chunks).splitlines():
        try:
            pending = [json.loads(line.partition(":")[2])]
        except (ValueError, RecursionError):
            continue
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                pending.extend(node.values())
                if str(node.get("item_id", node.get("id", ""))) != item_id:
                    continue
                publication |= any(node.get(field) is not None for field in (
                    "created_at", "created_at_ts", "published_at", "datePublished"))
                photos = node.get("photos")
                if isinstance(photos, list) and photos and all(
                    isinstance(p, dict) and isinstance(p.get("url"), str) and p["url"]
                    for p in photos
                ):
                    gallery = True
                description |= isinstance(node.get("description"), str)
                reviews |= ("seller_id" in node and type(node.get("feedback_count")) is int
                            and "feedback_reputation" in node)
    return gallery and description and reviews and publication


def install_local_health(app, response_model, *, ready=lambda: True):
    """Report service readiness without launching a browser or resolving proxy geo.

    Browser operations enforce their own deadlines. A periodic liveness probe
    must not compete with them for contexts or advance browser rotation.
    """
    async def local_health():
        from starlette.exceptions import HTTPException
        if not ready():
            raise HTTPException(status_code=503, detail="Browser service is shutting down")
        return response_model(user_agent="")

    for route in app.routes:
        if getattr(route, "path", None) == "/health" and "GET" in route.methods:
            # Rebuild the route so neither its signature nor its dependency graph
            # retains the original get_browser dependency (including lazy routers).
            app.routes.remove(route)
            app.add_api_route("/health", local_health, methods=["GET"],
                              response_model=response_model, name=route.name)
            return
    raise RuntimeError("Byparr health route was not found")


def install_response_metadata(app, *, reuse, enabled, static_cache=None, skip_analytics=False,
                              document_catalog=False, compact_catalog=False, api_catalog=False):
    from starlette.responses import JSONResponse

    @app.middleware("http")
    async def report_browser_mode(request, call_next):
        timings = {} if request.headers.get("X-Vinted-Measure-Timing") == "1" else None
        token = REQUEST_TIMINGS.set(timings)
        retry = {}
        retry_token = REQUEST_RETRY_AFTER.set(retry)
        status = {}
        status_token = REQUEST_STATUS.set(status)
        transport = {}
        transport_token = REQUEST_CATALOG_TRANSPORT.set(transport)
        try:
            try:
                response = await call_next(request)
            except TimeoutError:
                # The dependency deadline also covers context creation and HTML
                # extraction. It is a timeout, not an unhandled HTTP 500.
                response = JSONResponse(
                    {"detail": "Browser operation exceeded its request time budget"},
                    status_code=408,
                )
            response.headers["X-Vinted-Catalog-Mode"] = "ready-content" if enabled else "standard"
            response.headers["X-Vinted-Catalog-Transport"] = transport.get("mode", "rendered")
            response.headers["X-Vinted-Document-Catalog"] = "enabled" if document_catalog else "disabled"
            response.headers["X-Vinted-Compact-Catalog"] = "enabled" if compact_catalog else "disabled"
            response.headers["X-Vinted-Items-Catalog"] = "enabled" if api_catalog else "disabled"
            shared = reuse and not any(name in request.headers for name in
                                      ("X-Proxy-Server", "X-Proxy-Username", "X-Proxy-Password"))
            response.headers["X-Vinted-Browser-Lifecycle"] = "reused" if shared else "per-request"
            response.headers["X-Vinted-Static-Cache"] = "enabled" if static_cache is not None else "disabled"
            response.headers["X-Vinted-Skip-Analytics"] = "enabled" if skip_analytics else "disabled"
            if static_cache is not None:
                response.headers["X-Vinted-Static-Cache-Hits"] = str(static_cache.hits)
                response.headers["X-Vinted-Static-Cache-Disk"] = "enabled" if static_cache.disk else "disabled"
                response.headers["X-Vinted-Static-Cache-Restored"] = str(static_cache.stats["disk_loaded"])
                response.headers["X-Vinted-Static-Cache-Coalesced"] = str(static_cache.stats["coalesced"])
            if timings is not None:
                response.headers["X-Vinted-Timings"] = json.dumps(timings, separators=(",", ":"))
            if retry.get("seconds"):
                response.headers["X-Vinted-Retry-After"] = str(retry["seconds"])
            if status.get("code"):
                response.headers["X-Vinted-Upstream-Status"] = str(status["code"])
            return response
        finally:
            REQUEST_TIMINGS.reset(token)
            REQUEST_RETRY_AFTER.reset(retry_token)
            REQUEST_STATUS.reset(status_token)
            REQUEST_CATALOG_TRANSPORT.reset(transport_token)


def main():
    # Imports below exist inside the pinned Byparr image, not in the local CLI.
    sys.path.insert(0, "/app")
    import uvicorn
    from fastapi import Request
    from src import endpoints, utils
    from src.consts import HOST, PORT
    from src.models import HealthcheckResponse

    from proxy_budget import configured_budget
    proxy_budget = configured_budget()
    from browser_network import bind_network_lifespan, install_browser_network_policy
    network_policy = install_browser_network_policy(utils, budget=proxy_budget)

    # Patch the source router before main includes it. The pinned FastAPI uses
    # lazy _IncludedRouter entries; app.routes does not expose /health directly.
    pool = None
    install_local_health(endpoints.router, HealthcheckResponse,
                         ready=lambda: pool is None or not pool.closed)
    from main import app
    skip_analytics = os.environ.get("VINTED_SKIP_ANALYTICS", "1") == "1"
    from traffic_policy import install_traffic_policy
    install_traffic_policy(endpoints, skip_analytics=skip_analytics)
    static_cache = None
    if os.environ.get("VINTED_STATIC_CACHE", "1") == "1":
        from static_cache import StaticAssetCache, install_static_cache
        from direct_assets import DirectAssetFetcher
        static_cache = StaticAssetCache(disk_dir=os.environ.get("VINTED_STATIC_CACHE_DIR") or None,
                                       direct_fetcher=DirectAssetFetcher())
        install_static_cache(endpoints, static_cache)

    # Reuse only the fixed environment proxy. Requests with explicit proxy
    # overrides retain Byparr's original per-request browser lifecycle.
    reuse = os.environ.get("VINTED_REUSE_BROWSER", "1") == "1"
    pool = None
    if reuse:
        from browser_pool import SharedBrowser, bind_browser_lifespan

        proxy = None
        if utils.PROXY_SERVER:
            proxy = {"server": utils.PROXY_SERVER, "username": utils.PROXY_USERNAME,
                     "password": utils.PROXY_PASSWORD}
        # Verified documents do not execute the site's application. Keep their
        # four bounded regional contexts longer, avoiding a full HTML bootstrap
        # every 50 small requests. Page cleanup and the two-generation cap stay
        # enforced, including abandoned detail requests.
        pool = SharedBrowser(lambda: utils.InvisiblePlaywright(
            headless=True, proxy=proxy, humanize=True, locale=utils.BROWSER_LOCALE or "auto",
            extra_prefs={"devtools.jsonview.enabled": False,
                         "browser.tabs.remote.useCrossOriginOpenerPolicy": False,
                         "browser.tabs.remote.useCrossOriginEmbedderPolicy": False},
        ), max_uses=200)

        bind_browser_lifespan(app, pool)

    api_catalog = (reuse and os.environ.get("VINTED_FAST_CATALOGS", "1") == "1"
                   and os.environ.get("VINTED_DOCUMENT_CATALOG", "1") == "1"
                   and os.environ.get("VINTED_ITEMS_CATALOG", "1") == "1")

    async def pooled_browser(request: Request):
        budget = await browser_request_budget(request)
        # Bound both lifecycles, including creation, extraction and tab recovery.
        async with asyncio.timeout(budget / 1000 + 5):
            if pool is None or any(name in request.headers for name in
                                   ("X-Proxy-Server", "X-Proxy-Username", "X-Proxy-Password")):
                async with aclosing(utils.get_browser(
                    x_proxy_server=request.headers.get("X-Proxy-Server"),
                    x_proxy_username=request.headers.get("X-Proxy-Username"),
                    x_proxy_password=request.headers.get("X-Proxy-Password"),
                )) as browser_dependency:
                    async for dep in browser_dependency:
                        yield BrowserSession(dep.page, dep.context)
                return
            session_key = None
            if api_catalog:
                try:
                    payload = await request.json() if request.method == "POST" else {}
                except ValueError:
                    payload = {}
                if (isinstance(payload, dict) and payload.get("cmd") == "request.get" and payload.get("blockMedia") is True
                        and payload.get("returnOnlyCookies") is False):
                    from catalog_api import api_target
                    for region in REGIONS.values():
                        if api_target(payload.get("url", ""), region, 96):
                            session_key = region.code
                            break
            # One request deadline; pool cleanup survives its cancellation.
            if session_key is not None:
                logging.getLogger("uvicorn.error").info("Catalog browser session region=%s pooled=true", session_key)
            async with pool.page(key=session_key) as session:
                dep = BrowserSession(session.page, session.context)
                try:
                    yield dep
                finally:
                    session.page = dep.page

    app.dependency_overrides[utils.get_browser] = pooled_browser

    enabled = os.environ.get("VINTED_FAST_CATALOGS", "1") == "1"
    document_catalog = enabled and os.environ.get("VINTED_DOCUMENT_CATALOG", "1") == "1"
    compact_catalog = document_catalog and os.environ.get("VINTED_COMPACT_CATALOG", "1") == "1"
    if enabled:
        install_catalog_readiness(endpoints,
                                  document_catalog=document_catalog, compact_catalog=compact_catalog,
                                  api_catalog=api_catalog)
    install_navigation_timing(endpoints)

    install_response_metadata(app, reuse=reuse, enabled=enabled, static_cache=static_cache,
                              skip_analytics=skip_analytics, document_catalog=document_catalog,
                              compact_catalog=compact_catalog, api_catalog=api_catalog)

    from http_cancellation import CancelOnDisconnect
    app.add_middleware(CancelOnDisconnect)

    if proxy_budget is not None:
        from budget_http import install_budget_http
        install_budget_http(app, proxy_budget)

    bind_network_lifespan(app, network_policy)

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
