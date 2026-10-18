import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Protocol
from urllib.parse import urlsplit

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlockParagraph,
    InputRichMessage,
    RichTextBold,
    RichTextItalic,
)

from . import autobuy_availability as autobuy
from .domain import FIELDS, LABELS, Filters, SourceRegistry, conflict_notice, euro_price


def short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def html(text, limit=180):
    return escape(short(str(text), limit), quote=True)


def amount(value: Decimal) -> str:
    return (
        format(value, "f").rstrip("0").rstrip(".")
        if "." in format(value, "f")
        else format(value, "f")
    )


def filter_name(filters: dict) -> str:
    fields = (
        ("keywords",)
        if filters.get("keywords_all")
        else ("brands", "models", "sizes", "keywords", "conditions", "colors", "categories")
    )
    return (
        ", ".join(value for field in fields for value in filters.get(field, []))
        or "Без ключових слів"
    )


def filter_summary(filters: Filters, registry: SourceRegistry) -> str:
    notice = conflict_notice(filters)
    if euro_price(filters):
        # Saved legacy GBP values stay in storage, but EUR is now the common budget.
        from .domain import Price

        filters = filters.model_copy(update={
            "price": Price(currency="EUR", **euro_price(filters).model_dump(exclude={"currency"})),
            "native_prices": {},
        })
    if filters.keywords_all:
        lines = ["Ключові слова: " + html(", ".join(filters.keywords), 300)]
        if filters.excluded_keywords:
            lines.append("Виключення: " + html(", ".join(filters.excluded_keywords), 500))
        for currency, bounds in filters.native_prices.items():
            if bounds:
                low = amount(bounds.minimum) if bounds.minimum is not None else "0"
                high = amount(bounds.maximum) if bounds.maximum is not None else "∞"
                lines.append(f"Ціна {currency}: {low}–{high}")
            else:
                lines.append(f"Ціна {currency}: без обмежень")
        if filters.price:
            price = filters.price
            low = amount(price.minimum) if price.minimum is not None else "0"
            high = amount(price.maximum) if price.maximum is not None else "∞"
            lines.append(f"Ціна: {low}–{high} {price.currency}")
            if price.currency == "EUR":
                lines.append("Інші валюти — автоматично за курсом ЄЦБ")
        if notice:
            lines.append("⚠️ " + html(notice, 900))
        return "\n".join(lines)
    fields = ("brands", "models", "sizes") if filters.follow_enabled_sources else FIELDS
    lines = [
        f"{LABELS[field]}: {html(', '.join(getattr(filters, field)), 260)}"
        for field in fields
        if getattr(filters, field)
    ]
    if filters.excluded_keywords:
        lines.append("Виключення: " + html(", ".join(filters.excluded_keywords), 500))
    if filters.price:
        price = filters.price
        low = amount(price.minimum) if price.minimum is not None else "0"
        high = amount(price.maximum) if price.maximum is not None else "∞"
        lines.append(f"Ціна: {low}–{high} {price.currency}")
        if price.currency == "EUR":
            lines.append("Інші валюти — автоматично за курсом ЄЦБ")
    for currency, bounds in filters.native_prices.items():
        if bounds:
            low = amount(bounds.minimum) if bounds.minimum is not None else "0"
            high = amount(bounds.maximum) if bounds.maximum is not None else "∞"
            lines.append(f"Ціна {currency}: {low}–{high}")
        else:
            lines.append(f"Ціна {currency}: без обмежень")
    if filters.follow_enabled_sources and filters.keywords:
        lines.append("Ключові слова: " + html(", ".join(filters.keywords), 260))
    if filters.follow_enabled_sources:
        lines.append("Маркетплейси: усі ввімкнені в тебе")
    for key, regions in filters.sources.items():
        source = registry.sources.get(key)
        label = source.label if source else key
        if not source or len(source.regions) != 1:
            label += " · " + ", ".join(regions)
        lines.append(f"Маркетплейс: {html(label, 160)}")
    lines.append("Інші фільтри: без обмежень")
    if notice:
        lines.append("⚠️ " + html(notice, 900))
    return "\n".join(lines)


@dataclass(frozen=True)
class Conversion:
    amount: Decimal
    currency: str = "UAH"
    rate_at: datetime | None = None
    fetched_at: datetime | None = None
    provider: str | None = None


class CurrencyConverter(Protocol):
    async def to_uah(self, amount: Decimal, currency: str) -> Conversion | None: ...


class UnavailableCurrencyConverter:
    async def to_uah(self, amount: Decimal, currency: str) -> None:
        return None


async def optional_conversion(
    converter: CurrencyConverter, price: Decimal, currency: str, timeout: float = 0.4
) -> Conversion | None:
    try:
        async with asyncio.timeout(timeout):
            result = await converter.to_uah(price, currency)
            if (
                result
                and result.currency == "UAH"
                and result.amount.is_finite()
                and result.amount >= 0
                and result.amount <= Decimal("999999999999")
            ):
                return result
    except Exception:
        # A rate-provider outage never suppresses an original-price card. Do not log payloads.
        pass
    return None


