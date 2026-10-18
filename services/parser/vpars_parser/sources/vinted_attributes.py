"""Observed Vinted attribute labels, shared without loading the HTML parser."""

import re

from vpars_shared.sizes import ONE_SIZE as ONE_SIZE

WAIST_SIZE = re.compile(r"w\s*(\d{2,3})(?:\s*/?\s*l\s*\d{2,3})?", re.IGNORECASE)
WAIST_IN_TEXT = re.compile(r"(?<!\w)" + WAIST_SIZE.pattern + r"(?!\w|[.,/-]\d)", re.IGNORECASE)


def waist_size(value: str) -> str | None:
    """Normalize explicit waist labels; the requested size does not constrain length."""
    match = WAIST_SIZE.fullmatch(value.strip())
    return f"w{int(match[1])}" if match else None


def item_waist_size(raw: str | None, title: str) -> str | None:
    """Prefer the size field, then an unambiguous explicit W label in the title."""
    for text in (raw or "", title):
        values = {f"w{int(match[1])}" for match in WAIST_IN_TEXT.finditer(text)}
        if values:
            return next(iter(values)) if len(values) == 1 else None
    return None


CONDITIONS = {
    "new with tags": "new with tags",
    "neuf avec étiquette": "new with tags",
    "nuovo con cartellino": "new with tags",
    "neu, mit etikett": "new with tags",
    "new without tags": "new without tags",
    "neuf sans étiquette": "new without tags",
    "nuovo senza cartellino": "new without tags",
    "very good": "very good",
    "très bon état": "very good",
    "ottime": "very good",
    "sehr gut": "very good",
    "good": "good",
    "bon état": "good",
    "buone": "good",
    "gut": "good",
    "satisfaisant": "satisfactory",
    "discrete": "satisfactory",
    "satisfactory": "satisfactory",
    # Observed DE "Neu" does not explicitly say whether tags are present.
}
