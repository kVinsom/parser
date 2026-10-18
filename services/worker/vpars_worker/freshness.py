"""New discoveries plus a mandatory publication-age check before delivery."""

import re
import unicodedata
from datetime import UTC, datetime

MAX_AGE_SECONDS = 60
RELATIVE_CHECK_TTL_SECONDS = 5


def young_relative_label(evidence):
    """Allow only explicit Uploaded labels indicating less than one minute.

    Minute labels are rounded by Vinted: this is a displayed-age policy, not
    an invented exact timestamp. Do not search arbitrary page/title text.
    """
    if not isinstance(evidence, str) or not evidence.startswith("relative:"):
        return False
    value = unicodedata.normalize("NFKC", evidence.removeprefix("relative:")).casefold()
    value = " ".join(value.replace("’", "'").split()).rstrip(".")
    if value in {
        "just now",
        "a moment ago",
        "a few moments ago",
        "moments ago",
        "just a moment ago",
        "just a moment",
        "a few seconds ago",
        "seconds ago",
        "less than a minute ago",
        "à l'instant",
        "a l'instant",
        "il y a un instant",
        "il y a quelques secondes",
        "il y a moins d'une minute",
        "moins d'une minute",
        "gerade",
        "gerade eben",
        "vor wenigen sekunden",
        "wenigen sek",
        "vor weniger als einer minute",
        "soeben",
        "adesso",
        "proprio ora",
        "proprio adesso",
        "un attimo fa",
        "un momento fa",
        "pochi secondi fa",
        "meno di un minuto fa",
        "щойно",
        "менше хвилини тому",
    }:
        return True
    # Full matches prevent confusing 11 minutes, future times, or unrelated text.
    return any(
        re.fullmatch(pattern, value)
        for pattern in (
            r"0 (?:min(?:ute)?s?)(?: ago)?",
            r"(?:il y a )?0 (?:min|minutes?)",
            r"(?:vor )?0 (?:min\.?|minuten?)",
            r"0 (?:min|minut[oi]) fa",
            r"0 хв(?:илин[аиу])?\.? (?:тому)",
            r"[0-5]?\d (?:sec(?:ond)?s?) ago",
            r"il y a [0-5]?\d (?:s|secondes?)",
            r"(?:vor )?[0-5]?\d (?:sek\.?|sekunden?)",
            r"[0-5]?\d second[oi] fa",
        )
    )


def relative_snapshot_ttl(evidence):
    """Bound an accepted displayed-age snapshot without inventing a publication date."""
    evidence = (evidence or "").removeprefix("relative:")
    match = re.fullmatch(
        r"(?:il y a |vor )?(\d+) (?:s|seconds?|secondes?|secs?|second[oi]|sek\.?|sekunden?)"
        r"(?: ago| fa)?\.?",
        evidence.casefold().strip(),
    )
    return (
        min(RELATIVE_CHECK_TTL_SECONDS, max(0, 60 - int(match[1])))
        if match
        else RELATIVE_CHECK_TTL_SECONDS
    )


def publication_age_error(item, now, progress):
    """None means eligible, CheckRequired means fetch, other codes mean reject."""
    if progress.get("publication_evidence") == "conflicting_dates":
        return "PublicationDatesConflict"
    if item.published_at is not None:
        age = (now - item.published_at).total_seconds()
        if age < 0:
            return "PublicationTimeInFuture"
        if age >= MAX_AGE_SECONDS:
            return "PublicationTooOld"
        return None
    # Being observed does not prove youth, but a long queue wait proves that the
    # listing cannot still meet the new short delivery window. Drain it without I/O.
    if item.first_seen_at and (now - item.first_seen_at).total_seconds() >= MAX_AGE_SECONDS:
        return "FreshnessQueueExpired"
    if not progress.get("publication_checked"):
        return "PublicationCheckRequired"
    evidence = progress.get("publication_evidence")
    if evidence in (None, "unknown"):
        return "PublicationAgeUnknown"
    if evidence == "conflicting_dates":
        return "PublicationDatesConflict"
    if not young_relative_label(evidence):
        return "PublicationAgeNotAllowed"
    try:
        checked = datetime.fromisoformat(progress["publication_checked_at"])
        if checked.tzinfo is None:
            return "PublicationAgeUnknown"
    except (KeyError, TypeError, ValueError):
        return "PublicationAgeUnknown"
    elapsed = (now - checked).total_seconds()
    if elapsed < 0:
        return "PublicationTimeInFuture"
    if elapsed >= relative_snapshot_ttl(evidence):
        return "PublicationCheckRequired"
    return None


def freshness_error(item, now, started_at):
    """Baseline membership is checked separately using durable job provenance."""
    if item.first_seen_at is None:
        return "DiscoveryTimeUnknown"
    discovered = item.first_seen_at.astimezone(UTC)
    if discovered < started_at.astimezone(UTC):
        return "DiscoveredBeforeSearch"
    if discovered > now.astimezone(UTC):
        return "DiscoveryTimeInFuture"
    if item.published_at is not None:
        published = item.published_at.astimezone(UTC)
        if published < started_at.astimezone(UTC):
            return "PublishedBeforeSearch"
        if published > now.astimezone(UTC):
            return "PublicationTimeInFuture"
    return None
