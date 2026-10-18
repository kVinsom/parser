import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendPhoto
from aiogram.types import Update
from sqlalchemy import func, select
from test_reply_menu import menu as menu

from vpars_bot.db import Notification, Subscription, SubscriptionOrder, User, utcnow
from vpars_bot.domain import UserError
from vpars_bot.notifications import GRANTED_TEXT, NotificationSender
from vpars_bot.reply_menu import BACK, BUY_SUBSCRIPTION
from vpars_bot.subscription_payments import (
    RECEIPT_PENDING,
    SubscriptionPayments,
    period_label,
)
from vpars_bot.subscriptions import SubscriptionService
from vpars_bot.transport import TelegramDeferred


async def admin_setup(menu, services):
    await menu.send("/start", user_id=202, username="support")
    async with services[0].sessions.begin() as session:
        (await session.get(User, 202)).is_admin = True
    await menu.send("/start")


async def receipt(menu, *, user_id=101, update_id=None, document=False, mime="application/pdf"):
    menu.update_id = update_id or menu.update_id + 1
    media = (
        {"document": {"file_id": "receipt-pdf", "file_unique_id": "unique-doc", "mime_type": mime}}
        if document
        else {
            "photo": [
                {
                    "file_id": "small-photo",
                    "file_unique_id": "unique-small",
                    "width": 100,
                    "height": 100,
                },
                {
                    "file_id": "large-photo",
                    "file_unique_id": "unique-large",
                    "width": 1000,
                    "height": 1000,
                },
            ]
        }
    )
    await menu.dp.feed_update(
        menu.bot,
        Update.model_validate(
            {
                "update_id": menu.update_id,
                "message": {
                    "message_id": menu.update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": user_id, "type": "private"},
                    "from": {
                        "id": user_id,
                        "is_bot": False,
                        "first_name": "Buyer <&>",
                        "username": "alice",
                    },
                    **media,
                },
            }
        ),
    )


@pytest.mark.parametrize(
    "level,months,amount",
    [
        ("LITE", 1, 250),
        ("PRO", 1, 450),
        ("ULTRA", 1, 700),
        ("LITE", 3, 600),
        ("PRO", 3, 1080),
        ("ULTRA", 3, 1680),
    ],
)
async def test_six_offers_show_correct_checkout_without_granting(
    menu, services, level, months, amount
):
    await menu.send("/start")
    await menu.send(BUY_SUBSCRIPTION)
    assert "20% OFF" in menu.panel().text and "1680₴ (-420)" in menu.panel().text
    assert [len(row) for row in menu.panel().reply_markup.keyboard] == [2, 2, 2, 1]
    await menu.send(f"{level} · {period_label(months)}")
    assert f"Ви обрали підписку {level}" in menu.panel().text
    assert f"Сума: {amount}₴" in menu.panel().text
    assert "4149500118602062" in menu.panel().text and "М. Качмарик" in menu.panel().text
    state = await (await menu.state()).get_data()
    payments = SubscriptionPayments(services[0].sessions)
    order = await payments.get(101, state["subscription_order_id"])
    assert (order.level, order.months, order.amount) == (level, months, amount)
    assert await SubscriptionService(services[0].sessions).get(101) is None
    await menu.send("оплатив")
    assert "Надішли квитанцію" in menu.transport.calls[-1].text
    await menu.send(BACK)
    assert "20% OFF" in menu.panel().text


@pytest.mark.parametrize("document", [False, True])
async def test_receipt_delivered_with_buyer_and_offer_once_after_sender_restart(
    menu, services, document
):
    await admin_setup(menu, services)
    await menu.send(BUY_SUBSCRIPTION)
    await menu.send("PRO · 3 місяці")
    await receipt(menu, document=document)
    assert menu.panel().text == RECEIPT_PENDING
    update_id = menu.update_id
    await receipt(menu, document=document, update_id=update_id)
    # A new Telegram message with the same receipt also must not create a new application.
    await receipt(menu, document=document)
    async with services[0].sessions() as session:
        notifications = list(await session.scalars(select(Notification)))
        assert len(notifications) == 1
        assert notifications[0].state == "pending"
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 0
    # The sender needs only the DB, not a live FSM or receipt handler.
    sender = NotificationSender(services[0].sessions, menu.bot)
    assert await sender.once()
    sent = menu.transport.calls[-1]
    assert sent.chat_id == 202
    assert sent.__api_method__ == ("sendDocument" if document else "sendPhoto")
    assert "@alice" in sent.caption and "PRO" in sent.caption
    assert "90 днів" in sent.caption and "1080₴" in sent.caption
    assert "Перевірте квитанцію" in sent.caption
    assert len(sent.caption) < 1024
    assert getattr(sent, "document" if document else "photo") == (
        "receipt-pdf" if document else "large-photo"
    )
    assert not await NotificationSender(services[0].sessions, menu.bot).once()


