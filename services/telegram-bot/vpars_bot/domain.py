import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator
from vpars_shared.sizes import SIZE_HELP, normalized_size

from .exchange import ExchangeRates, ExchangeRateUnavailable

FIELDS = ("models", "sizes", "conditions", "colors", "brands", "keywords", "categories")
STEPS = (*FIELDS, "regions", "price", "review")
PRICE_PROMPT = (
    "💶 <b>Яка ціна в євро?</b>\n\n"
    "Введи від і до одним повідомленням: 20 80.\n"
    "Лише максимум: 80. Лише мінімум: 20 -.\n"
    "В інші валюти переведу автоматично за курсом ЄЦБ.\n"
    "Без обмежень — «Пропустити»."
)
LABELS = dict(
    zip(
        FIELDS,
        ("Модель", "Розмір", "Стан", "Колір", "Бренд", "Ключові слова", "Категорія"),
        strict=True,
    )
)


class UserError(Exception):
    """Short, safe text suitable for the private chat."""


def keyword_values(values: list[str]) -> list[str]:
    result = []
    for raw in values:
        value = " ".join(unicodedata.normalize("NFKC", raw).split())
        if not value or not re.search(r"\w", value) or len(value) > 100:
            raise UserError(
                "Введи слова через кому: Nike, hoodie, L. Кожне значення — до 100 символів."
            )
        value = jeans_size(value) or clothing_size(value) or value
        if value.casefold() not in [word.casefold() for word in result]:
            result.append(value)
    if not result:
        raise UserError("Введи хоча б одне ключове слово, наприклад: Nike, hoodie, L.")
    if len(result) > 8 or len(", ".join(result)) > 300:
        raise UserError("Введи до 8 значень через кому, разом до 300 символів.")
    return result


def parse_keywords(text: str) -> list[str]:
    return keyword_values(text.split(","))


def excluded_values(values: list[str]) -> list[str]:
    result = []
    for raw in values:
        value = " ".join(unicodedata.normalize("NFKC", raw).split()).strip(" .")
        if not value or not re.search(r"\w", value) or len(value) > 100:
            raise UserError("Введи виключені слова через кому: Fake, Hole, kids.")
        if value.casefold() not in [word.casefold() for word in result]:
            result.append(value)
    if len(result) > 20 or len(", ".join(result)) > 500:
        raise UserError("Введи до 20 виключень, разом до 500 символів.")
    return result


def keyword_conflicts(filters) -> list[str]:
    """Warn about identical phrases without rewriting any saved matching rules."""
    values = filters if isinstance(filters, dict) else filters.model_dump()

    def tokens(value):
        return tuple(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))

    excluded = {tokens(value) for value in values.get("excluded_keywords", [])}
    return [value for value in values.get("keywords", []) if tokens(value) in excluded]


def conflict_notice(filters) -> str:
    conflicts = keyword_conflicts(filters)
    if not conflicts:
        return ""
    return (
        "Є і в пошуку, і у виключеннях: "
        + ", ".join(conflicts)
        + ". Товари з цими словами в назві або описі будуть відхилені. Перевір умови."
    )


def english_value(text: str) -> str:
    value = " ".join(text.split())
    if not value or len(value) > 100 or not re.fullmatch(r"[\x20-\x7e]+", value):
        raise UserError("Введи значення англійською, до 100 символів.")
    if not any(c.isalnum() for c in value):
        raise UserError("Додай назву або число.")
    return value


def clothing_size(text: str) -> str | None:
    value = text.strip().upper().translate(str.maketrans({"С": "S", "М": "M", "Л": "L", "Х": "X"}))
    return value if re.fullmatch(r"(?:X{0,4}[SL]|M|[2-6]XL)", value) else None


def jeans_size(text: str) -> str | None:
    match = re.fullmatch(r"W\s*(\d{2,3})", text.strip(), flags=re.IGNORECASE)
    return f"W{int(match[1])}" if match else None


def normalize_size(text: str) -> str:
    known = normalized_size(text)
    if known:
        return (
            jeans_size(text)
            or clothing_size(text)
            or ("One size" if known == "one size" else known.upper())
        )
    # Keep legacy unsupported values readable. Saving/starting validates them
    # separately so old settings can be opened and corrected, not deleted.
    value = " ".join(unicodedata.normalize("NFKC", text).split())
    if not value or len(value) > 100:
        raise UserError(SIZE_HELP)
    return value


def parse_sizes(text: str) -> list[str]:
    result = []
    # A single fractional digit accepts localized shoe sizes (42,5). Two
    # adjacent whole sizes (42,43) remain two alternatives; dots are unambiguous.
    text = re.sub(r"(?<=\d),(?=\d(?:\D|$))", ".", text)
    for part in re.split(r"[,;\n]", text):
        if not part.strip():
            continue
        tokens = re.split(r"[\s/]+", part.strip())
        clothing = [jeans_size(token) or clothing_size(token) for token in tokens]
        result.extend(clothing if all(clothing) else [normalize_size(part)])
    if not result:
        raise UserError(
            "Введи розмір: W32 для джинсів, S, M, L, XL для одягу або EU 42 для взуття."
        )
    if any(normalized_size(value) is None for value in result):
        raise UserError(SIZE_HELP)
    return result


