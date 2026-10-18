from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select, text
from test_monitoring import Bot, Parser, due, lot, rich_photos, rich_text, rows, start
from vpars_bot.db import ControlCommand, utcnow
from vpars_notifier.service import Notifier
from vpars_parser.models import Seller

from vpars_worker.db import Delivery, Job, SeenItem, migrate
from vpars_worker.freshness import freshness_error
from vpars_worker.monitor import Monitor, Store
from vpars_worker.parser import ParseFailure


@pytest.mark.parametrize(
    "offset,error",
    [
        (0, None),
        (1, None),
        (600, None),
        (-0.001, "DiscoveredBeforeSearch"),
        (601, "DiscoveryTimeInFuture"),
    ],
)
def test_discovery_after_search_boundary(offset, error):
    started = utcnow()
    now = started + timedelta(seconds=600)
    item = lot(published_at=None, first_seen_at=started + timedelta(seconds=offset))
    assert freshness_error(item, now, started) == error
    assert freshness_error(lot(first_seen_at=None), now, started) == "DiscoveryTimeUnknown"


async def test_baseline_restart_returned_item_and_partial_page(environment):
    store, _ = environment
    await start(environment)  # The initial item 999 is already remembered.
    assert not await rows(store, Delivery)
    assert len(await rows(store, SeenItem)) == 1
    await Monitor(store, Parser(items=[lot("999"), lot("10001")])).once()
    deliveries = await rows(store, Delivery)
    assert len(deliveries) == 1
    timestamp = deliveries[0].item["first_seen_at"]
    reopened = Store(store.sessions, store.registry)
    await due(store, Job)
    await Monitor(reopened, Parser(items=[])).once()
    await due(store, Job)
    await Monitor(reopened, Parser(items=[lot("999"), lot("10001")])).once()
    assert len(await rows(store, Delivery)) == 1
    assert (await rows(store, Delivery))[0].item["first_seen_at"] == timestamp

    # A partial first page cannot mark unseen old items as future discoveries.
    async with store.sessions.begin() as session:
        job = await session.scalar(select(Job))
        job.baseline_ready = False
    await due(store, Job)
    claimed = (await store.claim())[0]
    from vpars_parser.models import ParseRequest

    result = await Parser(items=[lot("10002")]).parse(ParseRequest(**claimed.query))
    result.completeness = "partial"
    await store.finish(claimed, result, interval=4, max_pages=3)
    assert not (await rows(store, Job))[0].baseline_ready
    assert len(await rows(store, Delivery)) == 1


@pytest.mark.parametrize("count", [0, 1, 2, 10, 11, 23])
async def test_first_ten_photos_card_and_buttons_are_one_message(environment, count):
    store, _ = environment
    await start(environment)
    photos = [f"https://images1.vinted.net/{i}.jpg" for i in range(count)]
    await Monitor(store, Parser(items=[lot(photo_urls=photos)])).once()
    bot = Bot()
    assert await Notifier(store, bot).once()
    assert [call[0] for call in bot.calls] == ["rich"]
    card = bot.calls[-1][2]
    assert rich_photos(card) == photos[:10]
    assert "Brand:" in rich_text(card) and "Stone Island" in rich_text(card)
    paragraphs = [block for block in card["rich_message"].blocks if block.type == "paragraph"]
    assert len(paragraphs) == 1 and paragraphs[0].text.count("\n") == 5
    assert [[b.text for b in row] for row in card["reply_markup"].inline_keyboard] == [
        ["Open", "Soon"]
    ]
    assert card["reply_markup"].inline_keyboard[0][0].url == lot().item_url
    assert (await rows(store, Delivery))[0].state == "sent"
    assert not await Notifier(store, bot).once()


