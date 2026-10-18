"""Pure Vinted query/attribute rules shared by parser and local fan-out."""

from vpars_shared.sizes import normalized_size

from ..errors import ParserError
from ..models import Attribute, FilterCheck, ParseRequest
from ..ports import GatewayQuery, QueryPlan
from .vinted_attributes import CONDITIONS


def clean(value: str) -> str:
    return " ".join(value.split())


def attribute(field: str, raw: str | None) -> Attribute | None:
    if not raw:
        return None
    normalized = clean(raw).casefold()
    if field == "condition":
        normalized = CONDITIONS.get(normalized)
    elif field == "size":
        normalized = normalized_size(raw)
    return Attribute(raw=raw, normalized=normalized)


def normalize_region(region: str) -> str:
    region = region.strip().upper()
    region = "UK" if region == "GB" else region
    if region not in {"UK", "FR", "IT", "DE"}:
        raise ParserError("UnsupportedRegion", "Enabled Vinted regions: UK/GB, FR, IT, DE", 422)
    return region


def build_query(request: ParseRequest, timeout: int) -> QueryPlan:
    region = normalize_region(request.region_id)
    filters = request.filters
    unsupported = [name for name in ("colors", "categories") if getattr(filters, name)]
    if any(attribute("size", value).normalized is None for value in filters.sizes):
        unsupported.append("sizes")
    if filters.conditions and any(
        v.casefold() not in set(CONDITIONS.values()) for v in filters.conditions
    ):
        unsupported.append("conditions")
    if filters.price and filters.price.currency != ("GBP" if region == "UK" else "EUR"):
        unsupported.append("price.currency")
    if unsupported:
        raise ParserError(
            "UnsupportedFilter",
            "Requested filters cannot be verified for this catalog",
            422,
            fields=unsupported,
        )
    plan = QueryPlan(
        GatewayQuery(
            region,
            request.search_text,
            request.page,
            timeout,
            request.request_id,
            request.newest_first,
        )
    )
    if request.search_text:
        plan.applied.append(
            FilterCheck(
                field="search_text",
                values=[request.search_text],
                operation="source_text_search",
            )
        )
    for field, item_field in (
        ("models", "title"),
        ("keywords", "title"),
        ("brands", "brand.normalized"),
        ("conditions", "condition.normalized"),
        ("sizes", "size.normalized"),
    ):
        values = getattr(filters, field)
        if values:
            operation = (
                "any_alternative_all_words_casefold"
                if item_field == "title"
                else "any_exact_casefold"
            )
            if field == "keywords" and filters.keywords_all:
                operation = "grouped_keywords_card_casefold"
                item_field = "searchable_card"
            # Never reduce multiple alternatives to a narrower source text query.
            plan.residual.append(
                FilterCheck(
                    field=field,
                    values=values,
                    operation=operation,
                    item_field=item_field,
                    normalized_values=(
                        [attribute("size", v).normalized for v in values]
                        if field == "sizes"
                        else [clean(v).casefold() for v in values]
                    ),
                )
            )
    if filters.price:
        plan.residual.append(
            FilterCheck(
                field="price",
                price=filters.price,
                operation="inclusive_range_same_currency",
                item_field="price",
            )
        )
    return plan
