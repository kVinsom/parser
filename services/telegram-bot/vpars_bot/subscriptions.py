"""Manual subscription records; these do not gate search or initiate payments."""

import re
from datetime import UTC, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .application import previous, remember
from .db import Notification, Subscription, User, utcnow
from .domain import SourceRegistry, UserError
from .entitlements import NO_PLAN, PLANS, reconcile_user
from .notifications import GRANTED_TEXT

LEVELS = ("LITE", "PRO", "ULTRA")
GRANT_SUBSCRIPTION = "🎟️ Видати підписку"
KYIV = ZoneInfo("Europe/Kyiv")
DAYS_ERROR = "Введи цілу кількість днів від -3650 до 3650, крім 0. Наприклад: +10 або -5."


def parse_days(value):
    value = value.strip()
    if not re.fullmatch(r"[+-]?[0-9]{1,4}", value):
        raise UserError(DAYS_ERROR)
    days = int(value)
    if not 1 <= abs(days) <= 3650:
        raise UserError(DAYS_ERROR)
    return days


def expiry_label(value):
    return value.replace(tzinfo=UTC).astimezone(KYIV).strftime("%d.%m.%Y %H:%M")


def profile_text(subscription, *, is_admin=False):
    active = is_admin or (subscription and subscription.expires_at.replace(tzinfo=UTC) > utcnow())
    status = "активний" if active else "неактивний"
    level = subscription.level if subscription else "—"
    expiry = "до " + expiry_label(subscription.expires_at) if subscription else "—"
    if is_admin:
        level, expiry = "ULTRA", "безстроково — адміністратор"
    plan = PLANS.get(level, NO_PLAN) if active else NO_PLAN
    return (
        "<b>👤 Профіль Blade parser</b>\n\n"
        f"<b>Статус</b>: <b><i>{status}</i></b>\n"
        f"<b>Тариф</b>: <b><i>{level}</i></b>\n"
        f"<b>Термін</b>: <b><i>{expiry}</i></b>\n"
        f"Фільтри: {plan.filters} · Регіони: {plan.regions if plan.regions is not None else 'усі'}\n"
        "Autobuy — Soon"
    )


class SubscriptionService:
    def __init__(self, sessions, admins=None):
        self.sessions, self.admins = sessions, admins
        self.registry = SourceRegistry.read(
            Path(__file__).resolve().parents[1] / "config/sources.json"
        )

    async def require_admin(self, actor_id):
        if self.admins is None:
            raise UserError("Ця дія доступна лише адміністратору.")
        await self.admins.require(actor_id)

    async def get(self, user_id):
        async with self.sessions() as session:
            return await session.get(Subscription, user_id)

    async def resolve_recipient(self, actor_id, value):
        await self.require_admin(actor_id)
        value = value.strip()
        if not (value.isascii() and value.isdecimal() and len(value) <= 16):
            return await self.admins.resolve(actor_id, value)
        async with self.sessions() as session:
            recipient = await session.get(User, int(value))
            if recipient is None:
                raise UserError("Користувача не знайдено. Отримувач має виконати /start.")
        await self.admins.verifier.verify(recipient.id, recipient.username_key)
        return recipient

    async def grant(self, actor_id, recipient_id, username, level, days, operation_id):
        await self.require_admin(actor_id)
        if level not in LEVELS:
            raise UserError("Обери LITE, PRO або ULTRA.")
        if type(days) is not int or not 1 <= abs(days) <= 3650:
            raise UserError(DAYS_ERROR)
        expected = {
            "recipient_id": recipient_id,
            "username": username,
            "level": level,
            "days": days,
        }

        def replay_result(replay):
            if any(replay.get(key) != value for key, value in expected.items()):
                raise UserError("Ця дія застаріла. Почни видачу підписки заново.")
            return replay

        async with self.sessions() as session:
            replay = await previous(session, actor_id, operation_id, "grant_subscription")
            if replay:
                return replay_result(replay)
        await self.admins.verifier.verify(recipient_id, username)
        async with self.sessions.begin() as session:
            people = {
                user.id: user
                for user in await session.scalars(
                    select(User)
                    .where(User.id.in_(sorted({actor_id, recipient_id})))
                    .order_by(User.id)
                    .with_for_update()
                )
            }
            actor, recipient = people.get(actor_id), people.get(recipient_id)
            if actor is None or not actor.is_admin:
                raise UserError("Права адміністратора вже знято.")
            if recipient is None or recipient.username_key != username:
                raise UserError("Нік отримувача змінився. Почни видачу підписки заново.")
            replay = await previous(session, actor_id, operation_id, "grant_subscription")
            if replay:
                return replay_result(replay)
            subscription = await session.get(Subscription, recipient_id)
            now = utcnow()
            old_expiry = subscription.expires_at.replace(tzinfo=UTC) if subscription else None
            if days < 0 and (old_expiry is None or old_expiry <= now):
                raise UserError("У користувача немає активної підписки, з якої можна відняти дні.")
            # Freeze generations expired under the old subscription before renewing it.
            await reconcile_user(session, recipient, self.registry)
            base = max(now, old_expiry) if old_expiry else now
            try:
                expires_at = max(now, base + timedelta(days=days))
            except OverflowError:
                raise UserError("Завеликий термін підписки. Введи меншу кількість днів.") from None
            result = {
                **expected,
                "expires_at": expires_at.isoformat(),
                "active": expires_at > now,
                "previous_level": subscription.level if subscription else None,
                "previous_expires_at": subscription.expires_at.isoformat()
                if subscription
                else None,
            }
            if subscription is None:
                subscription = Subscription(user_id=recipient_id)
                session.add(subscription)
            if old_expiry is None or old_expiry <= now:
                subscription.period_started_at = now
            subscription.level = level
            subscription.expires_at = expires_at
            subscription.granted_by = actor_id
            subscription.updated_at = now
            await session.flush()
            await reconcile_user(session, recipient, self.registry)
            session.add(
                Notification(
                    id=f"grant:{actor_id}:{operation_id}",
                    recipient_id=recipient_id,
                    kind="text",
                    payload={
                        "text": GRANTED_TEXT
                        if days > 0
                        else (
                            "<b>Термін підписки скорочено.</b>"
                            if expires_at > now
                            else "<b>Підписку завершено.</b>"
                        )
                        + f"\nЗміна: {days} днів\nДо {expiry_label(expires_at)}"
                    },
                )
            )
            remember(session, actor_id, operation_id, "grant_subscription", result)
            return result