@pytest.mark.parametrize("delay", [1, 90])
async def test_rate_limit_retries_only_while_young_and_restart_does_not_replay(
    environment, monkeypatch, delay
):
    import vpars_notifier.service as service

    store, _ = environment
    await start(environment)
    await Monitor(
        store,
        Parser(items=[lot(photo_urls=[f"https://images1.vinted.net/{i}" for i in range(23)])]),
    ).once()
    bot = Bot()
    bot.error = TelegramRetryAfter(
        method=SendMessage(chat_id=101, text="x"), message="rate", retry_after=delay
    )
    await due(store, Delivery)
    await Notifier(store, bot).once()
    row = (await rows(store, Delivery))[0]
    assert row.state == "pending" and not row.media_progress.get("transport_started")
    assert not row.media_progress.get("caption_sent")
    bot.error = None
    later = utcnow() + timedelta(seconds=delay)
    monkeypatch.setattr(service, "utcnow", lambda: later)
    await due(store, Delivery)
    await Notifier(store, bot).once()
    if delay > 60:
        assert len(bot.calls) == 1
        assert (await rows(store, Delivery))[0].state == "expired"
        assert not await Notifier(store, bot).once()
        return
    assert len(bot.calls) == 2
    assert [call[0] for call in bot.calls] == ["rich", "rich"]
    assert rich_photos(bot.calls[0][2]) == rich_photos(bot.calls[1][2])
    row = (await rows(store, Delivery))[0]
    assert row.media_progress["offset"] == 10 and row.state == "sent"
    assert not await Notifier(store, bot).once()


async def test_enriched_gallery_uses_first_ten_unique_photos(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(photos_complete=False)])).once()
    photos = [f"https://images1.vinted.net/{i}" for i in range(23)]
    seller = Seller(source_user_id="987", username="seller <&>", rating=4.9, feedback_count=190)

    class Details:
        async def details(self, item):
            return SimpleNamespace(
                photo_urls=[photos[0], *photos],
                photos_complete=True,
                published_at=None,
                seller=seller,
            )

    bot = Bot()
    await Notifier(store, bot, parser=Details()).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].item["seller"] == seller.model_dump(mode="json")
    await Notifier(store, bot).once()
    await Notifier(store, bot).once()
    assert [call[0] for call in bot.calls] == ["rich"]
    assert rich_photos(bot.calls[0][2]) == photos[:10]
    card = rich_text(bot.calls[-1][2])
    assert "seller" not in card
    assert "⭐️⭐️⭐️⭐️☆ (190)" in card and "4.9/5" in card
    assert (await rows(store, Delivery))[0].state == "sent"


@pytest.mark.parametrize("mode", ["known", "missing", "unavailable"])
async def test_cached_gallery_enriches_seller_without_endless_requests(environment, mode):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(photos_complete=True)])).once()

    class Details:
        calls = 0

        async def details(self, item):
            self.calls += 1
            if mode == "unavailable":
                raise ParseFailure("GatewayTimeout")
            return SimpleNamespace(
                photo_urls=item.photo_urls,
                photos_complete=True,
                published_at=None,
                seller=Seller(source_user_id="987", rating=4.8, feedback_count=51)
                if mode == "known"
                else None,
            )

    details, bot = Details(), Bot()
    for _ in range(4 if mode == "unavailable" else 2):
        await due(store, Delivery)
        await Notifier(store, bot, parser=details).once()
    assert details.calls == 1
    assert len(bot.calls) == 1
    caption = rich_text(bot.calls[0][2])
    assert ("⭐️⭐️⭐️⭐️☆ (51)" in caption and "4.8/5" in caption) == (mode == "known")
    assert "Full gallery unavailable" not in caption
    assert (await rows(store, Delivery))[0].state == "sent"


@pytest.mark.parametrize("offset", [1, 10, 20])
async def test_pre_limit_partial_delivery_never_sends_more_photos(environment, offset):
    store, _ = environment
    await start(environment)
    photos = [f"https://images1.vinted.net/{i}" for i in range(23)]
    await Monitor(store, Parser(items=[lot(photo_urls=photos)])).once()
    async with store.sessions.begin() as session:
        row = await session.scalar(select(Delivery))
        row.media_progress = {"offset": offset, "caption_sent": True}
    bot = Bot()
    await Notifier(store, bot).once()
    assert [call[0] for call in bot.calls] == ["rich"]
    assert rich_photos(bot.calls[0][2]) == photos[offset:10]
    row = (await rows(store, Delivery))[0]
    assert row.state == "sent" and row.media_progress["offset"] == max(offset, 10)


async def test_details_wait_stop_and_expiry_are_rechecked(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(photos_complete=False)])).once()
    bot = Bot()

    class Details:
        async def details(self, item):
            await store.control.stop(101, "stop-during-gallery")
            return SimpleNamespace(
                photo_urls=["https://images1.vinted.net/a"], photos_complete=True, published_at=None
            )

    await Notifier(store, bot, parser=Details()).once()
    await due(store, Delivery)
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "cancelled"