class PriceBounds(BaseModel):
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    @model_validator(mode="after")
    def check_bounds(self):
        for value in (self.minimum, self.maximum):
            if value is not None and (
                not value.is_finite()
                or value < 0
                or value > Decimal("999999999")
                or value.as_tuple().exponent < -2
            ):
                raise ValueError("Invalid price")
        if self.minimum is None and self.maximum is None:
            raise ValueError("At least one price bound is required")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Minimum exceeds maximum")
        return self


class Price(PriceBounds):
    currency: Literal["EUR", "GBP"]


def parse_price_bound(text: str) -> Decimal:
    try:
        return PriceBounds(minimum=Decimal(text.strip().replace(",", "."))).minimum
    except (ValueError, InvalidOperation):
        raise UserError(
            "Введи одне невід’ємне число, до 2 знаків після крапки. Наприклад: 20.50."
        ) from None


def parse_price(text: str, currency: str) -> Price:
    pieces = text.strip().replace(",", ".").replace("–", " ").replace("—", " ").split()
    if len(pieces) == 1:
        pieces = ["-", pieces[0]]
    if len(pieces) != 2:
        raise UserError("Введи дві межі через пробіл: 20 80. Без межі: - 80 або 20 -.")
    try:
        return Price(
            currency=currency,
            minimum=None if pieces[0] == "-" else Decimal(pieces[0]),
            maximum=None if pieces[1] == "-" else Decimal(pieces[1]),
        )
    except (ValueError, InvalidOperation):
        raise UserError(
            "Перевір ціну: невід’ємні числа, до 2 знаків після крапки; від ≤ до."
        ) from None


