from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
RequestId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9._:-]{1,128}$")]


def decimal_input(value):
    if not isinstance(value, (str, Decimal)):
        raise ValueError("Money amounts must be decimal strings")
    return value


Amount = Annotated[
    Decimal,
    BeforeValidator(decimal_input, json_schema_input_type=str),
    Field(ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False),
]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Money(Model):
    amount: Amount
    currency: Currency


class PriceFilter(Model):
    currency: Currency
    minimum: Amount | None = None
    maximum: Amount | None = None

    @model_validator(mode="after")
    def valid_range(self):
        if self.minimum is None and self.maximum is None:
            raise ValueError("At least one price bound is required")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Minimum exceeds maximum")
        return self


class Filters(Model):
    models: list[Text] = Field(default_factory=list, max_length=50)
    keywords: list[Text] = Field(default_factory=list, max_length=50)
    keywords_all: bool = False
    sizes: list[Text] = Field(default_factory=list, max_length=50)
    conditions: list[Text] = Field(default_factory=list, max_length=50)
    brands: list[Text] = Field(default_factory=list, max_length=50)
    colors: list[Text] = Field(default_factory=list, max_length=50)
    categories: list[Text] = Field(default_factory=list, max_length=50)
    price: PriceFilter | None = None


class ParseRequest(Model):
    request_id: RequestId = Field(default_factory=lambda: str(uuid4()))
    source_id: Text = "vinted"
    region_id: Text
    search_text: str = Field(default="", max_length=500)
    filters: Filters = Field(default_factory=Filters)
    page: int = Field(default=1, ge=1, le=4294967295, strict=True)
    newest_first: bool = False
    timeout_seconds: int | None = Field(default=None, ge=1, le=300, strict=True)

    @field_validator("search_text")
    @classmethod
    def valid_search(cls, value):
        if len(value.encode("utf-8")) > 500 or any(ord(c) < 32 for c in value):
            raise ValueError("Search text must be at most 500 UTF-8 bytes, without controls")
        return value.strip()


class Attribute(Model):
    raw: str
    normalized: str | None = None


class Seller(Model):
    source_user_id: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,19}$")]
    username: str | None = Field(default=None, max_length=100)
    rating: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    feedback_count: int | None = Field(default=None, ge=0, le=2147483647, strict=True)


class Item(Model):
    source_id: str
    region_id: str
    source_namespace: str
    source_item_id: str
    title: str
    description: str | None = None
    price: Money
    item_url: str
    photo_urls: list[str] = Field(default_factory=list)
    brand: Attribute | None = None
    condition: Attribute | None = None
    size: Attribute | None = None
    colors: list[Attribute] | None = None
    category: Attribute | None = None
    availability: str = "unknown"
    fetched_at: datetime
    published_at: AwareDatetime | None = None
    first_seen_at: AwareDatetime | None = None
    photos_complete: bool = False
    seller: Seller | None = None
    source_attributes: dict[str, str] = Field(default_factory=dict)


class ItemDetailsRequest(Model):
    request_id: RequestId = Field(default_factory=lambda: str(uuid4()))
    source_id: Text = "vinted"
    region_id: Text
    source_item_id: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,19}$")]
    timeout_seconds: int = Field(default=15, ge=1, le=45, strict=True)
    force_html: bool = False


class ItemDetails(Model):
    source_id: str
    region_id: str
    source_item_id: str
    photo_urls: list[str]
    photos_complete: bool
    description: str | None = None
    seller: Seller | None = None
    published_at: AwareDatetime | None = None
    publication_evidence: str = "unknown"
    fetched_at: AwareDatetime


class Pagination(Model):
    requested_page: int
    has_more: Literal["true", "false", "unknown"] = "unknown"
    next_page: int | None = None
    cursor: str | None = None


class Warning(Model):
    code: str
    message: str
    item_id: str | None = None
    field: str | None = None


class FilterCheck(Model):
    field: str
    values: list[str] = Field(default_factory=list)
    normalized_values: list[str] | None = None
    price: PriceFilter | None = None
    operation: str
    item_field: str | None = None
    missing_value: Literal["not_confirmed"] = "not_confirmed"


class Timings(Model):
    access_ms: float
    parse_ms: float
    normalization_ms: float
    total_ms: float


class ParseResult(Model):
    request_id: str
    source_id: str
    region_id: str
    items: list[Item]
    pagination: Pagination
    completeness: Literal["complete", "partial"]
    warnings: list[Warning]
    applied_filters: list[FilterCheck]
    residual_filters: list[FilterCheck]
    filter_logic: Literal[
        "AND_between_fields_OR_within_field",
        "AND_between_fields_AND_within_keywords",  # Historical audit records.
        "AND_text_OR_sizes_OR_conditions",
    ] = "AND_between_fields_OR_within_field"
    timings: Timings
    html_bytes: int
    cards_seen: int
    cards_skipped: int
    observed_item_ids: list[str] = Field(default_factory=list)
    identities_complete: bool = False
    identities_ordered: bool = Field(
        default=False, description="Inventory follows DOM order, not publication time"
    )
    sort_order: Literal["source_default", "newest_first_requested"] = "source_default"
    empty_confirmed: bool = False


class ErrorDetail(Model):
    code: str
    message: str
    source_status: str | None = None
    fields: list[str] = Field(default_factory=list)
    retry_after: int | None = Field(default=None, ge=1, le=86400)


class ErrorResponse(Model):
    request_id: str
    error: ErrorDetail


class Region(Model):
    code: str
    name: str
    base_url: str


class FilterCapability(Model):
    mode: Literal["applied", "residual", "unsupported"]
    description: str


class Capabilities(Model):
    source_id: str
    version: str
    regions: list[Region]
    aliases: dict[str, str]
    filters: dict[str, FilterCapability]
    confirmed_empty_catalog: bool = False
    newest_first: bool = Field(
        default=False,
        description="Supports requesting newest_first; does not prove chronological results",
    )
    newest_first_guaranteed: bool = False