async def test_legacy_pending_delivery_without_discovery_is_never_sent(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    async with store.sessions.begin() as session:
        saved = await session.scalar(select(Delivery))
        saved.item = {
            k: v for k, v in saved.item.items() if k not in {"first_seen_at", "published_at"}
        }
    bot = Bot()
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "expired"


@pytest.mark.parametrize("count", [3, 13])
@pytest.mark.parametrize("cover_rejected", [False, True])
async def test_rejected_gallery_fallback_remains_one_message(environment, count, cover_rejected):
    store, _ = environment
    await start(environment)
    photos = [f"https://images1.vinted.net/{i}" for i in range(count)]
    await Monitor(store, Parser(items=[lot(photo_urls=photos)])).once()
    bot = Bot()
    bot.error = TelegramBadRequest(method=SendMessage(chat_id=101, text="x"), message="image")
    await Notifier(store, bot).once()  # Whole message definitely rejected; no photos sent.
    assert (await rows(store, Delivery))[0].media_progress["cover_only"]
    if not cover_rejected:
        bot.error = None
    await due(store, Delivery)
    await Notifier(store, bot).once()
    assert rich_photos(bot.calls[-1][2]) == photos[:1]
    if cover_rejected:
        bot.error = None
        await due(store, Delivery)
        await Notifier(store, bot).once()
    assert len(bot.calls) == (3 if cover_rejected else 2)
    assert rich_photos(bot.calls[-1][2]) == ([] if cover_rejected else photos[:1])
    assert "Some photos could not be delivered." in rich_text(bot.calls[-1][2])
    assert "Brand:" in rich_text(bot.calls[-1][2])
    assert bot.calls[-1][2]["reply_markup"].inline_keyboard[0][0].text == "Open"
    assert (await rows(store, Delivery))[0].state == "sent"


async def test_full_gallery_unavailable_retains_cover_with_honest_notice(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(photos_complete=False)])).once()

    class Details:
        async def details(self, item):
            raise ParseFailure("GatewayTimeout")

    bot = Bot()
    for _ in range(4):
        await due(store, Delivery)
        await Notifier(store, bot, parser=Details()).once()
    assert len(bot.calls) == 1
    assert "Full gallery unavailable" in rich_text(bot.calls[0][2])
    assert "N/A (N/A)" in rich_text(bot.calls[0][2])


async def test_enriched_lot_sent_before_loading_the_whole_queue(environment):
    store, _ = environment
    await start(environment)
    await Monitor(
        store,
        Parser(items=[lot("10001", photos_complete=False), lot("10002", photos_complete=False)]),
    ).once()

    class Details:
        calls = 0

        async def details(self, item):
            self.calls += 1
            return SimpleNamespace(
                photo_urls=item.photo_urls, photos_complete=True, published_at=None
            )

    details, bot = Details(), Bot()
    await Notifier(store, bot, parser=details).once()
    await Notifier(store, bot, parser=details).once()
    assert details.calls == 1 and len(bot.calls) == 1


async def test_stop_during_formatting_stops_send(environment, monkeypatch):
    import vpars_notifier.service as service

    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    original = service.prepare_lot

    async def prepare(item):
        card = await original(item)
        await store.control.stop(101, "stop-during-format")
        return card

    monkeypatch.setattr(service, "prepare_lot", prepare)
    bot = Bot()
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "cancelled"


async def test_migrate_v1_preserves_data(environment, monkeypatch):
    import vpars_worker.db as db

    store, _ = environment
    await start(environment)
    engine = store.sessions.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][db.SCHEMA]
    monkeypatch.setattr(db, "SCHEMA", schema)
    async with engine.begin() as connection:
        await connection.execute(
            text(f"ALTER TABLE {schema}.deliveries DROP COLUMN media_progress")
        )
        await connection.execute(
            text(f"ALTER TABLE {schema}.catalog_jobs DROP COLUMN baseline_ready")
        )
        await connection.execute(text(f"DROP TABLE {schema}.seen_items"))
        await connection.execute(text(f"INSERT INTO {schema}.schema_revision (id) VALUES (1)"))
    await migrate(engine)
    await migrate(engine)
    assert len(await rows(store, Job)) == 1
    assert not (await rows(store, Job))[0].baseline_ready


