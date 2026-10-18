from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import utcnow
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery
from vpars_worker.freshness import publication_age_error, young_relative_label
from vpars_worker.monitor import Monitor, Store


@pytest.mark.parametrize(
    "policy",
    [
        {"one_minute_user_ids": [101]},
        {"applies_to": "selected_users", "max_age_seconds": 60},
        {"applies_to": "all_users", "max_age_seconds": 600},
        {"applies_to": "all_users", "max_age_seconds": 60, "one_minute_user_ids": [101]},
        {},
        None,
    ],
)
def test_runtime_rejects_a_policy_that_exempts_recipients(policy):
    from vpars_worker.runtime import validate_publication_policy

    with pytest.raises(ValueError):
        validate_publication_policy(policy)


def test_deployed_policy_protects_all_recipients():
    import json

    from vpars_worker.runtime import ROOT, validate_publication_policy

    validate_publication_policy(json.loads((ROOT / "publication_policy.json").read_text()))


def test_conflict_in_details_cannot_be_masked_by_known_catalog_publication():
    assert (
        publication_age_error(lot(), utcnow(), {"publication_evidence": "conflicting_dates"})
        == "PublicationDatesConflict"
    )


@pytest.mark.parametrize(
    "uploaded,expected",
    [("Just a moment ago", "sent"), ("1 minute ago", "expired"), ("16 ore fa", "expired")],
)
async def test_api_without_age_then_html_uploaded_reaches_delivery(
    environment, monkeypatch, uploaded, expected
):
    import importlib.util
    import json
    import sys
    from pathlib import Path

    # This optional cross-service test uses the parser's installed DOM library.
    # Production worker only imports contracts and remains browser/DOM independent.
    if importlib.util.find_spec("selectolax") is None:
        package = Path(__file__).resolve().parents[2] / "parser/.venv/Lib/site-packages/selectolax"
        if not package.exists():
            pytest.skip("Cross-service check needs parser's selectolax environment")
        spec = importlib.util.spec_from_file_location("selectolax", package / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "selectolax", module)
        spec.loader.exec_module(module)

    from vpars_parser.models import ItemDetailsRequest
    from vpars_parser.ports import GatewayPage
    from vpars_parser.service import ParserService
    from vpars_parser.settings import Settings

    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    calls = []

    class Gateway:
        async def fetch_item(self, request):
            calls.append(request.force_html)
            url = "https://www.vinted.fr/items/10001"
            if request.force_html:
                html = (
                    '<div data-testid="item-attributes-upload_date">'
                    f'<div itemprop="upload_date">{uploaded}</div></div>'
                ).encode()
                return GatewayPage("FR", url, url, "PAGE_LOADED", html, 200)
            url = "https://www.vinted.fr/api/v2/items/10001/details"
            data = json.dumps(
                {
                    "code": 0,
                    "item": {
                        "id": "10001",
                        "description": "Good",
                        "photos": [{"url": "https://images1.vinted.net/a.webp"}],
                    },
                }
            ).encode()
            return GatewayPage("FR", url, url, "PAGE_LOADED", b"", 200, data)

        async def close(self):
            pass

    service = ParserService(Gateway(), Settings())

    class Details:
        async def details(self, item):
            return await service.details(ItemDetailsRequest(region_id="FR", source_item_id="10001"))

    bot = Bot()
    notifier = Notifier(store, bot, parser=Details())
    try:
        for _ in range(4):
            await notifier.once()
            if (await rows(store, Delivery))[0].state in ("sent", "expired"):
                break
        assert calls == [False, True]
        assert len(bot.calls) == (expected == "sent")
        saved = (await rows(store, Delivery))[0]
        assert saved.state == expected
        assert saved.media_progress["publication_evidence"] == "relative:" + uploaded
        if expected == "expired":
            assert saved.last_error == "PublicationAgeNotAllowed"
    finally:
        await service.close()


@pytest.mark.parametrize(
    "label",
    [
        "just now",
        "a moment ago",
        "a few moments ago",
        "just a moment ago",
        "just a moment",
        "a few seconds ago",
        "0 min ago",
        "less than a minute ago",
        "59 seconds ago",
        "à l’instant",
        "A l'instant",
        "il y a moins d'une minute",
        "il y a 30 secondes",
        "gerade eben",
        "wenigen Sek.",
        "vor weniger als einer Minute",
        "vor 45 Sekunden",
        "pochi secondi fa",
        "Proprio adesso",
        "meno di un minuto fa",
        "un attimo fa",
        "щойно",
        "менше хвилини тому",
    ],
)
def test_only_explicit_young_uploaded_labels_are_allowed(label):
    assert young_relative_label("relative:" + label)