async def test_receipt_after_fsm_expiry_and_interrupted_ack_recovers(menu, services, monkeypatch):
    await admin_setup(menu, services)
    await menu.send(BUY_SUBSCRIPTION)
    await menu.send("ULTRA · 1 місяць")
    await (await menu.state()).clear()
    original = menu.transport.make_request
    failed = False

    async def fail_ack(bot, method, timeout=None):
        nonlocal failed
        if getattr(method, "text", None) == RECEIPT_PENDING and not failed:
            failed = True
            raise RuntimeError("temporary acknowledgement failure")
        return await original(bot, method, timeout)

    monkeypatch.setattr(menu.transport, "make_request", fail_ack)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        await receipt(menu)
    await receipt(menu, update_id=menu.update_id)
    assert menu.panel().text == RECEIPT_PENDING
    async with services[0].sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_bad_receipt_no_order_no_admin_and_foreign_order_do_not_grant(menu, services):
    await menu.send("/start")
    await receipt(menu)
    assert "Спочатку обери підписку" in menu.transport.calls[-1].text
    await menu.send(BUY_SUBSCRIPTION)
    await menu.send("LITE · 1 місяць")
    await receipt(menu, document=True, mime="application/zip")
    assert "Надішли квитанцію" in menu.transport.calls[-1].text
    await receipt(menu)
    assert "Не вдалося передати заявку" in menu.transport.calls[-1].text
    payments = SubscriptionPayments(services[0].sessions)
    order = await payments.awaiting_receipt(101)
    await menu.send("/start", user_id=303, username="outsider")
    with pytest.raises(UserError, match="Спочатку"):
        await payments.submit(303, order.id, "photo", "forged", 100)
    async with services[0].sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Notification)) == 0
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 0


async def test_reselect_cancels_old_offer_and_replay_does_not_create_order_twice(menu, services):
    await admin_setup(menu, services)
    payments = SubscriptionPayments(services[0].sessions)
    first = await payments.choose(101, "LITE", 1, "first")
    assert (await payments.choose(101, "LITE", 1, "first")).id == first.id
    second = await payments.choose(101, "ULTRA", 3, "second")
    with pytest.raises(UserError):
        await payments.submit(101, first.id, "photo", "wrong-offer", 1)
    assert (await payments.awaiting_receipt(101)).id == second.id
    with pytest.raises(UserError):
        await payments.choose(101, "PRO", 1, "second")


async def test_grant_commits_one_recipient_notice_with_idempotent_operation(
    menu, services, profiles
):
    await admin_setup(menu, services)
    profiles.names.update({202: "support", 101: "alice"})
    subs = SubscriptionService(services[0].sessions, services[2])
    result = await subs.grant(202, 101, "alice", "ULTRA", 30, "grant-once")
    assert await subs.grant(202, 101, "alice", "ULTRA", 30, "grant-once") == result
    sender = NotificationSender(services[0].sessions, menu.bot)
    assert await sender.once()
    sent = menu.transport.calls[-1]
    assert sent.chat_id == 101 and sent.text == GRANTED_TEXT
    assert not await sender.once()


async def test_grant_to_buyer_without_username_by_verified_id(menu, services, profiles):
    from vpars_bot.subscriptions import GRANT_SUBSCRIPTION
    from vpars_bot.telegram import TelegramProfileVerifier

    await admin_setup(menu, services)
    await menu.send("/start", username=None)
    profiles.names.update({202: "support", 101: None})
    # An absent username is valid only if the actual private Telegram ID matches.
    await TelegramProfileVerifier(menu.bot).verify(101, None)
    await menu.send(GRANT_SUBSCRIPTION, user_id=202, username="support")
    await menu.send("101", user_id=202, username="support")
    await menu.send("LITE", user_id=202, username="support")
    await menu.send("30", user_id=202, username="support")
    assert "Підписку видано: 101" in menu.panel(202).text
    assert "@None" not in menu.panel(202).text
    await NotificationSender(services[0].sessions, menu.bot).once()
    assert menu.transport.calls[-1].chat_id == 101
    assert menu.transport.calls[-1].text == GRANTED_TEXT


async def queued_receipt(menu, services):
    await admin_setup(menu, services)
    await menu.send(BUY_SUBSCRIPTION)
    await menu.send("LITE · 1 місяць")
    await receipt(menu)
    return NotificationSender(services[0].sessions, menu.bot)


async def make_due(sessions):
    async with sessions.begin() as session:
        row = await session.scalar(select(Notification))
        row.next_attempt_at = utcnow() - timedelta(seconds=1)


