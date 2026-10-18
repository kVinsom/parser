"""Fixed-price offers and receipts for manual review; no automatic payment approval."""

from uuid import uuid4

from sqlalchemy import select

from .application import locked_user, previous, remember
from .db import Notification, SubscriptionOrder, User, utcnow
from .domain import UserError
from .formatting import html

PRICES = {"LITE": {1: 250, 3: 600}, "PRO": {1: 450, 3: 1080}, "ULTRA": {1: 700, 3: 1680}}
RECEIPT_PENDING = "<b><i>⏳Вашу заявку розглянуть найближчим часом…</i></b>"
RECEIPT_PROMPT = "Надішли квитанцію фото або файлом (зображення чи PDF)."
SUBSCRIPTION_TEXT = (
    "<b>1 month\n\n"
    "LITE - 250₴\nPRO - 450₴\nULTRA - 700₴\n\n"
    "3 Months — 20% OFF\n\n"
    "LITE - 600₴ (-150)\nPRO - 1080₴ (-270)\nULTRA - 1680₴ (-420)</b>\n\n"
    "<b><i>СПИСОК ФУНКЦІЙ:</i></b>\n\n"
    "<b><i>LITE</i> :\n"
    "•Регіон - 1\n•Тільки Vinted\n•Фільтри - 3\n•Autobuy — Soon\n"
    "•Виключення слів - ❌</b>\n\n"
    "<b><i>PRO :</i>\n"
    "•Регіони - 3\n•Любий маркет з доступних\n•Фільтри - 6\n•Autobuy — Soon\n"
    "•Виключення слів - ✅</b>\n\n"
    "<b><i>ULTRA :</i>\n"
    "•Всі регіони\n•Любий маркет з доступних\n•Фільтри - 10\n•Autobuy — Soon\n"
    "•Виключення слів - ✅\n•Максимальна швидкість</b>"
    "\n\nБез активної підписки пошук недоступний.\n"
    "Фільтри — збережені набори пошуку; регіони — одночасно вибрані країни."
)


def period_label(months):
    return "1 місяць" if months == 1 else "3 місяці"


def offer_rows():
    return [
        [(f"{level} · {period_label(months)}", f"offer:{level}:{months}") for months in (1, 3)]
        for level in PRICES
    ]


def checkout_text(order):
    return (
        f"<b><i>Ви обрали підписку {order.level}\n"
        f"Термін: {period_label(order.months)}\n\n"
        f"Сума: {order.amount}₴</i></b>\n\n"
        "<code>4149500118602062</code> <b><i>- М. Качмарик</i></b>\n\n"
        "<b><i>Після оплати скиньте квитанцію в чат\n"
        "Після перевірки бот видасть підписку</i></b>"
    )


def admin_receipt_text(order, user):
    who = "@" + html(user.username) if user.username else html(user.full_name, 120)
    return (
        "<b>💳 Заявка на підписку</b>\n\n"
        f'Користувач: <a href="tg://user?id={user.id}">{who}</a>\n'
        f"ID: <code>{user.id}</code>\n"
        f"Підписка: <b>{order.level}</b>\n"
        f"Термін: {period_label(order.months)} ({order.months * 30} днів)\n"
        f"Сума: <b>{order.amount}₴</b>\n\n"
        "Перевірте квитанцію та видайте підписку через «🎟️ Видати підписку».\n"
        f"Заявка: <code>{order.id}</code>"
    )


class SubscriptionPayments:
    def __init__(self, sessions):
        self.sessions = sessions

    async def choose(self, user_id, level, months, operation_id):
        if level not in PRICES or type(months) is not int or months not in PRICES[level]:
            raise UserError("Обери підписку кнопкою нижче.")
        async with self.sessions.begin() as session:
            await locked_user(session, user_id)
            replay = await previous(session, user_id, operation_id, "subscription_order")
            if replay:
                order = await session.get(SubscriptionOrder, replay["order_id"])
                if (order.level, order.months) != (level, months):
                    raise UserError("Вибір застарів. Обери підписку знову.")
                return order
            for old in await session.scalars(
                select(SubscriptionOrder).where(
                    SubscriptionOrder.user_id == user_id,
                    SubscriptionOrder.state == "awaiting_receipt",
                )
            ):
                old.state = "cancelled"
            order = SubscriptionOrder(
                id=str(uuid4()),
                user_id=user_id,
                level=level,
                months=months,
                amount=PRICES[level][months],
                state="awaiting_receipt",
            )
            session.add(order)
            remember(session, user_id, operation_id, "subscription_order", {"order_id": order.id})
            return order

    async def get(self, user_id, order_id):
        async with self.sessions() as session:
            order = await session.get(SubscriptionOrder, order_id)
            if order is None or order.user_id != user_id or order.state == "cancelled":
                raise UserError("Спочатку обери підписку в «💳 Купити підписку».")
            return order

    async def awaiting_receipt(self, user_id):
        async with self.sessions() as session:
            return await session.scalar(
                select(SubscriptionOrder)
                .where(
                    SubscriptionOrder.user_id == user_id,
                    SubscriptionOrder.state == "awaiting_receipt",
                )
                .order_by(SubscriptionOrder.created_at.desc())
                .limit(1)
            )

    async def submit(self, user_id, order_id, kind, file_id, message_id):
        if kind not in {"photo", "document"} or not file_id:
            raise UserError(RECEIPT_PROMPT)
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            order = await session.scalar(
                select(SubscriptionOrder)
                .where(
                    SubscriptionOrder.id == order_id,
                    SubscriptionOrder.user_id == user_id,
                )
                .with_for_update()
            )
            if order is None or order.state == "cancelled":
                raise UserError("Спочатку обери підписку в «💳 Купити підписку».")
            if order.state == "submitted":
                return order  # Update replay / duplicate receipt cannot notify admins twice.
            admins = list(await session.scalars(select(User).where(User.is_admin.is_(True))))
            if not admins:
                raise UserError("Не вдалося передати заявку. Звернись до @BladeBotSupport.")
            order.state = "submitted"
            order.receipt_kind, order.receipt_file_id = kind, file_id
            order.receipt_message_id, order.submitted_at = message_id, utcnow()
            for admin in admins:
                session.add(
                    Notification(
                        id=f"receipt:{order.id}:{admin.id}",
                        recipient_id=admin.id,
                        admin_only=True,
                        kind=kind,
                        payload={"file_id": file_id, "text": admin_receipt_text(order, user)},
                    )
                )
            return order
