"""Keywords followed by a single EUR price range for all marketplaces."""

from copy import deepcopy
from uuid import uuid4

from .domain import (
    PRICE_PROMPT,
    Filters,
    UserError,
    conflict_notice,
    excluded_values,
    parse_keywords,
    parse_price,
)

KEYWORDS_PROMPT = (
    "🔤 <b>Введи ключові слова через кому</b>\n\nНаприклад: Gucci, Jeans, W32, Very Good"
    "\n\nКілька розмірів або станів — на вибір: S, M, L; Good, Very Good."
    " Решта слів мають збігатися разом."
    "\n\n👖 Для джинсів вказуй розмір у форматі W: W30, W32, W34 замість S, M, L."
)
EXCLUSIONS_PROMPT = (
    "🔤 <b>Введи виключенні слова через кому</b>\n\nНаприклад: Fake, Hole, Junior, Damaged, kids."
)


def new_filter_draft(registry):
    return {
        "kind": "keyword_filter",
        "operation_id": str(uuid4()),
        "keywords": [],
        "excluded_keywords": [],
        "step": "keywords",
        "price": None,
    }


def upgrade_draft(draft, registry):
    if draft.get("kind") == "keyword_filter":
        result = new_filter_draft(registry)
        result.update(deepcopy(draft))
        if ":" in result["step"]:
            result["previous_draft"] = deepcopy(draft)
            result["step"] = "price"
        return result
    result = new_filter_draft(registry)
    result["previous_draft"] = deepcopy(draft)
    return result


def steps(draft):
    return [
        "keywords",
        "excluded_keywords",
        "price",
        "complete",
    ]


def input_value(draft, text, update_id, *, skip=False):
    result = deepcopy(draft)
    step = result["step"]
    if step == "keywords":
        if skip:
            raise UserError("Введи ключові слова через кому.")
        result["keywords"] = parse_keywords(text)
    elif step == "excluded_keywords":
        result["excluded_keywords"] = [] if skip else excluded_values(text.split(","))
    elif step == "price":
        result["price"] = None if skip else parse_price(text, "EUR").model_dump(mode="json")
    else:
        raise UserError("Натисни «Додати фільтр», щоб створити новий.")
    order = steps(result)
    result["step"] = order[order.index(step) + 1]
    return result


def build_filters(draft, registry):
    if draft["step"] != "complete":
        raise UserError("Спочатку вкажи або пропусти межі цін.")
    return Filters(
        keywords=draft["keywords"],
        keywords_all=True,
        excluded_keywords=draft.get("excluded_keywords", []),
        sources={key: source.regions for key, source in registry.sources.items()},
        follow_enabled_sources=True,
        price=draft.get("price"),
    )


def render_draft(draft, registry):
    if draft["step"] == "excluded_keywords":
        return EXCLUSIONS_PROMPT, [
            [("Пропустити", "exclusions_skip")],
            [("Скасувати", "cancel")],
        ]
    if draft["step"] == "price":
        from html import escape

        notice = conflict_notice(draft)
        prompt = PRICE_PROMPT
        if notice:
            prompt = "⚠️ " + escape(notice) + "\n\n" + prompt
        return prompt, [
            [("Пропустити", "price_skip")],
            [("Скасувати", "cancel")],
        ]
    return KEYWORDS_PROMPT, [[("Скасувати", "cancel")]]
