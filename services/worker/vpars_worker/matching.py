import re
import unicodedata
from decimal import Decimal

from vpars_parser.models import Item, ParseRequest, ParseResult
from vpars_parser.sources.vinted_attributes import CONDITIONS, item_waist_size
from vpars_shared.sizes import normalized_size

FIELDS = {
    "models": ("title", "any_alternative_all_words_casefold"),
    "keywords": ("title", "any_alternative_all_words_casefold"),
    "brands": ("brand.normalized", "any_exact_casefold"),
    "sizes": ("size.normalized", "any_exact_casefold"),
    "conditions": ("condition.normalized", "any_exact_casefold"),
    "price": ("price", "inclusive_range_same_currency"),
}


def keyword_size(value: str) -> str | None:
    """Use the same whole-entry size detection in querying and local matching."""
    return normalized_size(value)


def size_matches(item: Item, alternatives) -> bool:
    """Keep the requested size system; never guess an EU/UK conversion.

    Bare numeric catalog sizes in FR/IT/DE are local European sizes. In UK,
    an EU value needs an explicit EU label on the card (size or title).
    Composite clothing labels such as M / 38 / 10 remain M, not EU 38.
    An unqualified number also accepts an explicitly verified W size with that
    number. Qualified EU/UK/W requests never guess a different size system.
    """
    waist = item_waist_size(item.size.raw if item.size else None, item.title)
    if item.size is None and waist is None:
        return False
    value = waist or (
        normalized_size(item.size.raw) or normalized_size(item.size.normalized) or ""
        if item.size
        else ""
    )
    local_system = "uk" if item.region_id == "UK" else "eu"
    for alternative in alternatives:
        alternative = normalized_size(alternative) or alternative.casefold()
        qualified = re.fullmatch(r"(eu|uk) (\d{1,3}(?:\.\d+)?)", alternative)
        if not qualified:
            if value == alternative:
                return True
            if waist and re.fullmatch(r"\d{2,3}", alternative):
                if waist == f"w{int(alternative)}":
                    return True
            continue
        system, number = qualified.groups()
        # Explicit source labels are evidence; a plain number in a title is not.
        labels = re.findall(
            r"\b" + (r"(?:EU|EUR)" if system == "eu" else "UK") + r"\s*(\d{1,3}(?:[.,]\d+)?)\b",
            (item.size.raw if item.size else "") + " " + item.title,
            flags=re.IGNORECASE,
        )
        numbers = {Decimal(label.replace(",", ".")) for label in labels}
        if system == local_system and re.fullmatch(r"\d{1,3}(?:\.\d+)?", value):
            numbers.add(Decimal(value))
        if numbers == {Decimal(number)}:
            return True
    return False


def validate_result(request: ParseRequest, result: ParseResult):
    if result.empty_confirmed and (
        result.items
        or result.observed_item_ids
        or result.cards_seen
        or result.cards_skipped
        or not result.identities_complete
        or result.completeness != "complete"
        or result.pagination.has_more != "false"
        or result.pagination.next_page is not None
    ):
        raise ValueError("Contradictory empty catalog proof")
    if result.identities_complete and (
        (not result.observed_item_ids and not result.empty_confirmed)
        or len(result.observed_item_ids) != len(set(result.observed_item_ids))
        or any(not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in result.observed_item_ids)
        or not {i.source_item_id for i in result.items} <= set(result.observed_item_ids)
    ):
        raise ValueError("Incomplete catalog identity inventory")
    if result.pagination.requested_page != request.page:
        raise ValueError("Parser returned another page")
    region = "UK" if request.region_id == "GB" else request.region_id
    if (result.request_id, result.source_id, result.region_id) != (
        request.request_id,
        request.source_id,
        region,
    ):
        raise ValueError("Parser returned another query")
    expected = {name for name in FIELDS if getattr(request.filters, name)}
    checks = {check.field: check for check in result.residual_filters}
    if set(checks) != expected or len(checks) != len(result.residual_filters):
        raise ValueError("Parser did not verify all active filters")
    for name, check in checks.items():
        expected_operation = (
            ("searchable_card", "grouped_keywords_card_casefold")
            if name == "keywords" and request.filters.keywords_all
            else FIELDS[name]
        )
        if (check.item_field, check.operation) != expected_operation:
            raise ValueError("Unknown filter operation")
        if name == "price":
            if check.price != request.filters.price:
                raise ValueError("Price filter changed")
        elif check.values != getattr(request.filters, name) or not check.normalized_values:
            raise ValueError("Filter values changed or unverified")
        if (
            name == "keywords"
            and request.filters.keywords_all
            and check.normalized_values
            != [" ".join(value.split()).casefold() for value in request.filters.keywords]
        ):
            raise ValueError("Keyword values changed during normalization")
    for item in result.items:
        if (item.source_id, item.region_id, item.source_namespace) != (
            request.source_id,
            region,
            f"{request.source_id}:{region}",
        ):
            raise ValueError("Item belongs to another catalog")