@dataclass(frozen=True)
class Lot:
    title: str
    price: Decimal
    currency: str
    source_label: str
    region: str
    url: str
    search_name: str
    photo_url: str | None = None
    size: str | None = None
    condition: str | None = None
    color: str | None = None
    seller_name: str | None = None
    seller_rating: float | None = None
    seller_feedback_count: int | None = None
    brand: str | None = None


@dataclass(frozen=True)
class LotMessage:
    text: str
    keyboard: InlineKeyboardMarkup
    photo_url: str | None
    rich_message: InputRichMessage


def safe_url(value: str | None) -> str | None:
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            return value
    except ValueError:
        pass
    return None


def lot_keyboard(lot: Lot) -> InlineKeyboardMarkup:
    url = safe_url(lot.url)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Open", url=url),
                InlineKeyboardButton(text="Buy" if autobuy.AVAILABLE else autobuy.SOON,
                                     callback_data="lot:buy"),
            ]
        ]
        if url
        else []
    )


CONDITION_LABELS = {
    alias.casefold(): label
    for label, aliases in (
        (
            "New With Tags",
            ("neuf avec étiquette", "nuovo con cartellino", "neu, mit etikett", "neu mit etikett"),
        ),
        (
            "New Without Tags",
            (
                "neuf sans étiquette",
                "nuovo senza cartellino",
                "neu ohne etikett",
                "neu, ohne etikett",
            ),
        ),
        ("New", ("neu", "neuf", "nuovo")),
        ("Very Good", ("très bon état", "ottime", "sehr gut")),
        ("Good", ("bon état", "buone", "gut")),
        ("Satisfactory", ("satisfaisant", "discrete", "zufriedenstellend")),
    )
    for alias in (label, *aliases)
}

CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$", "UAH": "₴", "PLN": "zł"}


def cell(value: str | None, limit=22) -> str:
    # Keep marketplace values on one line before formatting.
    return short(" ".join((value or "").split()), limit) or "N/A"


def review_lines(lot: Lot) -> list[str]:
    count = (
        str(lot.seller_feedback_count)
        if lot.seller_feedback_count is not None and lot.seller_feedback_count >= 0
        else "N/A"
    )
    if (
        lot.seller_rating is None
        or not 0 <= lot.seller_rating <= 5
        or lot.seller_feedback_count == 0
    ):
        return [f"N/A ({short(count, 10)})"]
    rating = round(lot.seller_rating, 1)
    full = int(rating)
    lines = ["⭐️" * full + "☆" * (5 - full) + f" ({short(count, 10)})"]
    if rating != full:
        lines.append(f"{rating:.1f}/5")
    return lines


def format_lot(lot: Lot, conversion: Conversion | None = None, *, demo=False) -> LotMessage:
    """Six compact lines, bold icons and bold italic labels/values."""
    if not lot.price.is_finite() or not Decimal("0") <= lot.price <= Decimal("999999999999"):
        raise ValueError("Invalid original price")
    prefix = "🧪 <b>Demo — example data</b>\n\n" if demo else ""
    brand = cell(lot.brand)
    size = cell(lot.size).upper()
    if size.casefold() in {"one size", "taille unique", "taglia unica", "einheitsgröße"}:
        size = "One Size"
    region = cell(lot.region, 8).upper()
    region = "UK" if region == "GB" else region
    market = (
        f"Vinted.{region.title()}"
        if lot.source_label.casefold() == "vinted"
        else cell(lot.source_label)
    )
    condition = CONDITION_LABELS.get(cell(lot.condition, 80).casefold(), "N/A")
    currency = cell(lot.currency, 8).upper()
    price = f"{lot.price:.2f} {CURRENCY_SYMBOLS.get(currency, currency)}"
    rows = [
        ("👀", "Market", market),
        ("🥇", "Brand", brand),
        ("📏", "Size", size),
        ("😇", "Condition", condition),
        ("💰", "Price", price),
        ("✨", "Reviews", " · ".join(review_lines(lot))),
    ]
    text_parts = []
    for icon, label, value in rows:
        if text_parts:
            text_parts.append("\n")
        text_parts.append(RichTextBold(text=[icon, RichTextItalic(text=f"{label}: {value}")]))
    blocks = [InputRichBlockParagraph(text=text_parts)]
    if demo:
        blocks.insert(0, InputRichBlockParagraph(text="🧪 Demo — example data"))
    lines = [f"<b>{icon}<i>{label}: {escape(value)}</i></b>" for icon, label, value in rows]
    if (
        conversion
        and conversion.currency == "UAH"
        and conversion.amount.is_finite()
        and Decimal("0") <= conversion.amount <= Decimal("999999999999")
    ):
        lines.append(f"≈ {conversion.amount:.2f} UAH")
        blocks.append(InputRichBlockParagraph(text=lines[-1]))
    return LotMessage(
        prefix + "\n".join(lines),
        lot_keyboard(lot),
        safe_url(lot.photo_url),
        InputRichMessage(blocks=blocks, skip_entity_detection=True),
    )
