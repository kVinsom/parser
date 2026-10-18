"""Deliver committed subscription events independently from user dialog retries."""

import logging
from datetime import timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendDocument, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from . import autobuy_availability as autobuy
from .db import Notification, User, utcnow
from .transport import TelegramDeferred
from .wallet_signals import signals, wait_for_work

logger = logging.getLogger("vpars_bot")
GRANTED_TEXT = "<b><i>Підписку зараховано ✅</i></b>"


class NotificationSender:
    def __init__(self, sessions, bot):
        self.sessions, self.bot = sessions, bot

    async def once(self):
        # Persist the uncertainty barrier before transport. A lost response or a
        # crash must never make an accepted receipt eligible for an automatic resend.
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Notification)
                .where(
                    Notification.state == "pending",
                    Notification.next_attempt_at <= utcnow(),
                )
                .order_by(Notification.created_at, Notification.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            user = await session.get(User, row.recipient_id)
            if user is None or (row.admin_only and not user.is_admin):
                row.state, row.last_error = "failed", "RecipientUnavailable"
                return True
            try:
                # Old pending setup/Approve messages must not revive a parked feature.
                if not autobuy.AVAILABLE and row.id.startswith(("wallet:", "wallet-session:")):
                    row.kind, row.payload = "text", {"text": autobuy.SOON}
                kwargs = {"chat_id": user.chat_id, "parse_mode": "HTML"}
                if row.kind == "photo":
                    method = SendPhoto(
                        **kwargs,
                        photo=row.payload["file_id"],
                        caption=row.payload["text"],
                    )
                elif row.kind == "document":
                    method = SendDocument(
                        **kwargs,
                        document=row.payload["file_id"],
                        caption=row.payload["text"],
                    )
                else:
                    markup = row.payload.get("reply_markup")
                    method = SendMessage(
                        **kwargs, text=row.payload["text"],
                        reply_markup=InlineKeyboardMarkup.model_validate(markup) if markup else None,
                    )
            except (ValueError, KeyError, TypeError):
                row.state, row.last_error = "failed", "InvalidPayload"
                return True
            row.state, row.last_error = "unknown", "InterruptedSend"
            notification_id = row.id

        state, error, message_id, retry_after = "unknown", None, None, None
        try:
            sent = await self.bot(method, request_timeout=7)
        except (TelegramDeferred, TelegramRetryAfter) as exc:
            # Both prove that this attempt was rejected before delivery.
            state, error, retry_after = "pending", type(exc).__name__, exc.retry_after
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            state, error = "failed", type(exc).__name__
        except Exception as exc:
            error = type(exc).__name__
        else:
            state, message_id = "sent", sent.message_id

        async with self.sessions.begin() as session:
            row = await session.get(Notification, notification_id, with_for_update=True)
            row.state, row.last_error, row.message_id = state, error, message_id
            if retry_after is not None:
                row.next_attempt_at = utcnow() + timedelta(seconds=retry_after + 0.05)
            else:
                row.attempts += 1
            if state in {"unknown", "failed"}:
                logger.warning(
                    "Notification %s: %s (%s)", notification_id, state, error
                )
        return True

    async def run(self):
        wake = signals(self.sessions).notifications
        while True:
            wake.clear()
            if not await self.once():
                await wait_for_work(wake)
