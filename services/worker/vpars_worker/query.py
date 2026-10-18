"""Derive a broad Vinted query; every saved constraint is still checked locally."""

import unicodedata

from vpars_parser.sources.vinted_attributes import CONDITIONS, ONE_SIZE

from .matching import keyword_size


def source_text(filters):
    # Legacy lists are OR alternatives; selecting one of several loses valid lots.
    parts = [filters["brands"][0]] if len(filters.get("brands", [])) == 1 else []
    # A model stays a local predicate when a brand already anchors the catalog.
    # Otherwise brand-only and brand+model searches duplicate every regional
    # request and can push head polling beyond the one-minute delivery window.
    if not parts and len(filters.get("models", [])) == 1:
        parts.append(filters["models"][0])
    if not parts:
        keywords = filters.get("keywords", [])
        if filters.get("keywords_all"):
            for value in keywords:
                normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
                # Sizes/translated conditions may exist only in structured card data.
                # One textual anchor avoids requiring every attribute in the title.
                if (
                    normalized
                    and keyword_size(normalized) is None
                    and normalized not in CONDITIONS
                    and normalized not in ONE_SIZE
                ):
                    parts = [value]
                    break
        elif len(keywords) == 1:
            parts = keywords
    # The gateway limits bytes, not Python characters.
    return " ".join(" ".join(parts).split()).encode("utf-8")[:500].decode("utf-8", errors="ignore")
