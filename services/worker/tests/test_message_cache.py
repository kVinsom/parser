import asyncio
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import delete, select, text
from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import utcnow
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery, Job, SentLot
from vpars_worker.message_cache import message_key
from vpars_worker.monitor import Monitor, Store


def regional_lot(region, identity="10001"):
    return lot(
        identity,
        region_id=region,
        source_namespace=f"vinted:{region}",
        item_url=f"https://www.vinted.{region.lower()}/items/{identity}-test",
    )


async def queue_two_regions(environment):
    store, catalog = environment
    await catalog.save(
        101,
        Filters(
            keywords=["Stone Island"],
            keywords_all=True,
            sources={"vinted_fr": ["FR"], "vinted_de": ["DE"]},
        ),
        "two-countries",
    )
    await store.control.start(101, "start-two-countries")
    for region in ("FR", "DE"):
        await Monitor(store, Parser(items=[regional_lot(region, "999")])).once(region=region)
    await due(store, Job)
    for region in ("FR", "DE"):
        await Monitor(store, Parser(items=[regional_lot(region)])).once(region=region)
    assert len(await rows(store, Delivery)) == 2


def test_identity_ignores_vinted_country_but_not_user_source_or_item():
    fr, de = regional_lot("FR").model_dump(), regional_lot("DE").model_dump()
    key = message_key(101, fr)
    assert message_key(101, de) == key
    assert message_key(202, fr) != key
    assert message_key(101, {**fr, "source_item_id": "10002"}) != key
    other = {**fr, "source_id": "other", "source_namespace": "other:FR"}
    assert message_key(101, other) != key
    assert message_key(101, other) != message_key(101, {**other, "source_namespace": "other:DE"})


async def test_queued_country_duplicates_send_one_message_and_store_confirmation(environment):
    store, _ = environment
    await queue_two_regions(environment)
    bot = Bot()
    await Notifier(store, bot).once()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1
    assert sorted(d.state for d in await rows(store, Delivery)) == ["duplicate", "sent"]
    cached = (await rows(store, SentLot))[0]
    assert cached.user_id == 101 and cached.source_item_id == "10001"
    assert cached.message_id == 501 and cached.sent_at is not None


async def test_concurrent_country_senders_cannot_both_pass_cache_check(environment):
    store, _ = environment
    await queue_two_regions(environment)
    sending, release = asyncio.Event(), asyncio.Event()

    class SlowBot(Bot):
        async def send_rich_message(self, *args, **kwargs):
            sending.set()
            await release.wait()
            return await super().send_rich_message(*args, **kwargs)

    bot = SlowBot()
    first = asyncio.create_task(Notifier(store, bot).once())
    try:
        await asyncio.wait_for(sending.wait(), 3)
        await Notifier(store, bot).once()  # A different regional row, same lot lock.
    finally:
        release.set()
        await first
    await due(store, Delivery)
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1 and len(await rows(store, SentLot)) == 1


async def test_unsent_rate_limited_lot_is_not_cached(environment):
    store, _ = environment
    await queue_two_regions(environment)
    bot = Bot()
    bot.error = TelegramRetryAfter(
        method=SendMessage(chat_id=101, text="x"),
        message="rate",
        retry_after=90,
    )
    await Notifier(store, bot).once()
    assert not await rows(store, SentLot)
    bot.error = None
    await Notifier(store, bot).once()  # Other country can send after a definite rejection.
    await due(store, Delivery)
    await Notifier(store, bot).once()
    assert len(bot.calls) == 2  # One rejected request, one accepted message.
    assert len(await rows(store, SentLot)) == 1
    assert sorted(d.state for d in await rows(store, Delivery)) == ["duplicate", "sent"]


async def test_cache_survives_queue_cleanup_new_search_and_worker_restart(environment):
    store, catalog = environment
    first_filter = await start(environment)
    await Monitor(store, Parser(items=[lot()])).once()
    bot = Bot()
    await Notifier(store, bot).once()
    async with store.sessions.begin() as session:
        await session.execute(delete(Delivery))  # History must be independent of the outbox.
    await catalog.archive(101, first_filter.id, True, first_filter.version, "archive-old")
    reopened = Store(store.sessions, store.registry)
    await start((reopened, catalog), key="new-filter")
    await Monitor(reopened, Parser(items=[lot(), lot("10002")])).once()
    assert [d.item["source_item_id"] for d in await rows(store, Delivery)] == ["10002"]
    await Notifier(reopened, bot).once()
    assert len(bot.calls) == 2 and len(await rows(store, SentLot)) == 2


async def test_notification_cache_is_per_recipient(environment):
    store, catalog = environment
    await start(environment)
    await catalog.register(202, 202, "bob", "Bob", 2, start=True)
    await start(environment, user=202, key="bob")
    await Monitor(store, Parser()).once()
    bot = Bot()
    for _ in range(2):
        await Notifier(store, bot).once()
    assert sorted(c[1] for c in bot.calls) == [101, 202]
    assert {c.user_id for c in await rows(store, SentLot)} == {101, 202}


@pytest.mark.parametrize("version", [1, 2])
async def test_upgrade_backfills_confirmed_history_once_without_touching_searches(
    environment, monkeypatch, version
):
    import vpars_worker.db as db

    store, _ = environment
    await queue_two_regions(environment)
    engine = store.sessions.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][db.SCHEMA]
    monkeypatch.setattr(db, "SCHEMA", schema)
    previous_jobs = [(j.id, j.token, j.baseline_ready) for j in await rows(store, Job)]
    async with store.sessions.begin() as session:
        for index, row in enumerate(await session.scalars(select(Delivery).order_by(Delivery.id))):
            row.state = "sent" if index == 0 else "cancelled"
            row.message_id = 501 + index
            row.sent_at = utcnow() - timedelta(minutes=5 - index)
        session.add(
            Delivery(
                id="unconfirmed",
                user_id=101,
                item=regional_lot("FR", "10002").model_dump(mode="json"),
                candidates=[],
                state="pending",
            )
        )
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP TABLE {schema}.sent_lots"))
        await connection.execute(
            text(f"INSERT INTO {schema}.schema_revision (id) VALUES (:version)"),
            {"version": version},
        )
    await db.migrate(engine)
    await db.migrate(engine)
    cached = await rows(store, SentLot)
    assert len(cached) == 1 and cached[0].source_item_id == "10001"
    assert cached[0].message_id == 501  # First confirmed message wins across countries.
    assert sorted(previous_jobs) == sorted(
        (j.id, j.token, j.baseline_ready) for j in await rows(store, Job)
    )
    assert len(await rows(store, Delivery)) == 3


async def test_legacy_unknown_retry_is_suppressed_if_another_country_already_sent(environment):
    store, _ = environment
    await queue_two_regions(environment)
    bot = Bot()
    await Notifier(store, bot).once()
    async with store.sessions.begin() as session:
        pending = await session.scalar(select(Delivery).where(Delivery.state == "pending"))
        pending.state = "unknown"
        pending.uncertain = 1
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1
    assert sorted(d.state for d in await rows(store, Delivery)) == ["sent", "unknown"]
