"""Small public catalog JSON, in the same anonymous regional browser session.

The site's public ApiGatewayClient and catalog client use this GET endpoint.
No login/token refresh, cookie copying, inventory cache or retry on refusal.
"""
import json
import logging
import re
import time
from collections import OrderedDict
from urllib.parse import parse_qs, urlencode, urlsplit
from weakref import WeakKeyDictionary

from catalog_document import INERT_DOCUMENT, MAX_DOCUMENT_BYTES, catalog_states
from catalog_rsc import state_html
from vinted import is_region_url


def api_target(url, region, per_page):
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    params = parse_qs(parts.query, keep_blank_values=True)
    if (not is_region_url(url, region) or parts.path != "/catalog" or parts.fragment
            or set(params) - {"search_text", "page", "order"}
            or any(len(values) != 1 for values in params.values())
            or params.get("order", [""])[0] != "newest_first"):
        return None  # Never drop an unrecognized filter or change default ordering.
    page = params.get("page", ["1"])[0]
    if not re.fullmatch(r"[1-9][0-9]{0,5}", page):
        return None
    values = {key: entries[0] for key, entries in params.items()}
    values.update(page=page, per_page=per_page)
    return "https://api." + region.domain + "/svc-catalogue/items?" + urlencode(values)


def api_html(data, url, region, per_page):
    if (not isinstance(data, dict) or "errors" in data or "error" in data
            or data.get("code") not in (None, 0)
            or data.get("replica_warning") not in (None, False)
            or not isinstance(data.get("items"), list)
            or not isinstance(data.get("pagination"), dict)
            or data["pagination"].get("per_page") != per_page):
        raise ValueError("Unverified catalog API response")
    items = []
    for item in data["items"]:
        # The site's own DTO mapping explicitly permits a missing/null photo.
        # Keep that card and its identity; downstream gallery loading remains
        # responsible for actual media. Never fabricate a placeholder URL.
        photo, box, price = item.get("photo"), item["item_box"], item["price"]
        thumbnails = [t["url"] for t in photo.get("thumbnails", []) if t["type"] == "thumb310x430"] if photo is not None else []
        if len(thumbnails) > 1:
            raise ValueError("Ambiguous catalog thumbnail")
        items.append({"id": item["id"], "productItem": {
            "id": item["id"], "url": item["url"],
            "price": {"amount": price["amount"], "currencyCode": price["currency_code"]},
            "priceWithDiscount": item.get("discount"),
            "thumbnailUrl": (thumbnails[0] if thumbnails else photo["url"]) if photo is not None else None,
            "itemBox": {"itemId": box.get("item_id", item["id"]), "accessibilityLabel": box["accessibility_label"]}}})
    return state_html({"items": items, "pagination": data["pagination"],
                       "uiState": "SUCCESS", "error": None, "replicaWarning": None}, url, region)


class CatalogAPI:
    def __init__(self, clock=time.monotonic):
        self.sessions = WeakKeyDictionary()
        self.disabled_until = OrderedDict()
        self.clock = clock

    def ready(self, context, region):
        return region.code in self.sessions.get(context, {})

    def can_handle(self, url, region, context):
        config = self.sessions.get(context, {}).get(region.code)
        return bool(config and max(self.disabled_until.get(region.code, 0),
                                   self.disabled_until.get(url, 0)) <= self.clock()
                    and api_target(url, region, config[0]))

    def learn(self, body, context, region):
        try:
            states = list(catalog_states(body))
            locale = re.search(r'<meta\s+name="iso-locale"\s+content="([a-z]{2}-[A-Z]{2})"', body)
            if len(states) != 1 or not locale:
                logging.getLogger("uvicorn.error").info(
                    "Catalog API bootstrap region=%s accepted=false reason=%s", region.code,
                    "state_count" if len(states) != 1 else "missing_locale")
                return
            per_page = states[0]["pagination"]["per_page"]
            if type(per_page) is int and 1 <= per_page <= 100:
                self.sessions.setdefault(context, {})[region.code] = (per_page, locale[1])
                logging.getLogger("uvicorn.error").info("Catalog API bootstrap region=%s accepted=true", region.code)
        except (ValueError, TypeError, KeyError):
            logging.getLogger("uvicorn.error").info("Catalog API bootstrap region=%s accepted=false reason=schema", region.code)
            return

    async def handle(self, route, url, region, remaining, context, *, accept_document=None):
        if not self.can_handle(url, region, context):
            return None
        config = self.sessions[context][region.code]
        per_page, locale = config
        target = api_target(url, region, per_page)
        if target is None:
            return None
        # Let the same request context supply the destination's cookies.
        headers = {key: value for key, value in route.request.headers.items()
                   if key.lower() not in {"cookie", "host", "authorization", "content-length",
                                           "rsc", "next-router-state-tree", "upgrade-insecure-requests"}}
        headers.update({"accept": "application/json", "accept-encoding": "br, gzip",
                        "platform": "web", "x-next-app": "marketplace-web", "locale": locale,
                        "origin": region.base_url, "referer": url,
                        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-site"})
        response = None
        stage = "fetch"
        logging.getLogger("uvicorn.error").info("Catalog API request region=%s", region.code)
        try:
            response = await route.fetch(url=target, headers=headers, max_redirects=0,
                                         max_retries=0, timeout=max(1, min(8000, remaining())))
            if response.status in (401, 403, 429) or response.status >= 500:
                await route.fulfill(status=response.status, body=await response.body(), headers={
                    k: v for k, v in response.headers.items()
                    if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}})
                return False
            if response.status == 200 and "text/html" in response.headers.get("content-type", ""):
                # Keep a challenge in this navigation, without a second request.
                await route.fulfill(status=200, body=await response.body(), headers={
                    k: v for k, v in response.headers.items()
                    if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}})
                return False
            if response.status != 200 or "application/json" not in response.headers.get("content-type", ""):
                raise ValueError("Unsupported catalog API")
            body = await response.body()
            if len(body) > MAX_DOCUMENT_BYTES:
                raise ValueError("Oversized catalog API")
            stage = "validate"
            html = api_html(json.loads(body), url, region, per_page)
            if accept_document is not None and not accept_document(html):
                raise ValueError('Unverified projected catalog')
            await route.fulfill(status=200, body=INERT_DOCUMENT if accept_document is not None else html, headers={
                "content-type": "text/html; charset=utf-8", "cache-control": "no-store"})
            return True
        except Exception as exc:
            reason = (str(exc) if type(exc) is ValueError else
                      "MissingField:" + str(exc.args[0]) if type(exc) is KeyError and exc.args
                      and re.fullmatch(r"[a-z_]{1,50}", str(exc.args[0])) else type(exc).__name__)
            key = region.code if reason == "Unsupported catalog API" else url
            self.disabled_until[key] = self.clock() + 300
            self.disabled_until.move_to_end(key)
            while len(self.disabled_until) > 256:
                self.disabled_until.popitem(last=False)
            logging.getLogger("uvicorn.error").info("Catalog API fallback region=%s stage=%s reason=%s", region.code, stage, reason)
            return None
        finally:
            if response is not None:
                await response.dispose()