@pytest.mark.parametrize(
    "label",
    [
        "1 min ago",
        "1 minute ago",
        "a minute ago",
        "il y a 1 minute",
        "il y a une minute",
        "vor einer Minute",
        "1 Min.",
        "un minuto fa",
        "1 minuto fa",
        "1 хв тому",
        "хвилину тому",
        "60 seconds ago",
        "il y a 60 secondes",
        "vor 60 Sekunden",
        "60 secondi fa",
        "11 hours ago",
        "16 ore fa",
        "2 min ago",
        "11 min ago",
        "61 seconds ago",
        "19 min ago",
        "3 days ago",
        "il y a 2 minutes",
        "2 Min.",
        "1 h",
        "un'ora fa",
        "2 minuti fa",
        "Last seen just now",
        "Just a moment while we process your payment",
        "tomorrow",
        "1 min ago (edited 3 days ago)",
        "unknown",
        "",
        "-1 min ago",
    ],
)
def test_old_unknown_or_unrelated_text_cannot_confirm_publication_age(label):
    assert not young_relative_label("relative:" + label)


@pytest.mark.parametrize(
    "age,expected",
    [
        (-1, "PublicationTimeInFuture"),
        (0, None),
        (59.999, None),
        (60, "PublicationTooOld"),
        (60.001, "PublicationTooOld"),
    ],
)
def test_exact_age_boundary(age, expected):
    now = utcnow()
    assert (
        publication_age_error(lot(published_at=now - timedelta(seconds=age)), now, {}) == expected
    )


def test_relative_evidence_must_be_recent_and_timestamped():
    now = utcnow()
    item = lot(published_at=None, first_seen_at=now)
    progress = {
        "publication_checked": True,
        "publication_evidence": "relative:just now",
        "publication_checked_at": now.isoformat(),
    }
    assert publication_age_error(item, now, {}) == "PublicationCheckRequired"
    assert publication_age_error(item, now, progress) is None
    assert (
        publication_age_error(item, now + timedelta(seconds=5), progress)
        == "PublicationCheckRequired"
    )
    assert (
        publication_age_error(item, now + timedelta(seconds=60), progress)
        == "FreshnessQueueExpired"
    )
    assert (
        publication_age_error(item, now, {**progress, "publication_checked_at": None})
        == "PublicationAgeUnknown"
    )


@pytest.mark.parametrize(
    "label", ["59 seconds ago", "il y a 59 secondes", "vor 59 Sekunden", "59 secondi fa"]
)
def test_numeric_seconds_snapshot_cannot_outlive_the_minute(label):
    now = utcnow()
    item = lot(published_at=None, first_seen_at=now)
    progress = {
        "publication_checked": True,
        "publication_evidence": "relative:" + label,
        "publication_checked_at": now.isoformat(),
    }
    assert publication_age_error(item, now, progress) is None
    assert publication_age_error(item, now + timedelta(seconds=0.999), progress) is None
    assert (
        publication_age_error(item, now + timedelta(seconds=1), progress)
        == "PublicationCheckRequired"
    )


@pytest.mark.parametrize(
    "label,expected",
    [
        ("relative:just now", "sent"),
        ("relative:1 min ago", "expired"),
        ("relative:60 seconds ago", "expired"),
        ("relative:2 min ago", "expired"),
        ("relative:3 days ago", "expired"),
        ("unknown", "expired"),
        ("conflicting_dates", "expired"),
    ],
)
@pytest.mark.parametrize("recipient_user", [101, 202])
async def test_even_complete_gallery_requires_publication_verification(
    environment, label, expected, recipient_user
):
    store, catalog = environment
    if recipient_user != 101:
        await catalog.register(recipient_user, recipient_user, "newuser", "New", 2, start=True)
    await start(environment, user=recipient_user)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    # Model a legacy enriched row from before the new policy.
    async with store.sessions.begin() as session:
        row = await session.scalar(select(Delivery))
        row.media_progress = {"seller_checked": True, "description_checked": True}

    class Details:
        calls = 0

        async def details(self, item):
            self.calls += 1
            return SimpleNamespace(
                photo_urls=item.photo_urls,
                photos_complete=True,
                published_at=None,
                publication_evidence=label,
                fetched_at=utcnow(),
            )

    details, bot = Details(), Bot()
    # Reopening the store must enforce the same rule without a recipient allowlist.
    notifier = Notifier(Store(store.sessions, store.registry), bot, parser=details)
    await notifier.once()
    assert details.calls == 1 and not bot.calls
    for _ in range(4):
        await due(store, Delivery)
        await notifier.once()
        if (await rows(store, Delivery))[0].state in {"sent", "expired"}:
            break
    saved = (await rows(store, Delivery))[0]
    assert saved.state == expected
    assert len(bot.calls) == (expected == "sent")
    assert saved.media_progress["publication_evidence"] == label
    assert details.calls == (3 if label == "unknown" else 1)


