"""Wallet-only checkout policy and durable, at-most-once payment state machine."""

import re
from dataclasses import asdict, dataclass
from datetime import UTC, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select, text

from . import autobuy_availability as autobuy
from .application import locked_user
from .db import Notification, WalletAccount, WalletOrder, utcnow
from .domain import SourceRegistry, UserError
from .entitlements import MESSAGES, allowed_sources, autobuy_access, plan_for
from .wallet_accounts import wallet_account, wallet_session
from .wallet_mode import button_checkout
from .wallet_signals import signals

REGIONS = {
    "UK": ("www.vinted.co.uk", "GBP"),
    "FR": ("www.vinted.fr", "EUR"),
    "DE": ("www.vinted.de", "EUR"),
    "IT": ("www.vinted.it", "EUR"),
}
CONNECT_TEXT = (
    "<b>Autobuy · баланс Vinted</b>\n\n"
    "Акаунт ще не підключений. Потрібні вхід у Vinted і ліміт покупки.\n"
    "Відкрий FAQ → «Підключити автобай» для покрокового налаштування.\n"
    "Для кожного регіону — окреме підключення й ліміт. "
    "Доставку беремо з оформлення Vinted: збережену адресу або пункт видачі.\n"
    "Ключ сесії додавай лише на відповідному кроці майстра. Пароль і коди SMS не потрібні."
)
STATUSES = {
    "bot_requested": "⏳ Перевіряю товар, доставку та баланс. Потім надішлю суму для підтвердження.",
    "bot_checking": "⏳ Перевіряю повну суму. Для оплати потрібно буде натиснути Approve.",
    "bot_quoted": "Підтвердіть автобай кнопкою Approve у повідомленні із сумою.",
    "bot_queued": "⏳ Автобай підтверджено. Перевіряю оплату з балансу.",
    "bot_executing": "⏳ Перевіряю підтверджену суму перед оплатою.",
    "balance_waiting": "Перевірю баланс Vinted після відкриття Buy.",
    "balance_checking": "Перевіряю баланс, адресу та повну суму. Якщо балансу вистачить — оплачу з нього.",
    "human_waiting": "Відкрий особисте вікно покупки в повідомленні бота.",
    "human_active": "Оформлення відкрите. Оплату підтверджуєш особисто у Vinted.",
    "human_closed": "Сеанс завершено. Перевір результат покупки в замовленнях Vinted. Бот не повторюватиме оформлення цього товару.",
    "human_expired": "Вікно покупки не було відкрито; сеанс завершено без оплати ботом.",
    "requested": "⏳ Перевіряю лот і повну суму.",
    "checking": "⏳ Перевіряю лот і повну суму.",
    "quoted": "Підтвердь суму в повідомленні про покупку.",
    "queued": "⏳ Покупку підтверджено. Перевіряю оплату з балансу.",
    "executing": "⏳ Перевіряю оплату з балансу.",
    "paying": "⏳ Очікую результат оплати. Повторної оплати не буде.",
    "succeeded": "✅ Покупку оформлено з балансу Vinted.",
    "cancelled": "Покупку скасовано.",
    "failed": "Покупку не виконано. Баланс не списувався ботом.",
    "unknown": "⚠️ Результат оплати невідомий. Перевір замовлення у Vinted. Повтор заблокований.",
}
ERRORS = {
    "BrowserConnectionInvalid": "Перевір налаштування підключення до локального браузера.",
    "BrowserUnavailable": "Підключення Vinted на сервері недоступне. Покупку не виконано.",
    "BrowserAlreadyBound": "Цей браузер закріплений за іншим користувачем або регіоном. Потрібен окремий профіль.",
    "ProfileBusy": "Профіль Vinted зайнятий іншим оформленням або входом. Покупку не виконано.",
    **MESSAGES,
    "NotConnected": CONNECT_TEXT,
    "CheckoutChanged": "Сума або доставка змінилися. Покупку не виконано.",
    "CheckoutRejected": "Vinted повідомив про помилку створення оформлення. Покупку зупинено, оплату не натискали.",
    "DeliveryChanged": "Доставка не збігається зі збереженим вибором. Перевір адресу або відділення у Vinted.",
    "CheckoutAddressRequired": "Vinted не підтвердив адресу доставки. Додай або вибери свою адресу в оформленні Vinted і натисни «Зберегти адресу». Наступні оформлення використовуватимуть збережений вибір, якщо він доступний. Потім повтори перевірку оформлення в боті.",
    "CheckoutPhoneRequired": "Vinted просить телефон доставки. Вкажи в оформленні свій доступний номер, познач збереження для наступних покупок і натисни «Weiter» або «Продовжити». Підтверджений номер профілю може не підставлятися для доставки. Потім повтори перевірку оформлення в боті. Оплату не натискали.",
    "InsufficientBalance": "На балансі Vinted недостатньо коштів. Покупку зупинено, гроші не списано.",
    "BudgetExceeded": "Повна сума перевищує ліміт покупки.",
    "WalletOnly": "Не вдалося підтвердити оплату тільки з балансу Vinted.",
    "ManualAction": "Потрібен вхід, вибір доставки або перевірка у Vinted.",
    "CheckoutChallenge": "Vinted вимагає ручної перевірки CAPTCHA. Покупку зупинено до оплати; автоматичного повтору не буде. Потрібно пройти перевірку в збереженому акаунті й повторно перевірити підключення через FAQ → «Підключити автобай». Сесію збережено. Якщо Vinted повідомляє про блокування, звернися до його підтримки.",
    "CheckoutLoginRequired": "Vinted просить повторний вхід для цього регіону. Покупку зупинено до оплати. Віднови підключення через FAQ → «Підключити автобай».",
    "ContractMismatch": "Інтерфейс Vinted потребує перевірки. Покупку не виконано.",
    "Expired": "Час підтвердження минув. Покупку не виконано.",
    "Interrupted": "Перевірку перервано. Покупку не виконано.",
}


