"""Brand-first filters with a single EUR price question."""

from copy import deepcopy
from uuid import uuid4

from .domain import (
    PRICE_PROMPT,
    Filters,
    Price,
    PriceBounds,
    UserError,
    english_value,
    parse_price,
    parse_sizes,
)
from .formatting import filter_summary, html

SINGLE = {"brands", "models"}
MULTIPLE = {"sizes", "keywords"}
QUESTIONS = {
    "brands": "🏷 <b>Який бренд?</b>\n\nВведи один бренд англійською, наприклад: Nike.",
    "models": "👟 <b>Яку модель шукаємо?</b>\n\nВведи одну модель англійською, наприклад: Air Max 90.",
    "sizes": "📏 <b>Які розміри?</b>\n\nДжинси: W30, W32, W34.\nОдяг: XS, S, M, L, XL, XXL. Можна писати с, м, л.\nВзуття: наприклад, EU 42.\nКілька розмірів: W30, W32 або S, M, L. Також є «Додати ще».",
    "keywords": "🔤 <b>Які ключові слова?</b>\n\nВведи слово або фразу англійською. Можна додати ще або пропустити.",
}


def currency_markets(registry):
    groups = {}
    for source in registry.sources.values():
        currencies = {source.currencies.get(region) for region in source.regions}
        if len(currencies) != 1 or not currencies <= {"EUR", "GBP"}:
            raise UserError(f"Не вдалося визначити валюту для {source.label}.")
        groups.setdefault(next(iter(currencies)), []).append(source.label)
    return groups


def steps(draft):
    return [
        "brands",
        "models",
        "sizes",
        "price",
        "keywords",
        "review",
    ]


def new_filter_draft(registry):
    return {
        "kind": "native_filters",
        "step": "brands",
        "values": {},
        "bounds": {},
        "currencies": ["EUR"],
        "operation_id": str(uuid4()),
        "last_input_id": -1,
    }


def upgrade_draft(draft, registry):
    if draft.get("kind") == "native_filters":
        result = deepcopy(draft)
        if ":" in result["step"]:
            result["previous_draft"] = deepcopy(draft)
            result["step"] = "price"
        result["currencies"] = ["EUR"]
        return result
    result = new_filter_draft(registry)
    values = draft.get("values", {})
    result["values"] = {
        field: values.get(field, [])[:1] if field in SINGLE else values.get(field, [])
        for field in (*SINGLE, *MULTIPLE)
    }
    if values.get("price"):
        price = values["price"]
        result["bounds"][price["currency"]] = {
            key: price.get(key) for key in ("minimum", "maximum")
        }
    # Retain the original draft while asking the user to review the new single-value fields.
    result["previous_draft"] = deepcopy(draft)
    return result


def advance(draft):
    order = steps(draft)
    draft["step"] = order[min(order.index(draft["step"]) + 1, len(order) - 1)]


def check_bounds(bounds):
    try:
        return (
            PriceBounds.model_validate(bounds)
            if any(v is not None for v in bounds.values())
            else None
        )
    except ValueError:
        raise UserError("Ціна «до» має бути не меншою за ціну «від».") from None


def input_value(draft, text, update_id):
    result = deepcopy(draft)
    if update_id <= result["last_input_id"]:
        return result
    step = result["step"]
    if step in SINGLE:
        result["values"][step] = [english_value(text)]
        advance(result)
    elif step in MULTIPLE:
        additions = parse_sizes(text) if step == "sizes" else [english_value(text)]
        values = result["values"].setdefault(step, [])
        for value in additions:
            if value.casefold() not in [v.casefold() for v in values]:
                if len(values) >= 8:
                    raise UserError("Можна додати до 8 значень. Скороти список або натисни «Далі».")
                values.append(value)
    elif step == "price":
        price = parse_price(text, "EUR")
        result["bounds"] = {"EUR": price.model_dump(mode="json", exclude={"currency"})}
        advance(result)
    else:
        raise UserError("Перевір параметри та натисни «Зберегти» або «Змінити».")
    result["last_input_id"] = update_id
    return result


def change_step(draft, action):
    result = deepcopy(draft)
    step, order = result["step"], steps(result)
    if action == "back":
        result["step"] = order[max(0, order.index(step) - 1)]
    elif action == "change":
        result["step"] = "brands"
    elif action == "more" and step in MULTIPLE:
        pass
    elif action in {"next", "skip"} and step != "review":
        if step == "price":
            if action == "skip":
                result["bounds"] = {}
            elif not check_bounds(result["bounds"].get("EUR", {})):
                raise UserError("Введи суму або натисни «Пропустити».")
        elif action == "skip":
            result["values"].pop(step, None)
        advance(result)
    else:
        raise UserError("Ця кнопка застаріла. Скористайся поточним меню.")
    return result


def build_filters(draft, registry):
    bounds = check_bounds(draft["bounds"].get("EUR", {}))
    filters = Filters(
        **draft["values"],
        sources={key: source.regions for key, source in registry.sources.items()},
        follow_enabled_sources=True,
        price=Price(currency="EUR", **bounds.model_dump()) if bounds else None,
    )
    for key in filters.sources:
        registry.for_source(filters, key)
    return filters


def render_draft(draft, registry):
    step = draft["step"]
    if step in QUESTIONS:
        text = QUESTIONS[step]
        if draft["values"].get(step):
            text += "\n\nДодано: " + html(", ".join(draft["values"][step]), 700)
        rows = [[("Далі →", "f:next")]]
        if step in MULTIPLE:
            rows[0].insert(0, ("➕ Додати ще", "f:more"))
        rows += [[("Пропустити", "f:skip"), ("← Назад", "f:back")]]
    elif step == "price":
        text = PRICE_PROMPT
        rows = [[("Далі →", "f:next"), ("Пропустити", "f:skip")], [("← Назад", "f:back")]]
    else:
        filters = build_filters(draft, registry)
        text = "🔎 <b>Перевір параметри</b>\n\n" + filter_summary(filters, registry)
        rows = [[("✅ Зберегти", "save"), ("✏️ Змінити", "f:change")], [("← Назад", "f:back")]]
    if step != "review":
        for field, label in (("brands", "Бренд"), ("models", "Модель")):
            if field != step and draft["values"].get(field):
                text += f"\n{label}: " + html(draft["values"][field][0], 100)
        text += "\n\nПошук — на всіх увімкнених у тебе маркетплейсах."
    rows.append([("Скасувати", "cancel")])
    return text, rows
