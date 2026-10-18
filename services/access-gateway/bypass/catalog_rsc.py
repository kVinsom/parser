"""Public Next.js catalog navigation, with a bounded, learned routing cache.

Only public routing structure/build/cache-busting keys are cached, never items,
sessions or cookies. Each inventory is freshly fetched in the caller's context
and proxy. A refusal is returned as-is; unsupported data falls back to HTML.
"""
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from catalog_document import INERT_DOCUMENT, MAX_DOCUMENT_BYTES, flight_objects, flight_text
from vinted import catalog_context, is_region_url


def root_payload(stream):
    roots = [json.loads(line[2:]) for line in stream.splitlines() if line.startswith("0:")]
    if len(roots) != 1 or not isinstance(roots[0], dict):
        raise ValueError("Missing navigation root")
    return roots[0]


def routing_tree(node, depth=0):
    if depth > 12 or not isinstance(node, list) or len(node) < 2 or not isinstance(node[1], dict):
        raise ValueError("Unknown routing tree")
    segment = node[0]
    if isinstance(segment, str) and segment.startswith("__PAGE__"):
        segment = "__PAGE__"  # Request the fresh page, retain only shared public layouts.
    elif not isinstance(segment, (str, list)):
        raise ValueError("Invalid route segment")
    return [segment, {key: routing_tree(value, depth + 1) for key, value in node[1].items()}]


def successful_state(stream, url, build):
    if not stream.endswith("\n") or re.search(r"(?m)^[0-9a-f]+:E", stream):
        raise ValueError("Incomplete or failed component stream")
    root = root_payload(stream)
    if root.get("b") != build:
        raise ValueError("Navigation build changed")
    if not isinstance(root.get("q"), str):
        raise ValueError("Navigation query unavailable")
    if catalog_context("https://example.test/catalog" + root["q"]) != catalog_context(url):
        raise ValueError("Catalog query mismatch")
    states = [node for node in flight_objects(stream)
              if {"items", "pagination", "uiState", "error", "replicaWarning"} <= node.keys()]
    if len(states) != 1:
        raise ValueError("Ambiguous catalog state")
    state = states[0]
    if (state["uiState"] != "SUCCESS" or state["error"] not in (None, "$undefined")
            or state["replicaWarning"] not in (None, False, "$undefined")):
        raise ValueError("Catalog not successful")
    return state


def catalog_html(stream, url, region, build):
    """Project the verified source fields into the existing parser's DOM contract.

This projection is local, not counted as proxy savings. No item is filtered or
truncated. Unknown, discounted or incomplete cards reject the entire shortcut.
"""
    state = successful_state(stream, url, build)
    return state_html(state, url, region)


def state_html(state, url, region):
    """Shared strict projection for verified public catalog representations."""
    items, pagination = state["items"], state["pagination"]
    page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
    if (not isinstance(pagination, dict) or pagination.get("current_page") != page
            or type(pagination.get("per_page")) is not int
            or not 1 <= pagination["per_page"] <= 100
            or type(pagination.get("total_pages")) is not int
            or pagination["total_pages"] < page
            or not isinstance(items, list) or not items or len(items) > pagination["per_page"]):
        raise ValueError("Unverified catalog pagination or empty state")
    cards, ids = [], set()
    for entry in items:
        product = entry["productItem"]
        identity = str(entry["id"])
        if (not re.fullmatch(r"[1-9][0-9]{0,19}", identity) or identity in ids
                or str(product["id"]) != identity
                or str(product["itemBox"]["itemId"]) != identity):
            raise ValueError("Conflicting item identities")
        ids.add(identity)
        href = urljoin(region.base_url, product["url"])
        if (not is_region_url(href, region)
                or not re.match(r"^/items/" + identity + r"(?:-|$)", urlsplit(href).path)):
            raise ValueError("Invalid item link")
        label, photo = product["itemBox"]["accessibilityLabel"], product["thumbnailUrl"]
        photo_parts = urlsplit(photo) if isinstance(photo, str) else None
        if (not isinstance(label, str) or not label or (photo is not None and
                (photo_parts is None or photo_parts.scheme != "https"
                or not photo_parts.hostname or photo_parts.username or photo_parts.password
                or photo_parts.port not in (None, 443)))):
            raise ValueError("Missing card fields")
        price = product["price"]
        currency = "GBP" if region.code == "UK" else "EUR"
        amount = price["amount"]
        if (product.get("priceWithDiscount") not in (None, "$undefined")
                or price["currencyCode"] != currency or not isinstance(amount, str)
                or not re.fullmatch(r"[0-9]+\.[0-9]{2}", amount)
                or not Decimal(amount).is_finite()):
            raise ValueError("Unverified card price")
        display = "£" + amount if currency == "GBP" else amount.replace(".", ",") + " €"
        prefix = "product-item-id-" + identity
        image = f'<img data-testid="{prefix}--image--img" src="{escape(photo, quote=True)}">' if photo is not None else ''
        cards.append(f'<div data-testid="{prefix}"><a data-testid="{prefix}--overlay-link" '
                     f'href="{escape(href, quote=True)}" title="{escape(label, quote=True)}"></a>'
                     f'<p data-testid="{prefix}--price-text">{display}</p>'
                     + image + '</div>')
    params = parse_qs(urlsplit(url).query, keep_blank_values=True)
    params.pop("_rsc", None)
    params["page"] = [str(page + 1)]
    next_url = region.base_url + "/catalog?" + urlencode(params, doseq=True)
    disabled = "false" if page < pagination["total_pages"] else "true"
    # Preserve exact source inventory for the independent document completeness gate.
    embedded = json.dumps([1, "1:" + json.dumps(state, ensure_ascii=True) + "\n"]).replace("<", "\\u003c")
    return ('<html><head><title>Catalog | Vinted</title></head><body>' + "".join(cards)
            + f'<a data-testid="catalog-pagination--next-page" aria-disabled="{disabled}" '
            + f'href="{escape(next_url, quote=True)}">Next</a>'
            + '<script>self.__next_f=[]</script><script>self.__next_f.push(' + embedded + ')</script></body></html>')