class WalletError(Exception):
    """Only a fixed code may be persisted/logged, never an upstream error string."""


SESSION_BLOCKS = frozenset({"CheckoutChallenge", "CheckoutLoginRequired"})


def session_binding(account, connection):
    if (not account or not account.enabled or not account.contract_hash
            or not connection or connection.state not in {"connected", "checkout_ready"}
            or connection.error == "CheckoutPaused" or not connection.account_key):
        raise WalletError("NotConnected")
    if connection.error in SESSION_BLOCKS:
        raise WalletError(connection.error)
    return [account.contract_hash, connection.account_key, connection.attempt_id]


def minor(value) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > 1_000_000:
            raise ValueError
        cents = amount * 100
        if cents != cents.to_integral_value():
            raise ValueError
        return int(cents)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise WalletError("ContractMismatch") from exc


def listing_identity(url: str) -> tuple[str, str]:
    try:
        if (
            not isinstance(url, str)
            or len(url) > 2048
            or any(c.isspace() or ord(c) < 32 for c in url)
        ):
            raise ValueError
        p = urlsplit(url)
        region = next(k for k, (host, _) in REGIONS.items() if p.netloc == host)
        match = re.fullmatch(r"/items/([1-9][0-9]{0,19})(?:-[A-Za-z0-9_%~-]+)?/?", p.path)
        if p.scheme != "https" or p.query or p.fragment or not match:
            raise ValueError
        return region, match[1]
    except (ValueError, StopIteration, TypeError) as exc:
        raise WalletError("ContractMismatch") from exc


@dataclass(frozen=True)
class Quote:
    item_id: str
    currency: str
    item_price: int
    shipping: int
    protection: int
    total: int
    wallet_balance: int
    wallet_charge: int
    external_charge: int
    payment_method: str
    delivery_key: str
    account_key: str
    checkout_key: str

    def check_details(self, order, account):
        money = [
            self.item_price,
            self.shipping,
            self.protection,
            self.total,
            self.wallet_balance,
            self.wallet_charge,
            self.external_charge,
        ]
        if any(type(v) is not int or v < 0 or v > 100_000_000 for v in money):
            raise WalletError("ContractMismatch")
        if (
            self.item_id != order.item_id
            or self.currency != order.currency
            or self.currency != account.currency
            or self.item_price != order.listed_price
            or self.total != self.item_price + self.shipping + self.protection
            or self.total <= 0
        ):
            raise WalletError("CheckoutChanged")
        if any(
            not isinstance(v, str) or not v.strip() or len(v) > 500
            for v in (self.delivery_key, self.account_key, self.checkout_key)
        ):
            raise WalletError("ManualAction")
        if self.total > account.max_total:
            raise WalletError("BudgetExceeded")

    def check(self, order, account):
        self.check_details(order, account)
        if self.wallet_balance < self.total:
            raise WalletError("InsufficientBalance")
        if (
            self.payment_method != "wallet"
            or self.external_charge != 0
            or self.wallet_charge != self.total
        ):
            raise WalletError("WalletOnly")

    def same_purchase(self, other):
        # Balance may change after a different sale. All charge/identity fields must agree.
        return {k: v for k, v in asdict(self).items() if k != "wallet_balance"} == {
            k: v for k, v in asdict(other).items() if k != "wallet_balance"
        }


