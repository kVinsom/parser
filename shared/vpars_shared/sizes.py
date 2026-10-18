"""Unambiguous size labels; never infer age/category or EU/UK conversions."""

import re
import unicodedata
from decimal import Decimal

ONE_SIZE = {
    "one size",
    "one-size",
    "onesize",
    "os",
    "o/s",
    "osfa",
    "osfm",
    "taille unique",
    "taglia unica",
    "einheitsgröße",
    "einheitsgrösse",
    "einheitsgroesse",
    "універсальний",
    "єдиний розмір",
}
ALPHA_NAMES = {
    "extra small": "xs",
    "x-small": "xs",
    "small": "s",
    "medium": "m",
    "large": "l",
    "extra large": "xl",
    "x-large": "xl",
}
NUMBER = r"\d{1,3}(?:\.\d{1,2})?"


def normalized_size(raw: str | None) -> str | None:
    if not raw:
        return None
    value = " ".join(unicodedata.normalize("NFKC", raw).casefold().split()).replace(
        ",", "."
    )
    if value in ONE_SIZE:
        return "one size"
    value = ALPHA_NAMES.get(value, value)
    # Accept keyboard lookalikes only when the entire label is an alpha size.
    alpha = value.translate(str.maketrans({"с": "s", "м": "m", "л": "l", "х": "x"}))
    if numbered := re.fullmatch(r"([2-6])x([sl])", alpha):
        return "x" * int(numbered[1]) + numbered[2]
    if re.fullmatch(r"(?:x{0,6}[sl]|m)", alpha):
        return alpha
    if waist := re.fullmatch(r"w\s*(\d{2,3})(?:\s*/?\s*l\s*\d{2,3})?", value):
        return "w" + str(int(waist[1]))
    if qualified := re.fullmatch(r"(eu|eur|uk)\s*(" + NUMBER + ")", value):
        system = "eu" if qualified[1] == "eur" else qualified[1]
        return system + " " + format(Decimal(qualified[2]).normalize(), "f")
    if re.fullmatch(NUMBER, value):
        return format(Decimal(value).normalize(), "f")
    # Vinted clothing combines alpha + regional numbers. Preserve only its
    # explicit alpha identity; do not interpret 38/10 as numeric alternatives.
    if composite := re.fullmatch(
        r"([^/]+)\s*/\s*" + NUMBER + r"(?:\s*/\s*" + NUMBER + r")*", value
    ):
        base = normalized_size(composite[1])
        if base and re.fullmatch(r"(?:x{0,6}[sl]|m)", base):
            return base
    return None


SIZE_HELP = "Розмір не розпізнано. Приклади: S, M, XL, 2XL, W32, EU 42, UK 8, 42.5, One size. Дитячі вікові розміри та неоднозначні діапазони поки не підтримуються."
