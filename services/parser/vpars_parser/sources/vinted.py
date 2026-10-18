"""DOM contract observed in gateway captures from 2026-09-15; see fixtures/PROVENANCE.md."""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from ..errors import ParserError
from ..models import (
    Attribute,
    Capabilities,
    FilterCapability,
    Item,
    Money,
    Pagination,
    ParseRequest,
    Region,
    Warning,
)
from ..ports import GatewayPage, GatewayQuery, ParsedPage, QueryPlan
from .vinted_attributes import item_waist_size
from .vinted_query import attribute, build_query, clean, normalize_region

DOMAINS = {"UK": "vinted.co.uk", "FR": "vinted.fr", "IT": "vinted.it", "DE": "vinted.de"}
LABELS = {
    "Brand": "brand",
    "Marque": "brand",
    "Marke": "brand",
    "Condition": "condition",
    "État": "condition",
    "Condizioni": "condition",
    "Zustand": "condition",
    "Size": "size",
    "Taille": "size",
    "Taglia": "size",
    "Größe": "size",
    "Model": "model",
    "Modell": "model",
}
MARKER = re.compile(r", (" + "|".join(map(re.escape, LABELS)) + r"): ")
PRICE_TAIL = re.compile(r", (\d+\.\d{2}) ([£€]), (\d+\.\d{2}) ([£€])$")
CARD_ID = re.compile(r"product-item-id-(\d+)$")
ITEM_PATH = re.compile(r"^/items/(\d+)(?:[-/]|$)")


def region_url(url: str, region: str) -> bool:
    try:
        p = urlsplit(url)
        return (
            p.scheme == "https"
            and p.hostname in (DOMAINS[region], "www." + DOMAINS[region])
            and p.port in (None, 443)
            and p.username is None
            and p.password is None
        )
    except (ValueError, KeyError):
        return False


def photo_url(value: str) -> bool:
    try:
        p = urlsplit(value)
        return (
            p.scheme == "https"
            and bool(p.hostname)
            and p.username is None
            and p.password is None
            and p.port in (None, 443)
        )
    except ValueError:
        return False


def catalog_identity(href, region):
    try:
        absolute = urljoin(f"https://www.{DOMAINS[region]}", href)
        match = ITEM_PATH.match(urlsplit(absolute).path)
        return match[1] if match and region_url(absolute, region) else None
    except ValueError:
        return None


def parse_money(raw: str, region: str) -> Money:
    raw = clean(raw)
    if region == "UK":
        match = re.fullmatch(r"£((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})", raw)
        number = match.group(1).replace(",", "") if match else None
        currency = "GBP"
    else:
        match = re.fullmatch(r"((?:\d{1,3}(?:[. ]\d{3})+|\d+),\d{2}) €", raw)
        number = (
            match.group(1).replace(".", "").replace(" ", "").replace(",", ".") if match else None
        )
        currency = "EUR"
    if number is None:
        raise ValueError("Unrecognized item price or currency")
    return Money(amount=Decimal(number), currency=currency)


def item_size(raw: str | None, title: str) -> Attribute | None:
    if waist := item_waist_size(raw, title):
        return Attribute(raw=raw or waist.upper(), normalized=waist)
    return attribute("size", raw)


@dataclass
class Card:
    item_id: str
    href: str
    description: str
    price: str
    photos: list[str]