def status_text(order):
    return (
        ERRORS.get(order.error, STATUSES[order.state])
        if order.state == "failed"
        else STATUSES[order.state]
    )


def stored_quote(order):
    values = dict(order.quote or {})
    values.pop("session_binding", None)
    return Quote(**values)


def confirmation(order):
    from .formatting import html

    q = stored_quote(order)
    symbol = "€" if q.currency == "EUR" else "£"
    total = f"{q.total / 100:.2f}{symbol}"
    return {
        "text": (
            f"<b>Підтвердіть автобай на {total}</b>\n\n"
            f'<a href="{html(order.url, 2048)}">Товар у Vinted · {listing_identity(order.url)[0]}</a>\n'
            f"Товар: {q.item_price / 100:.2f}{symbol}\n"
            f"Доставка: {q.shipping / 100:.2f}{symbol}\n"
            f"Вибір у Vinted: {html(q.delivery_key, 500)}\n"
            f"Захист покупця: {q.protection / 100:.2f}{symbol}\n"
            f"<b>Разом: {total}</b>\n\nПідтвердження діє 90 секунд."
        ),
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data":
                     f"wallet:{'approve' if order.state == 'bot_quoted' else 'pay'}:{order.id}"},
                    {"text": "Скасувати", "callback_data": f"wallet:cancel:{order.id}"},
                ]
            ]
        },
    }


def notice(session, order):
    session.add(
        Notification(
            id=f"wallet:{order.id}:{order.state}",
            recipient_id=order.user_id,
            kind="text",
            payload=confirmation(order)
            if order.state in {"quoted", "bot_quoted"}
            else {"text": status_text(order)},
        )
    )


