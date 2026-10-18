from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .models import Capabilities, FilterCheck, Item, Pagination, ParseRequest, Region, Warning


@dataclass(frozen=True)
class GatewayQuery:
    region: str
    search_text: str
    page: int
    timeout_seconds: int
    request_id: str
    newest_first: bool = False


@dataclass(frozen=True)
class GatewayPage:
    region: str
    url: str
    final_url: str
    outcome: str
    html: bytes
    upstream_status: int | None = None
    item_json: bytes = b""
    retry_after: int | None = None


@dataclass
class QueryPlan:
    query: GatewayQuery
    applied: list[FilterCheck] = field(default_factory=list)
    residual: list[FilterCheck] = field(default_factory=list)


@dataclass
class ParsedPage:
    records: list[Any]
    pagination: Pagination
    warnings: list[Warning] = field(default_factory=list)
    # Includes unexpected item links outside recognized card containers.
    unrecognized_cards: int = 0
    observed_item_ids: list[str] = field(default_factory=list)
    identities_complete: bool = False
    empty_confirmed: bool = False


class GatewayClient(Protocol):
    async def list_regions(self, request_id: str) -> list[Region]: ...
    async def fetch_catalog(self, query: GatewayQuery) -> GatewayPage: ...
    async def close(self) -> None: ...


class SourceAdapter(Protocol):
    source_id: str

    def normalize_region(self, region: str) -> str: ...
    def available_regions(self, regions: list[Region]) -> list[Region]: ...
    def capabilities(self, regions: list[Region]) -> Capabilities: ...
    def build_query(self, request: ParseRequest, timeout: int) -> QueryPlan: ...
    def validate_response(self, page: GatewayPage, query: GatewayQuery) -> None: ...
    def parse_page(self, html: str, query: GatewayQuery) -> ParsedPage: ...
    def normalize_item(self, record: Any, region: str, fetched_at: datetime) -> Item: ...