class VintedAdapter:
    source_id = "vinted"

    def parse_details(self, html, request, fetched_at):
        from .vinted_details import parse_details

        return parse_details(html, request, fetched_at)

    def validate_item_response(self, page, request):
        from .vinted_details import validate_item_page

        validate_item_page(page, request)

    def parse_api_details(self, data, request, fetched_at):
        from .vinted_details import parse_api_details

        return parse_api_details(data, request, fetched_at)

    def normalize_region(self, region: str) -> str:
        return normalize_region(region)

    def available_regions(self, regions: list[Region]) -> list[Region]:
        return [r for r in regions if r.code in DOMAINS and region_url(r.base_url, r.code)]

    def capabilities(self, regions: list[Region]) -> Capabilities:
        filters = {
            "search_text": FilterCapability(
                mode="applied", description="Gateway text query; source relevance semantics"
            ),
            "models": FilterCapability(
                mode="residual", description="OR alternatives, all words per alternative in title"
            ),
            "keywords": FilterCapability(
                mode="residual",
                description=(
                    "OR title alternatives; keywords_all requires every text phrase, "
                    "any selected size and any selected normalized condition"
                ),
            ),
            "brands": FilterCapability(
                mode="residual", description="Case-insensitive exact normalized brand"
            ),
            "keywords_all": FilterCapability(
                mode="residual",
                description="AND text phrases; OR within size and condition groups",
            ),
            "conditions": FilterCapability(
                mode="residual", description="English mapped conditions; unknown is not a match"
            ),
            "sizes": FilterCapability(
                mode="residual",
                description="Normalized size with explicit EU/UK qualifiers; no size conversion",
            ),
            "price": FilterCapability(
                mode="residual", description="Inclusive decimal bounds in explicit source currency"
            ),
            "colors": FilterCapability(
                mode="unsupported", description="Not present in verified catalog cards"
            ),
            "categories": FilterCapability(
                mode="unsupported", description="Not present in verified catalog cards"
            ),
        }
        return Capabilities(
            source_id=self.source_id,
            version="vinted-dom-20260915-v1",
            regions=self.available_regions(regions),
            aliases={"GB": "UK"},
            filters=filters,
            newest_first=True,
        )

    def build_query(self, request: ParseRequest, timeout: int) -> QueryPlan:
        return build_query(request, timeout)

    def validate_response(self, page: GatewayPage, query: GatewayQuery) -> None:
        if page.region != query.region:
            raise ParserError("GatewayContractError", "Gateway returned a different region")
        for value in (page.url, page.final_url):
            if (
                not region_url(value, query.region)
                or urlsplit(value).path.rstrip("/") != "/catalog"
            ):
                raise ParserError(
                    "UnexpectedRedirect",
                    "Gateway response is outside the requested regional catalog",
                )
        # final_url deliberately omits query parameters in the actual gateway.
        params = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
        if params.get("order", []) != (["newest_first"] if query.newest_first else []):
            raise ParserError("GatewayContractError", "Gateway returned a different sort order")
        if params.get("page", ["1"]) != [str(query.page)] or params.get("search_text", [""]) != [
            query.search_text
        ]:
            raise ParserError("GatewayContractError", "Gateway returned a different catalog query")
        if page.upstream_status is not None and not 200 <= page.upstream_status < 300:
            raise ParserError(
                "GatewayContractError", "PAGE_LOADED has an unsuccessful upstream status"
            )

    def parse_page(self, html: str, query: GatewayQuery) -> ParsedPage:
        tree = LexborHTMLParser(html)
        records = []
        known_nodes = set()
        identities = {}  # DOM order only: promoted cards are not publication-time proof.
        identities_complete = True
        for node in tree.css('[data-testid^="product-item-id-"]'):
            prefix = node.attrs["data-testid"]
            match = CARD_ID.fullmatch(prefix)
            if not match:
                continue
            known_nodes.add(node.mem_id)
            link = node.css_first(f'a[data-testid="{prefix}--overlay-link"]')
            price = node.css_first(f'[data-testid="{prefix}--price-text"]')
            identity = catalog_identity(link.attrs.get("href", "") if link else "", query.region)
            if identity == match[1]:
                identities[match[1]] = None
            else:
                identities_complete = False
            records.append(
                Card(
                    item_id=match[1],
                    href=link.attrs.get("href", "") if link else "",
                    description=link.attrs.get("title", "") if link else "",
                    price=price.text() if price else "",
                    photos=[
                        n.attrs.get("src", "")
                        for n in node.css(f'img[data-testid="{prefix}--image--img"]')
                    ],
                )
            )
        outside = set()
        supplementary = set()
        for link in tree.css('a[href*="/items/"]'):
            href = link.attrs.get("href", "")
            parent = link.parent
            closet = False
            while parent and parent.mem_id not in known_nodes:
                # Verified in settled catalog HTML: promoted seller wardrobes
                # have their own closet-item containers and are not search rows.
                if parent.tag == "div" and re.fullmatch(
                    r"closet-item-[1-9][0-9]{0,19}", parent.attrs.get("data-testid", "")
                ):
                    closet = True
                    break
                parent = parent.parent
            if closet:
                supplementary.add(href)
                continue
            if parent is None:
                outside.add(href)
                identity = catalog_identity(href, query.region)
                if identity:
                    identities[identity] = None
                else:
                    identities_complete = False
        empty = tree.css_first('[data-testid="search-empty-state"]')
        if empty is not None:
            # Captured on a settled public catalog on 2026-10-02. Absence of
            # cards alone never proves the end; require Vinted's explicit state.
            title = empty.css_first('[data-testid="search-empty-state--title"]')
            body = empty.css_first('[data-testid="search-empty-state--body"]')
            if (
                title is not None
                and title.text().strip()
                and body is not None
                and body.text().strip()
                and not records
                and not outside
                and not supplementary
                and not identities
                and not tree.css('[data-testid^="product-item-id-"]')
                and not tree.css(
                    '[data-testid="catalog-pagination--next-page"][aria-disabled="false"]'
                )
            ):
                return ParsedPage(
                    [],
                    Pagination(requested_page=query.page, has_more="false"),
                    identities_complete=True,
                    empty_confirmed=True,
                )
            raise ParserError("SchemaChanged", "Incomplete or contradictory empty catalog evidence")
        if not records:
            raise ParserError(
                "SchemaChanged", "No recognized catalog cards; an empty catalog is not confirmed"
            )
        pagination = Pagination(requested_page=query.page)
        warnings = []
        next_links = tree.css('[data-testid="catalog-pagination--next-page"]')
        next_values = []
        for link in next_links:
            try:
                absolute = urljoin(
                    f"https://www.{DOMAINS[query.region]}", link.attrs.get("href", "")
                )
                params = parse_qs(urlsplit(absolute).query, keep_blank_values=True)
            except ValueError:
                next_values.append(None)
                continue
            pages = params.get("page", [])
            if (
                link.attrs.get("aria-disabled") == "false"
                and region_url(absolute, query.region)
                and urlsplit(absolute).path == "/catalog"
                and len(pages) == 1
                and re.fullmatch(r"[0-9]{1,10}", pages[0])
                and query.page < int(pages[0]) <= 4294967295
                and params.get("search_text", [""]) == [query.search_text]
                and params.get("order", []) == (["newest_first"] if query.newest_first else [])
                and set(params) <= {"page", "search_text", "order"}
            ):
                next_values.append(int(pages[0]))
            else:
                next_values.append(None)
        if next_values and None not in next_values and len(set(next_values)) == 1:
            pagination.has_more = "true"
            pagination.next_page = next_values[0]
        else:
            # Disabled/absent next alone is not evidence of the end.
            warnings.append(
                Warning(
                    code="PaginationUnknown",
                    message="No verified continuation or terminal-page evidence",
                )
            )
        if outside:
            warnings.append(
                Warning(
                    code="UnrecognizedCards",
                    message="Item links exist outside recognized catalog cards",
                )
            )
        if supplementary:
            warnings.append(
                Warning(
                    code="SupplementaryItemsIgnored",
                    message=(
                        f"Excluded {len(supplementary)} promoted wardrobe links "
                        "from search inventory"
                    ),
                )
            )
        return ParsedPage(
            records, pagination, warnings, len(outside), list(identities), identities_complete
        )

    def normalize_item(self, record: Card, region: str, fetched_at: datetime) -> Item:
        if not record.href:
            raise ValueError("Missing item link")
        absolute = urljoin(f"https://www.{DOMAINS[region]}", record.href)
        path = urlsplit(absolute)
        match = ITEM_PATH.match(path.path)
        if not region_url(absolute, region) or not match or match[1] != record.item_id:
            raise ValueError("Invalid item identity or URL")
        price = parse_money(record.price, region)
        description = clean(record.description)
        tail = PRICE_TAIL.search(description)
        if (
            not tail
            or tail[2] != tail[4]
            or {"£": "GBP", "€": "EUR"}[tail[2]] != price.currency
            or Decimal(tail[1]) != price.amount
        ):
            raise ValueError("Missing or inconsistent accessible item price")
        description = description[: tail.start()]
        markers = list(MARKER.finditer(description))
        title = description[: markers[0].start()] if markers else description
        if not title:
            raise ValueError("Missing title")
        attributes = {}
        for index, marker in enumerate(markers):
            field = LABELS[marker[1]]
            if field in attributes:
                raise ValueError("Ambiguous item attributes")
            end = markers[index + 1].start() if index + 1 < len(markers) else len(description)
            attributes[field] = description[marker.end() : end]
        return Item(
            source_id=self.source_id,
            region_id=region,
            source_namespace=f"vinted:{region}",
            source_item_id=record.item_id,
            title=title,
            price=price,
            item_url=urlunsplit(("https", f"www.{DOMAINS[region]}", path.path, "", "")),
            photo_urls=list(dict.fromkeys(p for p in record.photos if photo_url(p))),
            brand=attribute("brand", attributes.get("brand")),
            condition=attribute("condition", attributes.get("condition")),
            size=item_size(attributes.get("size"), title),
            fetched_at=fetched_at,
            source_attributes={"model": attributes["model"]} if attributes.get("model") else {},
        )