async def test_partial_attributes_still_remember_all_initial_identities(environment):
    from vpars_parser.models import ParseRequest

    store, _ = environment
    await start(environment)
    async with store.sessions.begin() as session:
        job = await session.scalar(select(Job))
        job.baseline_ready = False
    claimed = (await store.claim())[0]
    result = await Parser(items=[lot("10001")]).parse(ParseRequest(**claimed.query))
    result.completeness = "partial"
    result.observed_item_ids = ["10001", "10002"]
    result.identities_complete = True
    await store.finish(claimed, result, interval=4, max_pages=3)
    assert not await rows(store, Delivery)
    assert not (await rows(store, Job))[0].baseline_ready
    assert (await rows(store, Job))[0].coverage == "catchup_partial"
    # Retry the incomplete baseline page before any checkpoint may advance.
    await due(store, Job)
    await Monitor(store, Parser(items=[lot("10001"), lot("10002")])).once()
    assert (await rows(store, Job))[0].baseline_ready
    await due(store, Job)
    await Monitor(store, Parser(items=[lot("10002"), lot("10003")])).once()
    deliveries = await rows(store, Delivery)
    assert len(deliveries) == 1 and deliveries[0].item["source_item_id"] == "10003"


@pytest.mark.parametrize(
    "offset,expected",
    [(90, "sent"), (10, "expired"), (-1, "expired"), (3600, "expired"), (None, "expired")],
)
async def test_details_date_rejects_old_prestart_future_and_unknown_items(
    environment, offset, expected
):
    store, _ = environment
    await start(environment)
    async with store.sessions.begin() as session:
        command = await session.get(ControlCommand, (await rows(store, Job))[0].id)
        command.created_at = utcnow() - timedelta(seconds=120)
        started = command.created_at
    await Monitor(store, Parser(items=[lot(published_at=None, photos_complete=False)])).once()

    class Details:
        calls = 0

        async def details(self, item):
            self.calls += 1
            return SimpleNamespace(
                photo_urls=item.photo_urls,
                photos_complete=True,
                published_at=started + timedelta(seconds=offset) if offset is not None else None,
            )

    details, bot = Details(), Bot()
    await Notifier(store, bot, parser=details).once()
    assert not bot.calls  # Enrichment commits before delivery.
    for _ in range(4):
        await due(store, Delivery)
        await Notifier(store, bot, parser=details).once()
        if (await rows(store, Delivery))[0].state in {"sent", "expired"}:
            break
    assert details.calls == (3 if offset is None else 1)
    assert (await rows(store, Delivery))[0].state == expected
    assert len(bot.calls) == (1 if expected == "sent" else 0)


async def test_old_publication_is_not_queued_as_a_fresh_discovery(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=utcnow() - timedelta(days=1))])).once()
    assert not await rows(store, Delivery)


async def test_missing_publication_after_gallery_errors_cannot_fall_back_to_cover(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=None, photos_complete=False)])).once()

    class Details:
        async def details(self, item):
            raise ParseFailure("GatewayTimeout")

    bot = Bot()
    for _ in range(4):
        await due(store, Delivery)
        await Notifier(store, bot, parser=Details()).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "expired"


async def test_unknown_publication_with_complete_photos_requires_details(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    bot = Bot()
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "expired"


@pytest.mark.parametrize("tamper", ["legacy", "wrong_job", "before_start"])
async def test_only_current_discovery_provenance_can_deliver(environment, tamper):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    async with store.sessions.begin() as session:
        saved = await session.scalar(select(Delivery))
        candidate = dict(saved.candidates[0])
        if tamper == "legacy":
            candidate.pop("discovery_mode")
        elif tamper == "wrong_job":
            candidate["job_id"] = "missing"
        else:
            saved.item = {**saved.item, "first_seen_at": (utcnow() - timedelta(days=1)).isoformat()}
        saved.candidates = [candidate]
    bot = Bot()
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "expired"


async def test_cleared_old_queue_never_reactivates_after_another_baseline(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    await store.control.stop(101, "clear-stop")
    async with store.sessions.begin() as session:
        saved = await session.scalar(select(Delivery))
        saved.state = "cancelled"
        saved.last_error = "ClearedBeforeNewSearch"
    await store.control.start(101, "clear-new-start")
    await due(store, Job)
    # Claim cancels any obsolete job before it can advance the new generation.
    for _ in range(2):
        await Monitor(store, Parser(items=[lot("999")])).once()
    await due(store, Job)
    await Monitor(store, Parser(items=[lot(published_at=None)])).once()
    bot = Bot()
    await Notifier(store, bot).once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "cancelled"
