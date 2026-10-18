"""Owned checkout with manual payment or a single balance-first attempt after Buy."""

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, timedelta
from urllib.parse import parse_qsl
from uuid import uuid4
from weakref import WeakKeyDictionary

from sqlalchemy import BigInteger, Column, MetaData, String, Table, select

from . import autobuy_availability as autobuy
from .application import locked_user
from .db import WalletOrder, utcnow
from .domain import UserError
from .wallet import REGIONS, WalletError, WalletService, listing_identity, minor, status_text
from .wallet_accounts import wallet_account, wallet_session

_services = WeakKeyDictionary()
OPEN_STATES = {"human_waiting", "human_active", "balance_waiting", "balance_checking"}
DELIVERED = Table("deliveries", MetaData(), Column("id", String), Column("user_id", BigInteger),
                  Column("message_id", BigInteger), Column("state", String), schema="vpars_monitor")


def handoff_for(sessions):
    return _services.get(sessions)


def telegram_owner(raw, token, *, now=None):
    """Validate Telegram initData, never trust initDataUnsafe or URL owner parameters."""
    try:
        if not isinstance(raw, str) or not 0 < len(raw) <= 8192 or not token:
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=30)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError
        digest = fields.pop("hash")
        secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, digest):
            raise ValueError
        age = (time.time() if now is None else now) - int(fields["auth_date"])
        user = json.loads(fields["user"])
        uid = user["id"]
        if not -30 <= age <= 300 or type(uid) is not int or uid <= 0 or user.get("is_bot"):
            raise ValueError
        return uid
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise UserError("Відкрий це вікно заново кнопкою в особистому чаті бота.") from exc