def keyword_matches(item: Item, values) -> bool:
    """Require every text phrase, any selected size and any selected condition."""
    parts = [item.title, item.source_attributes.get("model", "")]
    for field in (item.brand, item.size, item.condition, item.category, *(item.colors or [])):
        if field:
            parts.extend([field.raw, field.normalized or ""])
    words = set(re.findall(r"\w+", unicodedata.normalize("NFKC", " ".join(parts)).casefold()))
    sizes, conditions = [], []
    for value in values:
        value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        # Whole-entry attributes are OR alternatives inside their own group.
        # The text constraints remain AND; a title cannot prove size or condition.
        if (normalized := keyword_size(value)) is not None:
            sizes.append(normalized)
        elif value in CONDITIONS:
            conditions.append(CONDITIONS[value])
        elif not (terms := set(re.findall(r"\w+", value))) or not terms <= words:
            return False
    if sizes and not size_matches(item, sizes):
        return False
    if conditions and (item.condition is None or item.condition.normalized not in conditions):
        return False
    return bool(values)


def excluded_match(item: Item, values) -> bool:
    """Any whole word/phrase in title or description rejects the lot for this filter."""
    parts = [item.title, item.description or ""]
    tokenized = [
        re.findall(r"\w+", unicodedata.normalize("NFKC", part).casefold()) for part in parts
    ]
    for value in values:
        terms = re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold())
        if not terms:
            continue
        for words in tokenized:
            if any(words[i : i + len(terms)] == terms for i in range(len(words) - len(terms) + 1)):
                return True
    return False


def matches(item: Item, checks) -> bool:
    if item.availability in {"sold", "reserved", "unavailable"}:
        return False
    for check in checks:
        if check.operation == "grouped_keywords_card_casefold":
            if not keyword_matches(item, check.normalized_values or []):
                return False
            continue
        if check.field == "sizes":
            if not size_matches(item, check.normalized_values or []):
                return False
            continue
        if check.operation == "inclusive_range_same_currency":
            price = check.price
            if (
                price is None
                or item.price.currency != price.currency
                or (price.minimum is not None and item.price.amount < price.minimum)
                or (price.maximum is not None and item.price.amount > price.maximum)
            ):
                return False
            continue
        value = item
        for part in (check.item_field or "").split("."):
            value = getattr(value, part, None)
            if value is None:
                return False
        value = " ".join(str(value).split()).casefold()
        alternatives = check.normalized_values
        if not alternatives:
            return False
        if check.operation == "any_exact_casefold":
            if value not in [v.casefold() for v in alternatives]:
                return False
        elif check.operation == "any_alternative_all_words_casefold":
            words = set(re.findall(r"\w+", value))
            if not any(
                (terms := set(re.findall(r"\w+", alternative.casefold()))) and terms <= words
                for alternative in alternatives
            ):
                return False
        else:
            return False
    return True


def mismatch_reason(item: Item, checks):
    """Keep a reviewable explanation without weakening any existing predicate."""
    if item.availability in {"sold", "reserved", "unavailable"}:
        return {"code": "Unavailable", "field": "availability", "actual": item.availability}
    for check in checks:
        if not matches(item, [check]):
            if check.item_field == "searchable_card":
                actual = item.model_dump(
                    mode="json",
                    include={
                        "title",
                        "brand",
                        "size",
                        "condition",
                        "category",
                        "colors",
                    },
                )
                actual["model"] = item.source_attributes.get("model")
            else:
                actual = item
                for part in (check.item_field or "").split("."):
                    actual = getattr(actual, part, None)
            if hasattr(actual, "model_dump"):
                actual = actual.model_dump(mode="json")
            return {
                "code": "FilterValueUnknown" if actual is None else "FilterMismatch",
                "field": check.field,
                "actual": actual,
                "expected": check.model_dump(mode="json"),
            }
    return None
