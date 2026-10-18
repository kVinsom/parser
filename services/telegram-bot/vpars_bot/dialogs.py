from copy import deepcopy
from uuid import uuid4

from .domain import (
    FIELDS,
    STEPS,
    Filters,
    SourceRegistry,
    UserError,
    english_value,
    parse_price,
    parse_sizes,
)


def new_draft(filters=None, search_id=None, version=None):
    return {
        "kind": "filters",
        "step": 0,
        "values": deepcopy(filters or {}),
        "search_id": search_id,
        "version": version,
        "operation_id": str(uuid4()),
        "currency": "EUR",
        "last_input_id": -1,
    }


def input_text(draft, text: str, update_id: int):
    result = deepcopy(draft)
    if update_id <= draft.get("last_input_id", -1):
        return result
    step = STEPS[result["step"]]
    if step in FIELDS:
        incoming = parse_sizes(text) if step == "sizes" else [english_value(text)]
        values = result["values"].get(step, [])
        for value in incoming:
            if value.casefold() not in [v.casefold() for v in values]:
                if len(values) >= 8:
                    raise UserError(
                        "Вже додано 8 альтернатив. Натисни «Далі» або прибери обмеження."
                    )
                values.append(value)
        result["values"][step] = values
    elif step == "price":
        result["values"]["price"] = parse_price(text, "EUR").model_dump(mode="json")
        result["values"].pop("native_prices", None)
    elif step == "regions":
        raise UserError("Вибери маркетплейси кнопками нижче.")
    else:
        raise UserError("Перевір параметри й натисни «Зберегти» або «Змінити».")
    result["last_input_id"] = update_id
    return result


def transition(draft, action: str, registry: SourceRegistry):
    result = deepcopy(draft)
    if result["values"].get("sources"):
        result["values"]["sources"] = registry.normalize_sources(result["values"]["sources"])
    step = STEPS[result["step"]]
    if action == "back":
        result["step"] = max(0, result["step"] - 1)
    elif action == "change":
        result["step"] = 0
    elif action == "more" and step in FIELDS:
        pass
    elif action.startswith("currency-") and step == "price":
        currency = action.removeprefix("currency-")
        if currency != "EUR":
            raise UserError("Тепер ціну вводимо в EUR; інші валюти перераховуються автоматично.")
        result["currency"] = currency
        result["values"].pop("price", None)
    elif action.startswith("region|") and step == "regions":
        _, source_id, region = action.split("|", 2)
        normalized = registry.normalize_sources({source_id: [region]})
        source_id, regions = next(iter(normalized.items()))
        region = regions[0]
        source = registry.sources.get(source_id)
        if source is None or region not in source.regions:
            raise UserError("Цей маркетплейс зараз недоступний.")
        sources = result["values"].setdefault("sources", {})
        regions = sources.setdefault(source_id, [])
        if region in regions:
            regions.remove(region)
        else:
            regions.append(region)
        if not regions:
            sources.pop(source_id)
    elif action in {"skip", "next"} and step != "review":
        if step == "regions" and not result["values"].get("sources"):
            raise UserError("Вибери хоча б один маркетплейс.")
        if action == "skip" and step != "regions":
            result["values"].pop(step, None)
            if step == "price":
                result["currency"] = "EUR"
                result["values"].pop("native_prices", None)
        if action == "next" and step == "price" and not result["values"].get("price"):
            raise UserError("Введи ціну або натисни «Пропустити».")
        result["step"] += 1
        if STEPS[result["step"]] == "review":
            registry.validate(Filters.model_validate(result["values"]))
    else:
        raise UserError("Ця кнопка застаріла. Відкрий поточний крок.")
    # Preserve step indexes for existing drafts. Hide unavailable empty fields,
    # but retain an old value's screen so the user can explicitly remove it.
    direction = -1 if action == "back" else 1
    return skip_unavailable(result, registry, direction)


def skip_unavailable(draft, registry, direction=1):
    result = deepcopy(draft)
    while 0 < result["step"] < len(STEPS) - 1:
        field = STEPS[result["step"]]
        sources = (
            registry.normalize_sources(result["values"]["sources"])
            if result["values"].get("sources")
            else registry.sources
        )
        if result["values"].get(field) or not all(
            field in registry.sources[key].unsupported for key in sources
        ):
            break
        result["step"] += direction
    return result