class WalletService:
    def __init__(self, sessions, resolver=None):
        self.sessions = sessions
        self.resolver = resolver or self.delivered_item
        self.registry = SourceRegistry.read(
            Path(__file__).resolve().parents[1] / "config/sources.json"
        )

    async def access(self, session, user, account, *, order=None):
        if account is None:
            return "NotConnected", None
        error, period = await autobuy_access(
            session, user, exclude_order=order.id if order else None
        )
        if error:
            return error, period
        chosen = allowed_sources(user, self.registry, await plan_for(session, user))
        region = "GB" if account.region == "UK" else account.region
        if not any(
            self.registry.sources[key].provider == "vinted"
            and region in self.registry.sources[key].regions
            for key in chosen
        ):
            return "SubscriptionRegionLimit", period
        return None, period

    async def delivered_item(self, user_id, message_id):
        # Callback arguments never contain an arbitrary purchase URL or price.
        async with self.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT item FROM vpars_monitor.deliveries "
                        "WHERE user_id=:uid AND message_id=:mid AND state='sent' LIMIT 1"
                    ),
                    {"uid": user_id, "mid": message_id},
                )
            ).first()
            if not row:
                raise UserError("Лот не знайдено серед надісланих тобі повідомлень.")
            import json

            return json.loads(row[0]) if isinstance(row[0], str) else row[0]

    async def connection_text(self, user_id, region=None):
        if not autobuy.AVAILABLE:
            return autobuy.SOON
        async with self.sessions() as session:
            acc = await wallet_account(session, user_id, region)
            if not acc:
                return CONNECT_TEXT
            text = (
                f"<b>Autobuy · Vinted {acc.region}</b>\n\n"
                f"Ліміт: {acc.max_total / 100:.2f} {acc.currency}\n"
            )
            from .wallet_handoff import handoff_for

            handoff = handoff_for(self.sessions)
            if button_checkout(self.sessions):
                connection = await wallet_session(session, user_id, acc.region)
                if connection and connection.error in SESSION_BLOCKS:
                    return text + ERRORS[connection.error]
                return text + (
                    "Buy надсилає повну суму покупки. Для оплати з балансу натисни Approve. "
                    "Результат прийде в цей чат; вікно оформлення не відкривається.\n"
                    "Якщо балансу не вистачає або бракує даних доставки — покупку зупиняємо.\n"
                    + ("✅ Автооплату ввімкнено. Перед оплатою перевіряємо повну суму."
                       if acc.enabled and acc.contract_hash else
                       "⏳ Автооплату ще не ввімкнено: потрібна успішна перевірка оформлення.")
                )
            if handoff and handoff.balance_first:
                return text + (
                    "Buy перевіряє повну суму й автоматично оплачує тільки з балансу Vinted.\n"
                    "Якщо коштів бракує — відкриває те саме оформлення для ручного вибору оплати.\n"
                    + ("⏳ Автооплата очікує перевірки оформлення цього акаунта.\n"
                       if not acc.enabled or not acc.contract_hash else "")
                    + ("⏳ Вікно Buy очікує підключення зовнішнього HTTPS."
                       if not handoff.base_url else "")
                )
            if handoff and not handoff.base_url:
                return text + "Ручну оплату підготовлено. Вікно покупки очікує підключення зовнішнього доступу. Оплата ботом вимкнена."
            if handoff:
                return text + (
                    "🛒 Buy відкриває особисте вікно Vinted. CAPTCHA та оплату підтверджуєш сам.\n"
                    "Vinted підставляє доступні збережені дані. Якщо чогось бракує — сайт попросить це додати.\n"
                    "Бот перевіряє ціну товару за лімітом; повну суму з доставкою перевір перед оплатою у Vinted."
                )
            if not acc.enabled or not acc.contract_hash:
                return text + (
                    "⏳ Підключення не завершене або вимкнене. Викуп вимкнений.\n"
                    "Відкрий FAQ → «Підключити автобай» і перевір стан сесії та оформлення."
                )
            from .wallet_browser import load_contract

            try:
                contract, digest = load_contract(user_id, acc.region)
                if digest != acc.contract_hash or contract["region"] != acc.region:
                    raise WalletError("ContractMismatch")
            except WalletError:
                return text + "⚠️ Потрібна повторна перевірка. Натисни «Перевірити оформлення» в цьому регіоні."
            error, _ = await self.access(session, await locked_user(session, user_id), acc)
            if error:
                return text + ERRORS[error]
            return text + (
                "✅ Підключення перевірене.\n"
                "Натисни Buy під лотом. Оплата — тільки з балансу після підтвердження суми. "
                "Перед кожною покупкою бот знову перевіряє оформлення у Vinted."
            )

    async def request(self, user_id, message_id):
        autobuy.require_available()
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            connected = await session.scalar(select(WalletAccount.user_id).where(
                WalletAccount.user_id == user_id, WalletAccount.enabled.is_(True),
                WalletAccount.contract_hash.is_not(None),
            ).limit(1))
            if connected is None:
                if button_checkout(self.sessions):
                    from .db import WalletSession

                    saved = await session.scalar(select(WalletSession.user_id).where(
                        WalletSession.user_id == user_id,
                        WalletSession.state.in_(["connected", "checkout_ready"]),
                        WalletSession.account_key.is_not(None),
                    ).limit(1))
                    if saved is not None:
                        raise UserError("Сесію Vinted збережено, але автооплату ще не ввімкнено: "
                                        "потрібна успішна перевірка оформлення й доставки. "
                                        "Покупку не виконано, гроші не списано.")
                raise UserError(CONNECT_TEXT)
            item = await self.resolver(user_id, message_id)
            region, item_id = listing_identity(item["item_url"])
            acc = await wallet_account(session, user_id, region)
            if not acc or not acc.enabled or not acc.contract_hash:
                raise UserError(f"Підключи автобай для Vinted {region}.\n\n" + CONNECT_TEXT)
            if region != acc.region or item["price"]["currency"] != acc.currency:
                raise UserError("Цей регіон не відповідає підключеному акаунту Vinted.")
            old = await session.scalar(
                select(WalletOrder).where(
                    WalletOrder.user_id == user_id,
                    WalletOrder.item_id == item_id,
                )
            )
            if old:
                return old
            error, _ = await self.access(session, user, acc)
            if error:
                raise WalletError(error)
            if acc.active_order:
                raise UserError("Спочатку заверши попередню покупку або перевір її результат.")
            price = minor(item["price"]["amount"])
            if price > acc.max_total:
                raise WalletError("BudgetExceeded")
            binding = None
            if button_checkout(self.sessions):
                binding = session_binding(acc, await wallet_session(session, user_id, region))
            order = WalletOrder(
                id=uuid4().hex,
                user_id=user_id,
                item_id=item_id,
                url=f"https://{REGIONS[region][0]}/items/{item_id}",
                listed_price=price,
                currency=acc.currency,
                state="bot_requested" if binding else "requested",
                quote={"session_binding": binding} if binding else None,
                expires_at=utcnow() + timedelta(seconds=90) if binding else None,
            )
            acc.active_order = order.id
            session.add(order)
        signals(self.sessions).wallet.set()
        return order

    async def decide(self, user_id, order_id, *, pay, approve=False):
        autobuy.require_available()
        from .wallet_handoff import handoff_for

        if pay and handoff_for(self.sessions):
            raise UserError("Оплату тепер підтверджуєш у вікні Vinted. Бот оплату не натискає.")
        bot = button_checkout(self.sessions)
        if pay and bot != approve:
            raise UserError("Це попереднє підтвердження покупки. Натисни Buy під товаром і підтвердь актуальну суму кнопкою Approve.")
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            order = await session.get(WalletOrder, order_id)
            if not order or order.user_id != user_id:
                raise UserError("Покупку не знайдено.")
            acc = await wallet_account(session, user_id, listing_identity(order.url)[0])
            if order.state != ("bot_quoted" if bot else "quoted"):
                return order
            if not pay:
                order.state, acc.active_order = "cancelled", None
            elif not acc.enabled or not acc.contract_hash:
                raise UserError(CONNECT_TEXT)
            elif order.expires_at.replace(tzinfo=UTC) <= utcnow():
                order.state, order.error, acc.active_order = "failed", "Expired", None
            else:
                error, _ = await self.access(session, user, acc, order=order)
                if error:
                    raise WalletError(error)
                if acc.active_order != order.id:
                    raise WalletError("NotConnected")
                if bot:
                    binding = session_binding(acc, await wallet_session(session, user_id, acc.region))
                    if not order.quote or order.quote.get("session_binding") != binding:
                        raise WalletError("NotConnected")
                stored_quote(order).check(order, acc)
                order.state = "bot_queued" if bot else "queued"
            order.updated_at = utcnow()
        signals(self.sessions).wallet.set()
        return order

    async def callback(self, callback):
        from aiogram.types import Message

        data = callback.data or ""
        if not autobuy.AVAILABLE and autobuy.callback_action(data):
            msg = callback.message
            if (isinstance(msg, Message) and msg.chat.type == "private"
                    and callback.from_user.id == msg.chat.id):
                if msg.reply_markup:
                    from aiogram.exceptions import TelegramAPIError
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text=autobuy.SOON, callback_data=button.callback_data)
                        if autobuy.callback_action(button.callback_data or "") else button
                        for button in row
                    ] for row in msg.reply_markup.inline_keyboard])
                    if keyboard != msg.reply_markup:
                        try:
                            await msg.edit_reply_markup(reply_markup=keyboard)
                        except TelegramAPIError:
                            pass  # Old messages may be uneditable; the action is still inert.
                await msg.answer(autobuy.SOON)
            return True
        if data != "lot:buy" and not data.startswith("wallet:"):
            return False
        msg = callback.message
        if (
            not isinstance(msg, Message)
            or msg.chat.type != "private"
            or callback.from_user.id != msg.chat.id
        ):
            return True
        try:
            if data == "lot:buy":
                from aiogram.types import InlineKeyboardMarkup

                from .wallet_handoff import handoff_for

                handoff = handoff_for(self.sessions)
                if handoff:
                    order = await handoff.request(callback.from_user.id, msg.message_id)
                    from .wallet_handoff import OPEN_STATES
                    from .wallet_links import direct_buy_keyboard

                    if order.state in OPEN_STATES and msg.reply_markup:
                        # Recovery for old cards missed by the bulk button upgrade.
                        # Telegram cannot launch a Mini App from a callback button.
                        await msg.edit_reply_markup(reply_markup=direct_buy_keyboard(
                            msg.reply_markup, f"{handoff.base_url}/purchase/{order.id}",
                        ))
                        await msg.answer("Кнопку Buy оновлено. Натисни її ще раз — відкриється оформлення без додаткової реєстрації.")
                        return True
                    payload = handoff.message(order)
                    markup = payload.get("reply_markup")
                    await msg.answer(payload["text"], parse_mode="HTML", reply_markup=(
                        InlineKeyboardMarkup.model_validate(markup) if markup else None
                    ))
                    return True
                order = await self.request(callback.from_user.id, msg.message_id)
            else:
                match = re.fullmatch(r"wallet:(pay|approve|cancel):([0-9a-f]{32})", data)
                if not match:
                    return True
                order = await self.decide(callback.from_user.id, match[2],
                                          pay=match[1] != "cancel", approve=match[1] == "approve")
            if order.state in {"quoted", "bot_quoted"}:
                from aiogram.types import InlineKeyboardMarkup

                payload = confirmation(order)
                await msg.answer(payload["text"], parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup.model_validate(payload["reply_markup"]))
            else:
                await msg.answer(status_text(order), parse_mode="HTML")
        except UserError as exc:
            await msg.answer(str(exc), parse_mode="HTML")
        except WalletError as exc:
            await msg.answer(ERRORS.get(str(exc), ERRORS["ContractMismatch"]), parse_mode="HTML")
        return True

    async def finish(self, order_id, state, *, error=None, external_id=None, quote=None,
                     expected_binding=None):
        async with self.sessions.begin() as session:
            order = await session.get(WalletOrder, order_id)
            user = await locked_user(session, order.user_id)
            await session.refresh(order)
            acc = await wallet_account(session, order.user_id, listing_identity(order.url)[0])
            allowed = {
                "bot_checking": {"bot_quoted", "failed"},
                "bot_executing": {"paying", "failed"},
                "balance_checking": {"paying", "failed", "human_waiting"},
                "checking": {"quoted", "failed"},
                "executing": {"paying", "failed"},
                "paying": {"succeeded", "unknown"},
            }
            if state not in allowed.get(order.state, set()):
                raise WalletError("ContractMismatch")
            if state in {"paying", "bot_quoted"}:
                error, period = await self.access(session, user, acc, order=order)
                if error:
                    raise WalletError(error)
                if not acc.enabled or not acc.contract_hash or acc.active_order != order.id:
                    raise WalletError("NotConnected")
                if order.state in {"bot_checking", "bot_executing"}:
                    connection = await wallet_session(session, order.user_id, acc.region)
                    binding = session_binding(acc, connection)
                    if (not button_checkout(self.sessions) or binding != expected_binding
                            or not order.quote or order.quote.get("session_binding") != binding
                            or quote is None or quote.account_key != binding[1]):
                        raise WalletError("NotConnected")
                elif order.state == "balance_checking":
                    connection = await wallet_session(session, order.user_id, acc.region)
                    if (not expected_binding or not connection
                            or connection.state not in {"connected", "checkout_ready"}
                            or connection.error == "CheckoutPaused"
                            or (acc.contract_hash, connection.account_key) != expected_binding):
                        raise WalletError("NotConnected")
                if order.expires_at.replace(tzinfo=UTC) <= utcnow():
                    raise WalletError("Expired")
                approved = quote if quote is not None else stored_quote(order)
                approved.check(order, acc)
                if state == "paying":
                    if order.state == "bot_executing" and not approved.same_purchase(stored_quote(order)):
                        raise WalletError("CheckoutChanged")
                    order.quota_period_start = period
            if order.state in {"bot_checking", "bot_executing"} and state == "failed" and error in SESSION_BLOCKS:
                connection = await wallet_session(session, order.user_id, acc.region)
                # The site rejected this particular saved connection. Do not reopen
                # it for a different item or after restart; an explicit successful
                # session probe clears the error. Never mark a replacement session.
                if (connection and connection.state in {"connected", "checkout_ready"}
                        and connection.error != "CheckoutPaused" and order.quote
                        and order.quote.get("session_binding") == [
                            acc.contract_hash, connection.account_key, connection.attempt_id,
                        ]):
                    connection.error, connection.updated_at = error, utcnow()
            order.state, order.error, order.updated_at = state, error, utcnow()
            if quote:
                quote.check(order, acc)
                order.quote = asdict(quote)
                if expected_binding and button_checkout(self.sessions):
                    order.quote["session_binding"] = expected_binding
                if state in {"quoted", "bot_quoted"} or not button_checkout(self.sessions):
                    order.expires_at = utcnow() + timedelta(seconds=90)
            if state in {"succeeded", "failed"}:
                acc.active_order = None
            if state == "succeeded":
                if not external_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", external_id):
                    raise WalletError("ContractMismatch")
                order.external_order_id = external_id
            if state not in {"paying", "human_waiting"}:
                notice(session, order)
        if state not in {"paying", "human_waiting"}:
            signals(self.sessions).notifications.set()
        return order