@dataclass
class Routing:
    header: str
    build: str
    expires: float
    cache_key: str | None = None


class CompactCatalog:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.routes = {}
        self.disabled_until = OrderedDict()

    def disable(self, key):
        self.disabled_until[key] = self.clock() + 300
        self.disabled_until.move_to_end(key)
        while len(self.disabled_until) > 256:
            self.disabled_until.popitem(last=False)

    def learn(self, body, region):
        try:
            root = root_payload(flight_text(body))
            header = quote(json.dumps(routing_tree(root["f"][0][0]), separators=(",", ":")), safe="")
            if len(header) > 12000 or not isinstance(root["b"], str) or not 1 <= len(root["b"]) <= 100:
                return
            previous = self.routes.get(region.code)
            key = previous.cache_key if previous and (previous.header, previous.build) == (header, root["b"]) else None
            self.routes[region.code] = Routing(header, root["b"], self.clock() + 1800, key)
            # A verified full document has already taught us the new build.
            # Its previous build's protocol pause must not force five more
            # minutes of full HTML. Query-specific failures remain isolated.
            if previous and previous.build != root["b"]:
                self.disabled_until.pop(region.code, None)
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            return

    async def handle(self, route, url, region, remaining, *, accept_document=None):
        entry = self.routes.get(region.code)
        if (not entry or entry.expires <= self.clock()
                or max(self.disabled_until.get(region.code, 0),
                       self.disabled_until.get(url, 0)) > self.clock()):
            return None
        headers = {**route.request.headers, "rsc": "1", "next-router-state-tree": entry.header,
                   "accept-encoding": "br, gzip"}
        target = url + ("&" if "?" in url else "?") + "_rsc=" + quote(entry.cache_key, safe="") if entry.cache_key else url
        response = None
        try:
            response = await route.fetch(url=target, headers=headers, max_redirects=0, timeout=max(1, remaining()))
            if response.status == 307:
                location = urljoin(url, response.headers.get("location", ""))
                params = parse_qs(urlsplit(location).query, keep_blank_values=True)
                key = params.get("_rsc", [])
                if (not is_region_url(location, region) or urlsplit(location).path != "/catalog"
                        or catalog_context(location) != catalog_context(url)
                        or set(params) - {"search_text", "page", "order", "_rsc"}
                        or len(key) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key[0])):
                    raise ValueError("Unexpected compact redirect")
                entry.cache_key = key[0]
                await response.dispose()
                response = await route.fetch(url=location, headers=headers, max_redirects=0, timeout=max(1, remaining()))
            if response.status in (401, 403, 429) or response.status >= 500:
                body = await response.body()
                await route.fulfill(status=response.status, body=body, headers={
                    k: v for k, v in response.headers.items()
                    if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}})
                return False  # Handled refusal, no HTML retry or changed identity.
            if response.status == 200 and "text/html" in response.headers.get("content-type", ""):
                # A challenge or ordinary HTML response stays in this navigation.
                # Do not repeat the document simply because RSC was not returned.
                self.disable(url)
                await route.fulfill(status=200, body=await response.body(), headers={
                    k: v for k, v in response.headers.items()
                    if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}})
                return False
            if (response.status != 200 or "text/x-component" not in response.headers.get("content-type", "")
                    or response.headers.get("x-nextjs-postponed")):
                raise ValueError("Unsupported compact response")
            body = await response.body()
            if len(body) > MAX_DOCUMENT_BYTES:
                raise ValueError("Oversized compact response")
            html = catalog_html(body.decode("utf-8"), url, region, entry.build)
            if accept_document is not None and not accept_document(html):
                raise ValueError('Unverified projected catalog')
            await route.fulfill(status=200, body=INERT_DOCUMENT if accept_document is not None else html, headers={
                "content-type": "text/html; charset=utf-8", "cache-control": "no-store"})
            return True
        except Exception as exc:
            # Own validation messages contain no URLs, response bodies or cookies.
            reason = str(exc) if type(exc) is ValueError else type(exc).__name__
            shared_failure = reason in {"Navigation build changed", "Unexpected compact redirect",
                                        "Unsupported compact response"}
            # One empty/warning/discounted search must not disable every other
            # user's valid catalog in the region. Transport/build failures do.
            self.disable(region.code if shared_failure else url)
            logging.getLogger("uvicorn.error").info("Compact catalog fallback region=%s reason=%s",
                                                    region.code, reason)
            return None
        finally:
            if response is not None:
                await response.dispose()