class Filters(BaseModel):
    models: list[str] = Field(default_factory=list)
    sizes: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    keywords_all: bool = False
    excluded_keywords: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    sources: dict[str, list[str]] = Field(default_factory=dict)
    price: Price | None = None
    # Current reply-menu filters follow the user's marketplace switches at every start.
    follow_enabled_sources: bool = False
    native_prices: dict[Literal["EUR", "GBP"], PriceBounds | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def values(self):
        self.excluded_keywords = excluded_values(self.excluded_keywords)
        for field in FIELDS:
            values = getattr(self, field)
            if field == "keywords" and self.keywords_all:
                self.keywords = keyword_values(values)
                continue
            if len(values) > 8:
                raise UserError("Для одного параметра можна ввести до 8 альтернатив.")
            normalize = normalize_size if field == "sizes" else english_value
            setattr(self, field, list(dict.fromkeys(normalize(v) for v in values)))
        if self.keywords_all and any(
            getattr(self, field) for field in FIELDS if field != "keywords"
        ):
            raise UserError("Додай бренд, модель і розмір до ключових слів.")
        if self.follow_enabled_sources:
            if len(self.brands) > 1 or len(self.models) > 1:
                raise UserError("В одному фільтрі може бути лише один бренд і одна модель.")
            if self.colors or self.categories or self.conditions:
                raise UserError("Ці параметри не використовуються в новому фільтрі.")
        if self.native_prices and self.price:
            raise UserError("Використовуй лише один спосіб задання ціни.")
        if not self.sources or any(not regions for regions in self.sources.values()):
            raise UserError("Вибери хоча б один маркетплейс.")
        return self


def euro_price(filters: Filters) -> PriceBounds | None:
    """Use existing EUR bounds without rewriting saved personal settings."""
    if filters.price and filters.price.currency == "EUR":
        return filters.price
    return filters.native_prices.get("EUR")


class Source(BaseModel):
    label: str
    provider: str | None = None
    regions: list[str]
    aliases: dict[str, str] = Field(default_factory=dict)
    unsupported: list[str] = Field(default_factory=list)
    supported: list[str] = Field(default_factory=list)
    currencies: dict[str, str] = Field(default_factory=dict)
    capabilities_verified: bool = False


class FilterCapability(BaseModel):
    status: Literal["supported", "unsupported", "unknown"]
    values: list[str] | None = None  # None means no verified vocabulary is available.


class CapabilityReport(BaseModel):
    source_id: str
    regions: list[str]
    filters: dict[str, FilterCapability]
    verified: bool = False


class CapabilityProvider(Protocol):
    async def describe(self, source_id: str, regions: list[str]) -> CapabilityReport: ...
    async def issues(self, filters: Filters) -> list[str]: ...


class SourceRegistry:
    def __init__(self, sources: dict[str, Source], *, exchange=None):
        if not sources or any(not re.fullmatch(r"[a-z0-9_-]{1,24}", s) for s in sources):
            raise ValueError("Invalid source configuration")
        for source_id, source in sources.items():
            if not source.regions or any(not re.fullmatch(r"[A-Z]{2}", r) for r in source.regions):
                raise ValueError("Invalid regions")
            if (
                source.provider == "vinted" or source_id.startswith("vinted")
            ) and "PL" in source.regions:
                raise ValueError("Vinted PL is disabled")
        self.sources = sources
        self.exchange = exchange if exchange is not None else ExchangeRates()

    @classmethod
    def read(cls, path: Path):
        return cls(
            {k: Source.model_validate(v) for k, v in json.loads(path.read_text("utf-8")).items()}
        )

    def normalize_sources(self, sources: dict[str, list[str]]) -> dict[str, list[str]]:
        """Read old grouped Vinted filters while writing separate marketplace IDs."""
        normalized = {}
        for key, regions in sources.items():
            if key == "vinted" and key not in self.sources:
                for region in regions:
                    region = "GB" if region.upper() == "UK" else region.upper()
                    target = f"vinted_{region.lower()}"
                    source = self.sources.get(target)
                    if (
                        source is None
                        or source.provider != "vinted"
                        or region not in source.regions
                    ):
                        raise UserError(
                            "Один із майданчиків Vinted недоступний. Зміни вибір маркетплейсів."
                        )
                    chosen = normalized.setdefault(target, [])
                    if region not in chosen:
                        chosen.append(region)
                continue
            source = self.sources.get(key)
            if source is None:
                raise UserError("Цей маркетплейс зараз недоступний.")
            values = list(dict.fromkeys(source.aliases.get(r.upper(), r.upper()) for r in regions))
            if any(v not in source.regions for v in values):
                raise UserError("Один із майданчиків недоступний. Зміни вибір маркетплейсів.")
            normalized[key] = list(dict.fromkeys([*normalized.get(key, []), *values]))
        return normalized

    def validate(self, filters: Filters) -> Filters:
        normalized = self.normalize_sources(filters.sources)
        return filters.model_copy(update={"sources": normalized})

    def region_label(self, source_id: str, region: str) -> str:
        source = self.sources[source_id]
        return source.label if len(source.regions) == 1 else f"{source.label} · {region}"

    def for_source(self, filters: Filters, source_id: str) -> Filters:
        source = self.sources[source_id]
        regions = source.regions if filters.follow_enabled_sources else filters.sources[source_id]
        price = filters.price
        euro = euro_price(filters)
        if filters.native_prices or euro:
            currencies = {source.currencies.get(region) for region in regions}
            if len(currencies) != 1 or not currencies <= {"EUR", "GBP"}:
                raise UserError(f"Не вдалося визначити валюту для {source.label}.")
            currency = next(iter(currencies))
            if euro:
                try:
                    price = Price(
                        currency=currency,
                        minimum=self.exchange.convert(euro.minimum, currency),
                        maximum=self.exchange.convert(euro.maximum, currency),
                    )
                except ExchangeRateUnavailable:
                    raise UserError(
                        f"Курс EUR → {currency} тимчасово недоступний. Спробуй трохи пізніше."
                    ) from None
            elif currency not in filters.native_prices and any(filters.native_prices.values()):
                raise UserError(
                    f"Додай межі ціни в {currency} для {source.label} або пропусти ціну в новому фільтрі."
                )
            else:
                bounds = filters.native_prices.get(currency)
                price = Price(currency=currency, **bounds.model_dump()) if bounds else None
        return self.validate(
            filters.model_copy(
                update={
                    "sources": {source_id: regions},
                    "price": price,
                    "native_prices": {},
                    "follow_enabled_sources": False,
                }
            )
        )

    async def describe(self, source_id: str, regions: list[str]) -> CapabilityReport:
        self.validate(Filters(sources={source_id: regions}))
        source = self.sources[source_id]
        return CapabilityReport(
            source_id=source_id,
            regions=regions,
            verified=source.capabilities_verified,
            filters={
                field: FilterCapability(
                    status="unsupported"
                    if field in source.unsupported
                    else "supported"
                    if source.capabilities_verified and field in source.supported
                    else "unknown"
                )
                for field in (*FIELDS, "price")
            },
        )

    async def issues(self, filters: Filters) -> list[str]:
        filters = self.validate(filters)
        issues = [
            f"{source.label}: {LABELS.get(field, field)} поки не підтримується."
            for key in filters.sources
            for source in [self.sources[key]]
            for field in source.unsupported
            if getattr(filters, field, None)
        ]
        if any(normalized_size(value) is None for value in filters.sizes):
            issues.append(SIZE_HELP)
        if filters.price:
            for key, regions in filters.sources.items():
                source = self.sources[key]
                for region in regions:
                    currency = source.currencies.get(region)
                    if currency and currency != filters.price.currency and not euro_price(filters):
                        issues.append(
                            f"{self.region_label(key, region)}: фільтр ціни має бути в {currency}. Конвертація фільтрів недоступна."
                        )
        return issues

    def notice(self) -> str:
        unavailable = [
            f"{s.label}: {', '.join(LABELS.get(f, f) for f in s.unsupported)}"
            for s in self.sources.values()
            if s.unsupported
        ]
        return "Можливості пошуку ще не підтверджені." + (
            " Поки недоступні: " + "; ".join(unavailable) + "." if unavailable else ""
        )


def normalize_username(value: str) -> str:
    name = value.strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,32}", name):
        raise UserError("Введи @username користувача.")
    return name