async def test_new_user_old_catalog_lot_is_collected_but_not_queued(environment):
    from vpars_bot.db import ControlCommand

    from vpars_worker.db import Job, SeenItem

    store, catalog = environment
    await catalog.register(202, 202, "newuser", "New", 2, start=True)
    await start(environment, user=202)
    async with store.sessions.begin() as session:
        job = await session.scalar(select(Job))
        command = await session.get(ControlCommand, job.id)
        command.created_at = utcnow() - timedelta(hours=4)
    await Monitor(store, Parser(items=[lot(published_at=utcnow() - timedelta(hours=3))])).once()
    assert not await rows(store, Delivery)
    seen = next(row for row in await rows(store, SeenItem) if row.item_id == "10001")
    assert seen.item and seen.decision["code"] == "PublicationTooOld"


async def test_old_queue_expires_without_loading_photos_or_sending(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    async with store.sessions.begin() as session:
        row = await session.scalar(select(Delivery))
        row.item = {**row.item, "first_seen_at": (utcnow() - timedelta(seconds=61)).isoformat()}
    bot = Bot()
    await Notifier(store, bot).once()
    assert not bot.calls
    saved = (await rows(store, Delivery))[0]
    assert saved.state == "expired" and saved.last_error == "FreshnessWindowExpired"


async def test_age_is_checked_again_after_formatting(environment, monkeypatch):
    import vpars_notifier.service as service

    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    original = service.prepare_lot

    async def format_and_age(item):
        card = await original(item)
        later = utcnow() + timedelta(seconds=61)
        monkeypatch.setattr(service, "utcnow", lambda: later)
        return card

    monkeypatch.setattr(service, "prepare_lot", format_and_age)
    bot = Bot()
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "expired"


async def test_batch_expiry_covers_all_recipients_and_preserves_inflight_sends(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    now = utcnow()
    async with store.sessions.begin() as session:
        fresh = await session.scalar(select(Delivery))
        old_item = {
            **fresh.item,
            "published_at": None,
            "first_seen_at": (now - timedelta(minutes=5)).isoformat(),
        }
        for index in range(30):
            session.add(
                Delivery(
                    id=f"{index:064x}",
                    user_id=101,
                    item=old_item,
                    candidates=[],
                    next_due=now - timedelta(minutes=5),
                )
            )
        session.add(
            Delivery(
                id="f" * 64,
                user_id=202,
                item=old_item,
                candidates=[],
                next_due=now + timedelta(minutes=1),
            )
        )
        session.add(
            Delivery(
                id="e" * 64,
                user_id=101,
                item=old_item,
                candidates=[],
                state="sending",
                lease_id="owned",
                lease_until=now + timedelta(minutes=1),
            )
        )
        session.add(
            Delivery(
                id="d" * 64, user_id=101, item=old_item, candidates=[], state="sent", message_id=999
            )
        )
    claimed = await Notifier(store, Bot()).claim()
    assert claimed.id == fresh.id
    saved = {d.id: d for d in await rows(store, Delivery)}
    assert sum(d.state == "expired" for d in saved.values()) == 31
    assert saved["f" * 64].state == "expired"
    assert saved["f" * 64].last_error == "FreshnessWindowExpired"
    assert saved["e" * 64].state == "sending" and saved["e" * 64].lease_id == "owned"
    assert saved["d" * 64].state == "sent" and saved["d" * 64].message_id == 999


async def test_relative_label_ages_during_formatting_and_is_refreshed(environment, monkeypatch):
    import vpars_notifier.service as service

    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    now = utcnow()
    async with store.sessions.begin() as session:
        row = await session.scalar(select(Delivery))
        row.media_progress = {
            "seller_checked": True,
            "publication_checked": True,
            "publication_evidence": "relative:just now",
            "publication_checked_at": now.isoformat(),
        }
    original = service.prepare_lot

    async def format_and_age(item):
        card = await original(item)
        monkeypatch.setattr(service, "utcnow", lambda: now + timedelta(seconds=6))
        return card

    class Details:
        async def details(self, item):
            return SimpleNamespace(
                photo_urls=item.photo_urls,
                photos_complete=True,
                published_at=None,
                publication_evidence="relative:1 min ago",
                fetched_at=now + timedelta(seconds=6),
            )

    monkeypatch.setattr(service, "prepare_lot", format_and_age)
    bot = Bot()
    notifier = Notifier(store, bot, parser=Details())
    await notifier.once()
    assert not bot.calls and (await rows(store, Delivery))[0].state == "pending"
    await notifier.once()  # Refresh an out-of-date relative Uploaded label.
    await notifier.once()
    assert not bot.calls and (await rows(store, Delivery))[0].state == "expired"