async def test_sender_honors_pacing_and_retries_rejected_sends(menu, services, monkeypatch):
    sender = await queued_receipt(menu, services)
    original = menu.transport.make_request
    errors = [
        TelegramDeferred(3),
        TelegramRetryAfter(method=SendPhoto(chat_id=202, photo="x"), message="slow", retry_after=3),
    ]

    async def temporary(bot, method, timeout=None):
        if errors:
            raise errors.pop(0)
        return await original(bot, method, timeout)

    monkeypatch.setattr(menu.transport, "make_request", temporary)
    for error in ("TelegramDeferred", "TelegramRetryAfter"):
        assert await sender.once()
        async with services[0].sessions() as session:
            row = await session.scalar(select(Notification))
            assert row.state == "pending" and row.last_error == error
        assert not await sender.once()
        await make_due(services[0].sessions)
    assert await sender.once()
    assert menu.transport.calls[-1].chat_id == 202
    assert not await sender.once()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_sender_never_repeats_accepted_message_after_lost_response(
    menu, services, monkeypatch, cancelled
):
    sender = await queued_receipt(menu, services)
    original = menu.transport.make_request
    sent = []

    async def lost_response(bot, method, timeout=None):
        sent.append(await original(bot, method, timeout))
        if cancelled:
            raise asyncio.CancelledError()
        raise OSError("response lost after Telegram accepted the receipt")

    monkeypatch.setattr(menu.transport, "make_request", lost_response)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await sender.once()
    else:
        assert await sender.once()
    async with services[0].sessions() as session:
        assert (await session.scalar(select(Notification))).state == "unknown"
    await make_due(services[0].sessions)
    assert not await NotificationSender(services[0].sessions, menu.bot).once()
    assert len(sent) == 1


async def test_sender_commits_uncertainty_before_contacting_telegram(menu, services, monkeypatch):
    sender = await queued_receipt(menu, services)
    original = menu.transport.make_request

    async def inspect_barrier(bot, method, timeout=None):
        async with services[0].sessions() as session:
            row = await session.scalar(select(Notification))
            assert row.state == "unknown" and row.last_error == "InterruptedSend"
        return await original(bot, method, timeout)

    monkeypatch.setattr(menu.transport, "make_request", inspect_barrier)
    assert await sender.once()
    async with services[0].sessions() as session:
        row = await session.scalar(select(Notification))
        assert row.state == "sent" and row.message_id is not None


async def test_invalid_notification_is_failed_without_blocking_queue(menu, services):
    sender = await queued_receipt(menu, services)
    async with services[0].sessions.begin() as session:
        row = await session.scalar(select(Notification))
        row.payload = {"text": "missing file_id"}
    calls = len(menu.transport.calls)
    assert await sender.once()
    assert not await sender.once()
    assert len(menu.transport.calls) == calls
    async with services[0].sessions() as session:
        row = await session.scalar(select(Notification))
        assert row.state == "failed" and row.last_error == "InvalidPayload"


@pytest.mark.integration
async def test_pg_competing_notification_senders_deliver_once(pg_db, registry, menu):
    from vpars_bot.application import Catalog

    _, sessions = pg_db
    await Catalog(sessions, registry).register(101, 101, "alice", "Alice", 1, start=True)
    async with sessions.begin() as session:
        session.add(Notification(id="concurrent", recipient_id=101, kind="text",
                                 payload={"text": "one notification"}))
    sender = NotificationSender(sessions, menu.bot)
    before = len(menu.transport.calls)
    assert sorted(await asyncio.gather(sender.once(), sender.once())) == [False, True]
    assert len(menu.transport.calls) == before + 1
    async with sessions() as session:
        assert (await session.get(Notification, "concurrent")).state == "sent"


@pytest.mark.parametrize("revoked", [False, True])
async def test_blocked_or_revoked_recipient_does_not_loop_or_leak_receipt(
    menu, services, monkeypatch, revoked
):
    sender = await queued_receipt(menu, services)
    calls = len(menu.transport.calls)
    if revoked:
        async with services[0].sessions.begin() as session:
            (await session.get(User, 202)).is_admin = False
    else:

        async def forbidden(bot, method, timeout=None):
            raise TelegramForbiddenError(
                method=SendPhoto(chat_id=202, photo="x"), message="blocked"
            )

        monkeypatch.setattr(menu.transport, "make_request", forbidden)
    assert await sender.once()
    assert not await sender.once()
    assert len(menu.transport.calls) == calls
    async with services[0].sessions() as session:
        assert (await session.scalar(select(Notification))).state == "failed"


@pytest.mark.integration
async def test_pg_duplicate_receipts_are_one_request_and_notification(pg_db, registry, profiles):
    import asyncio

    from vpars_bot.application import Catalog

    _, sessions = pg_db
    catalog = Catalog(sessions, registry)
    await catalog.register(101, 101, "alice", "Alice", 1, start=True)
    await catalog.register(202, 202, "support", "Support", 2, start=True)
    async with sessions.begin() as session:
        (await session.get(User, 202)).is_admin = True
    payments = SubscriptionPayments(sessions)
    orders = await asyncio.gather(*[payments.choose(101, "ULTRA", 3, "same") for _ in range(2)])
    assert orders[0].id == orders[1].id
    await asyncio.gather(
        *[payments.submit(101, orders[0].id, "photo", "receipt", 3) for _ in range(2)]
    )
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SubscriptionOrder)) == 1
        assert await session.scalar(select(func.count()).select_from(Notification)) == 1
