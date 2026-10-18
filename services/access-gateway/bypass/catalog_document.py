"""Read a complete server-rendered source page before downloading its application.

Only validated source data is eligible. An unknown shell keeps
loading in the SAME page: no repeated document, changed identity, or retry on
refusal. Access-check resources are never held. Nothing caches live inventory.
"""
import asyncio
import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

from vinted import CatalogResult, classify

MAX_DOCUMENT_BYTES = 12 * 1024 * 1024
INERT_DOCUMENT = b'<html><head><title>Vinted</title></head><body></body></html>'


def flight_text(body):
    chunks = []
    for script in re.findall(r"<script\b[^>]*>(.*?)</script\s*>", body, re.S | re.I):
        script = script.strip().removesuffix(";")
        prefix = "self.__next_f.push("
        if not script.startswith(prefix) or not script.endswith(")"):
            continue
        try:
            value = json.loads(script[len(prefix):-1])
        except (ValueError, RecursionError):
            continue
        if isinstance(value, list) and len(value) == 2 and value[0] == 1 and isinstance(value[1], str):
            chunks.append(value[1])
    return "".join(chunks)


def flight_objects(stream):
    for line in stream.splitlines():
        try:
            nodes = [json.loads(line.partition(":")[2])]
        except (ValueError, RecursionError):
            continue
        while nodes:
            node = nodes.pop()
            if isinstance(node, list):
                nodes.extend(node)
            elif isinstance(node, dict):
                yield node
                nodes.extend(node.values())


def catalog_states(body):
    return (node for node in flight_objects(flight_text(body))
            if {"items", "pagination", "uiState", "error", "replicaWarning"} <= node.keys())


def complete_catalog(body, url, region):
    """Prove stream EOF, successful source state, exact identities and card fields.

Do not infer completeness from a single item or item count. The parser still
validates all fields, filters, identity links and pagination downstream.
"""
    if not isinstance(body, str) or not body.rstrip().lower().endswith("</html>"):
        return False
    if classify(CatalogResult(region.code, url), region, url, 200, body).outcome != "page_loaded":
        return False
    try:
        page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        states = list(catalog_states(body))
        if len(states) != 1:
            return False
        state = states[0]
        if (state["uiState"] != "SUCCESS" or state["error"] not in (None, "$undefined")
                or state["replicaWarning"] not in (None, False, "$undefined")):
            return False
        pagination = state["pagination"]
        if (not isinstance(pagination, dict) or pagination.get("current_page") != page
                or type(pagination.get("per_page")) is not int
                or not 1 <= pagination["per_page"] <= 100):
            return False
        items = state["items"]
        if not isinstance(items, list) or not items or len(items) > pagination["per_page"]:
            return False  # Empty states retain existing settled-page validation.
        ids = [str(item["id"]) for item in items if isinstance(item, dict)]
        if (len(ids) != len(items) or len(set(ids)) != len(ids)
                or any(not re.fullmatch(r"[1-9][0-9]{0,19}", identity) for identity in ids)):
            return False
        markers = re.findall(r'data-testid=["\']product-item-id-([0-9]+)["\']', body)
        if len(markers) != len(ids) or set(markers) != set(ids):
            return False
        for suffix in ("--overlay-link", "--price-text"):
            found = re.findall(r'data-testid=["\']product-item-id-([0-9]+)' + suffix + r'["\']', body)
            if len(found) != len(ids) or set(found) != set(ids):
                return False
        # A verified source DTO may explicitly say this card has no photo.
        # Unknown/missing schema still requires the original image proof.
        without_photos = {str(item["id"]) for item in items
                          if isinstance(item.get("productItem"), dict)
                          and "thumbnailUrl" in item["productItem"]
                          and item["productItem"]["thumbnailUrl"] is None}
        images = re.findall(r'data-testid=["\']product-item-id-([0-9]+)--image--img["\']', body)
        if len(images) != len(set(images)) or set(images) != set(ids) - without_photos:
            return False
        return bool(re.search(r'data-testid=["\']catalog-pagination--next-page["\']', body))
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


def access_resource(request):
    """Allow source access checks to proceed independently of catalog rendering."""
    try:
        parsed = urlsplit(request.url)
        host = parsed.hostname or ""
        return (parsed.path.startswith("/cdn-cgi/")
                or host == "challenges.cloudflare.com"
                or host == "captcha-delivery.com" or host.endswith(".captcha-delivery.com"))
    except ValueError:
        return True


