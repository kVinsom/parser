from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import RichMessage
from sqlalchemy import select
from test_monitoring import Bot, Parser, due, lot, rich_photos, rows, start
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters
from vpars_notifier.photo_cache import PhotoCache, confirmed_photos
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute

from vpars_worker.db import Delivery, Job, SentLot, TelegramPhoto
from vpars_worker.monitor import Monitor


def confirmed(count, prefix="confirmed_photo_"):
    blocks = [{"type": "photo", "photo": [
        {"file_id": f"small_photo_{i}", "file_unique_id": f"small_{i}", "width": 90, "height": 90},
        {"file_id": f"{prefix}{i}", "file_unique_id": f"large_{i}", "width": 1000, "height": 1000},
    ]} for i in range(count)]
    return SimpleNamespace(message_id=501, rich_message=RichMessage(
        blocks=blocks if count == 1 else [{"type": "collage", "blocks": blocks}]))


def test_mapping_requires_exact_photo_count_and_keeps_largest_sizes():
    urls = [f"https://images1.vinted.net/{i}.jpg" for i in range(10)]
    assert list(confirmed_photos(confirmed(10), urls).values()) == [f"confirmed_photo_{i}" for i in range(10)]
    assert not confirmed_photos(confirmed(9), urls)
    assert not confirmed_photos(SimpleNamespace(message_id=501), urls)
    assert not confirmed_photos(confirmed(11), urls + ["extra"])


async def test_persistence_bot_url_isolation_expiry_capacity_and_invalidation(environment):
    store, _ = environment
    urls = [f"https://images1.vinted.net/{i}.jpg" for i in range(3)]
    cache = PhotoCache(store.sessions, 123, max_entries=2)
    await cache.remember(confirmed(3), urls)
    restored = PhotoCache(store.sessions, 123)
    mapping = await restored.resolve(urls)
    assert len(mapping) == 2
    assert not await PhotoCache(store.sessions, 456).resolve(urls)
    assert not await restored.resolve([url + "?version=2" for url in urls])
    url, file_id = next(iter(mapping.items()))
    await restored.invalidate({url: "different_file_id"})
    assert await restored.resolve([url]) == {url: file_id}
    await restored.invalidate({url: file_id})
    assert not await restored.resolve([url])
    async with store.sessions.begin() as session:
        for row in await session.scalars(select(TelegramPhoto)):
            row.expires_at = utcnow() - timedelta(seconds=1)
    assert not await restored.resolve(urls)


@pytest.mark.parametrize("brand", ["Nike", "Adidas", "Stone Island"])
async def test_two_recipients_reuse_confirmed_photos_after_notifier_restart(environment, brand):
    store, catalog = environment
    await catalog.register(202, 202, "second", "Second", 2, start=True)
    for recipient in (101, 202):
        await catalog.save(recipient, Filters(brands=[brand], sources={"vinted_fr": ["FR"]}), f"save-{recipient}")
        await store.control.start(recipient, f"start-{recipient}")
    await Monitor(store, Parser(items=[lot("999")])).once()
    await due(store, Job)
    photos = [f"https://images1.vinted.net/{i}.jpg" for i in range(13)]
    item = lot(title=f"{brand} Hoodie", brand=Attribute(raw=brand, normalized=brand.casefold()), photo_urls=photos)
    await Monitor(store, Parser(items=[item])).once()

    class ConfirmingBot(Bot):
        id = 123
        async def send_rich_message(self, chat_id, **kwargs):
            await super().send_rich_message(chat_id, **kwargs)
            return confirmed(10)

    bot = ConfirmingBot()
    await Notifier(store, bot).once()
    await Notifier(store, bot).once()  # New cache instance, same persistent metadata.
    assert len(bot.calls) == 2
    assert {call[1] for call in bot.calls} == {101, 202}
    assert rich_photos(bot.calls[0][2]) == photos[:10]
    assert rich_photos(bot.calls[1][2]) == [f"confirmed_photo_{i}" for i in range(10)]
    assert len(await rows(store, SentLot)) == 2
    assert not await Notifier(store, bot).once()


@pytest.mark.parametrize("stop", [False, True])
async def test_rejected_cached_photo_retries_original_full_gallery_and_rechecks_stop(environment, stop):
    store, _ = environment
    await start(environment)
    photos = [f"https://images1.vinted.net/{i}.jpg" for i in range(3)]
    await Monitor(store, Parser(items=[lot(photo_urls=photos)])).once()
    bot = Bot()
    bot.id = 123
    notifier = Notifier(store, bot)
    await notifier.photo_cache.remember(confirmed(3), photos)
    bot.error = TelegramBadRequest(method=SendMessage(chat_id=101, text="x"), message="wrong file identifier")
    await notifier.once()
    row = (await rows(store, Delivery))[0]
    assert row.state == "pending" and row.media_progress["photo_cache_bypass"]
    assert not row.media_progress.get("cover_only") and not row.text_only
    assert not await notifier.photo_cache.resolve(photos)
    bot.error = None
    if stop:
        await store.control.stop(101, "stop-before-original-url-retry")
    await due(store, Delivery)
    await Notifier(store, bot).once()
    assert len(bot.calls) == (1 if stop else 2)
    if not stop:
        assert rich_photos(bot.calls[-1][2]) == photos
        assert (await rows(store, Delivery))[0].state == "sent"


async def test_uncertain_send_does_not_learn_or_retry_cached_photos(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    bot = Bot()
    bot.id = 123
    bot.error = TelegramNetworkError(method=SendMessage(chat_id=101, text="x"), message="disconnected")
    await Notifier(store, bot).once()
    assert (await rows(store, Delivery))[0].state == "unknown"
    assert not await rows(store, TelegramPhoto)
    assert not await Notifier(store, bot).once()


async def test_cache_lookup_failure_keeps_original_delivery(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    class ConfirmingBot(Bot):
        id = 123
        async def send_rich_message(self, chat_id, **kwargs):
            await super().send_rich_message(chat_id, **kwargs)
            return confirmed(1)
    bot = ConfirmingBot()
    notifier = Notifier(store, bot)
    class BrokenSessions:
        def __call__(self):
            raise RuntimeError("unavailable")
        def begin(self):
            raise RuntimeError("unavailable")
    notifier.photo_cache.sessions = BrokenSessions()
    await notifier.once()
    assert rich_photos(bot.calls[0][2]) == lot().photo_urls
    assert (await rows(store, Delivery))[0].state == "sent"
    assert len(await rows(store, SentLot)) == 1
    assert not await notifier.once()


async def test_stop_while_cache_lookup_is_pending_prevents_send(environment):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    bot = Bot()
    notifier = Notifier(store, bot)
    async def resolve(urls):
        await store.control.stop(101, "stop-during-photo-cache")
        return {url: "confirmed_photo_0" for url in urls}
    notifier.photo_cache.resolve = resolve
    await notifier.once()
    assert not bot.calls
    assert (await rows(store, Delivery))[0].state == "cancelled"
