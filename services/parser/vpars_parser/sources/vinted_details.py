"""Public item-page data. Never evaluate JavaScript or infer dates from item/photo IDs."""

import json
import math
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..errors import ParserError
from ..models import ItemDetails, Seller
from .vinted import ITEM_PATH, photo_url, region_url


def objects(value):
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            yield node
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(reversed(node))


def flight_objects(tree):
    chunks = []
    prefix = "self.__next_f.push("
    for script in tree.css("script"):
        text = script.text().strip().removesuffix(";")
        if text.startswith(prefix) and text.endswith(")"):
            try:
                value = json.loads(text[len(prefix) : -1])
            except (ValueError, RecursionError):
                continue
            if (
                isinstance(value, list)
                and len(value) == 2
                and value[0] == 1
                and isinstance(value[1], str)
            ):
                chunks.append(value[1])
    for line in "".join(chunks).splitlines():
        try:
            value = json.loads(line.partition(":")[2])
        except (ValueError, RecursionError):
            continue
        yield from objects(value)


def exact_date(value):
    # Only explicit ISO date+time with timezone: no relative labels, updated_at,
    # first-seen time, thumbnail timestamps or numeric ID estimates.
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except ValueError:
        return None


def created_date(value):
    """Legacy item created_at_ts is usually ISO; also accept explicit Unix seconds."""
    if type(value) in (int, float):
        try:
            if not math.isfinite(value) or not 0 <= value < 253402300800:
                return None
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return exact_date(value)


def seller_id(value):
    value = str(value)
    return value if re.fullmatch(r"[1-9][0-9]{0,19}", value) else None


def seller_data(record, identity, name):
    """Vinted reputation is 0..1; the visible rating uses five stars."""
    identity = seller_id(identity)
    if identity is None:
        return None
    count = record.get("feedback_count")
    if type(count) is not int or not 0 <= count <= 2147483647:
        count = None
    reputation = record.get("feedback_reputation")
    rating = (
        round(reputation * 5, 2)
        if type(reputation) in (int, float) and 0 <= reputation <= 1 and count != 0
        else None
    )
    return Seller(
        source_user_id=identity,
        username=" ".join(name.split())[:100] or None if isinstance(name, str) else None,
        rating=rating,
        feedback_count=count,
    )


def api_seller(item):
    user = item.get("user")
    if not isinstance(user, dict):
        return None
    identity = seller_id(user.get("id"))
    if any(
        field in item and seller_id(item[field]) != identity for field in ("user_id", "seller_id")
    ):
        return None
    return seller_data(user, identity, user.get("login"))


def page_seller(records):
    # Only records explicitly tied to this item; never use recommendations or buyer data.
    identities = {seller_id(r["seller_id"]) for r in records if "seller_id" in r}
    if len(identities) != 1 or None in identities:
        return None
    candidates = [
        seller_data(record, record.get("seller_id"), record.get("name"))
        for record in records
        if "feedback_count" in record or "feedback_reputation" in record
    ]
    candidates = [seller for seller in candidates if seller is not None]
    if not candidates or any(seller != candidates[0] for seller in candidates):
        return None
    return candidates[0]


def parse_api_details(data, request, fetched_at):
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ParserError("InvalidItemDetails", "Invalid item API JSON") from None
    if (
        not isinstance(payload, dict)
        or type(payload.get("code")) is not int
        or payload["code"] != 0
    ):
        raise ParserError("InvalidItemDetails", "Item API did not return a successful result")
    item = payload.get("item")
    if not isinstance(item, dict) or str(item.get("id")) != request.source_item_id:
        raise ParserError("ItemIdentityMismatch", "API returned another item")
    photos = item.get("photos")
    complete = isinstance(photos, list) and bool(photos)
    urls = []
    for photo in photos if isinstance(photos, list) else []:
        if (
            not isinstance(photo, dict)
            or not isinstance(photo.get("url"), str)
            or not photo_url(photo["url"])
        ):
            complete = False
            continue
        if photo["url"] not in urls:
            urls.append(photo["url"])
    # Only the root item date is evidence. Photo timestamps, seller registration,
    # updated_at and last_push_up_at never establish the age of this listing.
    dates = {}
    state_fields = ("is_draft", "is_processing", "is_hidden", "is_closed")
    explicitly_public = all(item.get(field) is False for field in state_fields)
    contradicts_publication = any(
        field in item and item[field] is not False and item[field] is not None
        for field in state_fields
    )
    if not contradicts_publication:
        for field in ("created_at_ts", "published_at", "datePublished", "created_at"):
            # An explicit publication date does not require every optional flag.
            # Creation dates can belong to drafts; retain the stricter proof for them.
            if field in {"created_at_ts", "created_at"} and not explicitly_public:
                continue
            stamp = (
                created_date(item.get(field))
                if field == "created_at_ts"
                else exact_date(item.get(field))
            )
            if stamp is not None:
                dates[field] = stamp
    unique = set(dates.values())
    published = next(iter(unique)) if len(unique) == 1 else None
    return ItemDetails(
        source_id=request.source_id,
        region_id=request.region_id,
        source_item_id=request.source_item_id,
        photo_urls=urls,
        photos_complete=complete,
        seller=api_seller(item),
        description=item.get("description") if isinstance(item.get("description"), str) else None,
        published_at=published,
        fetched_at=fetched_at,
        publication_evidence=(
            "api:" + ",".join(dates)
            if published
            else "conflicting_dates"
            if len(unique) > 1
            else "unknown"
        ),
    )