class HandoffService:
    def __init__(self, sessions, base_url, *, resolver=None, delivery_resolver=None,
                 balance_first=False):
        self.sessions, self.base_url = sessions, base_url.rstrip("/")
        self.wallet = WalletService(sessions, resolver)
        self.delivery_resolver = delivery_resolver or self.delivered_message
        self.balance_first = balance_first

    def register(self):
        _services[self.sessions] = self

    def unregister(self):
        if handoff_for(self.sessions) is self:
            del _services[self.sessions]

    async def delivered_message(self, user_id, delivery_id):
        async with self.sessions() as session:
            message_id = await session.scalar(select(DELIVERED.c.message_id).where(
                DELIVERED.c.id == delivery_id, DELIVERED.c.user_id == user_id,
                DELIVERED.c.state == "sent",
            ))
        if message_id is None:
            raise UserError("Лот не знайдено серед надісланих тобі повідомлень.")
        return message_id

    async def request_delivery(self, user_id, delivery_id):
        autobuy.require_available()
        if not re.fullmatch(r"[0-9a-f]{64}", delivery_id):
            raise UserError("Некоректне посилання на товар.")
        # The URL selects a delivered lot. Only verified Telegram initData selects
        # its owner, then the existing request path checks session/region/access.
        message_id = await self.delivery_resolver(user_id, delivery_id)
        return await self.request(user_id, message_id)

    async def request(self, user_id, message_id):
        autobuy.require_available()
        if not self.base_url:
            raise UserError("Вікно Buy ще не підключено до зовнішнього доступу. Покупку не розпочато.")
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            item = await self.wallet.resolver(user_id, message_id)
            region, item_id = listing_identity(item["item_url"])
            account = await wallet_account(session, user_id, region)
            connection = await wallet_session(session, user_id, region)
            if (not account or not connection or not connection.account_key
                    or connection.state not in {"connected", "checkout_ready"}
                    or connection.error == "CheckoutPaused"):
                raise UserError(f"Спочатку підключи сесію Vinted {region} через FAQ → Підключити автобай.")
            error, _ = await self.wallet.access(session, user, account)
            if error:
                raise WalletError(error)
            old = await session.scalar(select(WalletOrder).where(
                WalletOrder.user_id == user_id, WalletOrder.item_id == item_id,
            ))
            if old:
                if old.state == "human_expired" and not account.active_order:
                    old.state = "balance_waiting" if self.balance_first else "human_waiting"
                    old.expires_at = utcnow() + timedelta(minutes=15)
                    old.updated_at = utcnow()
                    account.active_order = old.id
                return old  # Never create a second checkout for a possibly purchased item.
            if account.active_order:
                raise UserError("Спочатку заверши відкрите вікно покупки цього регіону.")
            if item["price"]["currency"] != account.currency:
                raise WalletError("ContractMismatch")
            price = minor(item["price"]["amount"])
            if price > account.max_total:
                raise WalletError("BudgetExceeded")
            order = WalletOrder(
                id=uuid4().hex, user_id=user_id, item_id=item_id,
                url=f"https://{REGIONS[region][0]}/items/{item_id}",
                listed_price=price, currency=account.currency,
                state="balance_waiting" if self.balance_first else "human_waiting",
                expires_at=utcnow() + timedelta(minutes=15),
            )
            account.active_order = order.id
            session.add(order)
        return order

    def message(self, order):
        if order.state not in OPEN_STATES:
            return {"text": status_text(order)}
        region, _ = listing_identity(order.url)
        return {
            "text": (
                f"<b>Покупка у Vinted {region}</b>\n\n"
                "Відкрий особисте вікно оформлення. Бот відкриє покупку цього товару; "
                "Vinted підставить доступні збережені дані.\n"
                "Адресу достатньо один раз додати й зберегти у Vinted. Якщо сайт не підставив її, "
                "вікно попросить додати або вибрати адресу.\n"
                "Якщо з’явиться CAPTCHA — пройди її вручну в цьому ж вікні. "
                + ("Після Buy бот спершу перевіряє баланс і повну суму. Якщо балансу вистачає, "
                   "оплачує з нього в межах твого ліміту. Якщо бракує — вибираєш оплату сам."
                   if self.balance_first else
                   "Перевір адресу, доставку та повну суму й сам натисни оплату у Vinted. "
                   "Сеанс діє 15 хвилин. Бот не натискає оплату й не підтверджує факт покупки.")
            ),
            "reply_markup": {"inline_keyboard": [[{
                "text": "🛒 Відкрити вікно покупки",
                "web_app": {"url": f"{self.base_url}/purchase/{order.id}"},
            }]]},
        }

    async def authorize(self, user_id, order_id, *, activate=False, claim_balance=False):
        autobuy.require_available()
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            order = await session.get(WalletOrder, order_id)
            if (not order or order.user_id != user_id or order.state not in OPEN_STATES
                    or not order.expires_at or order.expires_at.replace(tzinfo=UTC) <= utcnow()):
                raise UserError("Це вікно недоступне або час сеансу минув.")
            region, _ = listing_identity(order.url)
            account = await wallet_account(session, user_id, region)
            connection = await wallet_session(session, user_id, region)
            if (not account or account.active_order != order.id or not connection
                    or not connection.account_key
                    or connection.state not in {"connected", "checkout_ready"}
                    or connection.error == "CheckoutPaused"):
                raise UserError("Підключення змінилося. Вікно закрито.")
            error, period = await self.wallet.access(session, user, account, order=order)
            if error:
                raise WalletError(error)
            if activate:
                if order.state not in {"human_waiting", "human_active"}:
                    raise UserError("Перевірка балансу ще не завершена.")
                order.state, order.quota_period_start = "human_active", period
                order.updated_at = utcnow()
            if claim_balance:
                if not self.balance_first or order.state != "balance_waiting":
                    raise UserError("Перевірку покупки вже розпочато.")
                order.state, order.updated_at = "balance_checking", utcnow()
            return order, account, connection.account_key

    async def close(self, user_id, order_id):
        async with self.sessions.begin() as session:
            await locked_user(session, user_id)
            order = await session.get(WalletOrder, order_id)
            if not order or order.user_id != user_id or order.state not in OPEN_STATES:
                return
            account = await wallet_account(session, user_id, listing_identity(order.url)[0])
            # Once a person saw the browser, a payment may have happened. Never call it cancelled.
            if order.state == "balance_checking":
                order.state, order.error = "failed", "Interrupted"
            else:
                order.state = "human_closed" if order.state == "human_active" else "human_expired"
            order.updated_at = utcnow()
            if account and account.active_order == order.id:
                account.active_order = None

    async def recover(self):
        async with self.sessions() as session:
            rows = list(await session.scalars(select(WalletOrder).where(
                WalletOrder.state.in_(OPEN_STATES | {"paying"}),
            )))
        for row in rows:
            if row.state == "paying":
                await self.wallet.finish(row.id, "unknown", error="Interrupted")
            else:
                await self.close(row.user_id, row.id)