class DocumentGate:
    def __init__(self, hold_seconds=20, *, compact=None, url=None, region=None, remaining=None,
                 api=None, context=None, validator=None, fetch_document=False):
        self.ready = asyncio.Event()
        self.accepted = False
        self.pages = set()
        self.hold_seconds = hold_seconds
        self.compact, self.url, self.region, self.remaining = compact, url, region, remaining
        self.used_compact = False
        self.api, self.context, self.used_api = api, context, False
        self.deadline = None
        self.validator = validator or complete_catalog
        self.fetch_document = fetch_document
        self.document = None
        self.document_error = None

    def accept_document(self, text):
        if self.validator(text, self.url, self.region):
            self.document = text
            self.release(accepted=True)
            return True
        return False

    async def document_request(self, route):
        """Read the one original document before allowing Firefox to parse it.

        A large streamed page queues hundreds of script routes. Reading its
        body through Firefox can then wait for those same paused routes. The
        context request fetches the original URL once with its original browser
        headers/cookies/proxy. Refusals and unknown shells keep their actual
        response and navigation; they never cause another document GET.
        """
        response = None
        try:
            response = await route.fetch(max_redirects=0, max_retries=0,
                                         timeout=max(1, self.remaining()))
            body = await response.body()
            headers = {key: value for key, value in response.headers.items()
                       if key.lower() not in {'content-encoding', 'content-length', 'transfer-encoding'}}
            if (response.status == 200 and len(body) <= MAX_DOCUMENT_BYTES
                    and 'text/html' in headers.get('content-type', '').lower()):
                try:
                    text = body.decode('utf-8', errors='strict')
                except UnicodeDecodeError:
                    text = None
                if text is not None and self.accept_document(text):
                    # The complete source HTML is returned to the existing
                    # parser, unchanged. Firefox only needs a committed URL;
                    # executing its application adds no verified source fields.
                    body = INERT_DOCUMENT
                    headers['cache-control'] = 'no-store'
            if self.document is None:
                self.release()
            await route.fulfill(status=response.status, headers=headers, body=body)
        except Exception as exc:
            # A failed original GET is not permission to repeat paid traffic.
            logging.getLogger('uvicorn.error').info('Source document fetch failed: %s', type(exc).__name__)
            self.document_error = exc
            self.release()
            await route.abort()
        finally:
            if response is not None:
                await response.dispose()

    def wait_seconds(self):
        now = asyncio.get_running_loop().time()
        if self.deadline is None:
            budget = self.hold_seconds
            if self.remaining is not None:
                remaining = max(0, self.remaining() / 1000)
                # Reserve time for rendering an unknown shell in the same page.
                budget = min(budget, remaining - min(2, remaining * 0.25))
            self.deadline = now + max(0, budget)
        return max(0, self.deadline - now)

    async def attach(self, page):
        if page in self.pages:
            return
        self.pages.add(page)

        async def route_request(route):
            request = route.request
            api_selected = False
            if (self.api is not None and request.resource_type == "document"
                    and request.url == self.url and request.frame == page.main_frame
                    and self.api.can_handle(self.url, self.region, self.context)):
                api_selected = True
                result = await self.api.handle(route, self.url, self.region, self.remaining, self.context,
                                               accept_document=self.accept_document)
                if result is not None:
                    self.used_api = result
                    return
            # A failed API attempt falls directly back to the original document,
            # not a second speculative representation of the same fresh catalog.
            if (not api_selected and self.compact is not None and request.resource_type == "document"
                    and request.url == self.url and request.frame == page.main_frame):
                result = await self.compact.handle(route, self.url, self.region, self.remaining,
                                                   accept_document=self.accept_document)
                if result is not None:
                    self.used_compact = result
                    return
            if (request.resource_type == "document" or access_resource(request)):
                if (self.fetch_document and request.resource_type == 'document'
                        and request.url == self.url and request.frame == page.main_frame):
                    await self.document_request(route)
                    return
                await route.fallback()
                return
            try:
                if not self.ready.is_set():
                    await asyncio.wait_for(self.ready.wait(), timeout=self.wait_seconds())
            except TimeoutError:
                # Never deadlock a browser whose document requires a resource.
                self.release()
            if self.accepted:
                await route.abort()
            else:
                await route.fallback()

        await page.route("**/*", route_request)

    def release(self, accepted=False):
        if not self.ready.is_set():
            self.accepted = accepted
            self.ready.set()

    async def read(self, response, url, region):
        if self.document is not None:
            return self.document
        if response is None or response.status != 200 or response.url != url:
            return None
        try:
            # The body and held resources share one deadline. Independent 2-second
            # timers used to start the full JS application while HTML was streaming.
            async with asyncio.timeout(self.wait_seconds()):
                if "text/html" not in (await response.header_value("content-type") or "").lower():
                    return None
                body = await response.body()
            if len(body) > MAX_DOCUMENT_BYTES:
                return None
            text = body.decode("utf-8", errors="strict")
            if self.validator(text, url, region):
                return text
        except Exception as exc:
            logging.getLogger("uvicorn.error").info("Catalog document fallback: %s", type(exc).__name__)
        return None