def parse_details(html, request, fetched_at):
    tree = LexborHTMLParser(html)
    galleries = []
    dates = set()
    item_records = []
    descriptions = set()
    complete = True
    for record in flight_objects(tree):
        if str(record.get("item_id", record.get("id", ""))) != request.source_item_id:
            continue
        item_records.append(record)
        if isinstance(record.get("description"), str):
            descriptions.add(record["description"])
        photos = record.get("photos")
        if isinstance(photos, list) and photos:
            urls = []
            for photo in photos:
                if (
                    not isinstance(photo, dict)
                    or not isinstance(photo.get("url"), str)
                    or not photo_url(photo["url"])
                ):
                    complete = False
                    continue
                if photo["url"] not in urls:
                    urls.append(photo["url"])
            galleries.append(urls)
        if stamp := exact_date(record.get("created_at")):
            dates.add(stamp)
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text())
        except (ValueError, RecursionError):
            continue
        for record in objects(data):
            if record.get("@type") != "Product":
                continue
            offers = record.get("offers")
            url = record.get("url") or (offers.get("url") if isinstance(offers, dict) else None)
            if not isinstance(url, str) or not region_url(url, request.region_id):
                continue
            match = ITEM_PATH.match(urlsplit(url).path)
            if match and match[1] == request.source_item_id:
                if isinstance(record.get("description"), str):
                    descriptions.add(record["description"])
                if stamp := exact_date(record.get("datePublished")):
                    dates.add(stamp)
    longest = max(galleries, key=len, default=[])
    complete = complete and bool(longest)
    if any(urls != longest[: len(urls)] for urls in galleries):
        complete = False
        longest = list(dict.fromkeys(url for urls in galleries for url in urls))
    uploaded = tree.css_first(
        '[data-testid="item-attributes-upload_date"] [itemprop="upload_date"]'
    )
    published = next(iter(dates)) if len(dates) == 1 else None
    return ItemDetails(
        source_id=request.source_id,
        region_id=request.region_id,
        source_item_id=request.source_item_id,
        photo_urls=longest,
        photos_complete=complete,
        seller=page_seller(item_records),
        description="\n".join(sorted(descriptions)) if descriptions else None,
        published_at=published,
        fetched_at=fetched_at,
        publication_evidence=(
            "exact"
            if published
            else "conflicting_dates"
            if len(dates) > 1
            else "relative:" + " ".join(uploaded.text().split())[:100]
            if uploaded and uploaded.text().strip()
            else "unknown"
        ),
    )


def validate_item_page(page, request):
    if page.region != request.region_id:
        raise ParserError("GatewayContractError", "Gateway returned another region")
    for url in (page.url, page.final_url):
        if not region_url(url, request.region_id):
            raise ParserError("UnexpectedRedirect", "Item left its regional domain")
        parsed = urlsplit(url)
        match = ITEM_PATH.match(parsed.path)
        valid = (
            parsed.path == f"/api/v2/items/{request.source_item_id}/details"
            and not parsed.query
            and not parsed.fragment
            if page.item_json
            else match and match[1] == request.source_item_id
        )
        if not valid:
            raise ParserError("UnexpectedRedirect", "Gateway returned another item")
    if page.upstream_status is not None and not 200 <= page.upstream_status < 300:
        raise ParserError("GatewayContractError", "Item response has an unsuccessful status")
