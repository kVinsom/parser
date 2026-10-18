"""Anonymous item_info protocol adapted from Pawikoski/vinted-api-wrapper.

Reference: 96f504cd6e5ad79a9efcac6ec8a00a359db0dc3f, vinted/endpoints.py.
No account credentials, persisted cookies, redirects, or unbounded retries.
"""

import http.cookiejar
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from vinted import retry_after_seconds
from vinted_regions import get_region

MAX_BYTES = 1024 * 1024
COOLDOWN = 300
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,*;q=0.5",
    "X-Requested-With": "XMLHttpRequest",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def anonymous_session():
    return urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


def item_url(region, item_id):
    region = get_region(region)
    if not isinstance(item_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", item_id):
        raise ValueError("Invalid item identity")
    return region.base_url + f"/api/v2/items/{item_id}/details"


def minimal_item(payload, item_id):
    """Keep item metadata and public reputation; never forward session/private data."""
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
        raise ValueError("Unsuccessful item response")
    item = payload.get("item")
    if not isinstance(item, dict) or str(item.get("id")) != item_id:
        raise ValueError("Item identity mismatch")
    photos = item.get("photos")
    fields = ("id", "is_draft", "is_processing", "is_hidden", "is_closed",
              "created_at_ts", "created_at", "published_at", "datePublished")
    clean = {key: item[key] for key in fields if key in item}
    clean["photos"] = [
        {"url": photo.get("url")} if isinstance(photo, dict) else {}
        for photo in (photos if isinstance(photos, list) else [])
    ]
    if isinstance(item.get("description"), str):
        clean["description"] = item["description"]
    if isinstance(item.get("user"), dict):
        clean["user"] = {
            key: item["user"][key]
            for key in ("id", "feedback_count", "feedback_reputation")
            if key in item["user"]
        }
    for key in ("user_id", "seller_id"):
        if key in item:
            clean[key] = item[key]
    return {"code": 0, "item": clean}


class ItemApiClient:
    def __init__(self, session_factory=anonymous_session, clock=time.monotonic):
        self.session_factory = session_factory
        self.clock = clock
        self.sessions = {}
        self.retry_after = {}
        self.locks = {code: threading.Lock() for code in ("UK", "FR", "IT", "DE")}

    def fetch(self, region, item_id, timeout=3):
        url = item_url(region, item_id)
        region = get_region(region)
        with self.locks[region.code]:
            if self.clock() < self.retry_after.get(region.code, 0):
                return None
            deadline = self.clock() + min(timeout, 3)
            reason = "invalid_response"
            try:
                session = self.sessions.get(region.code)
                if session is None:
                    session = self.session_factory()
                    with session.open(urllib.request.Request(region.base_url, headers=HEADERS),
                                      timeout=max(0.01, deadline - self.clock())) as response:
                        if response.status != 200:
                            raise ValueError("Anonymous session unavailable")
                    self.sessions[region.code] = session
                if self.clock() >= deadline:
                    raise TimeoutError
                with session.open(urllib.request.Request(url, headers=HEADERS),
                                  timeout=deadline - self.clock()) as response:
                    if response.status != 200 or "json" not in response.headers.get("Content-Type", ""):
                        raise ValueError("Expected JSON")
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("Response too large")
                payload = minimal_item(json.loads(raw), item_id)
                return json.dumps(payload, ensure_ascii=False).encode("utf-8")
            except urllib.error.HTTPError as exc:
                reason = f"http_{exc.code}"
                # Respect longer Retry-After values, without leaking response contents.
                retry = exc.headers.get("Retry-After", "") if exc.headers else ""
                delay = max(COOLDOWN, retry_after_seconds(retry) or 0)
            except (OSError, ValueError, TypeError, RecursionError):
                delay = COOLDOWN
            self.sessions.pop(region.code, None)
            self.retry_after[region.code] = self.clock() + delay
            logging.getLogger("vpars.item_api").info(
                "item_api region=%s outcome=%s cooldown=%s", region.code, reason, delay
            )
            return None
