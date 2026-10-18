"""Telegram setup wizard; authentication and checkout verification stay in Vinted."""

import hashlib
import json
import re
from uuid import uuid4

from . import autobuy_availability as autobuy
from .application import locked_user, previous, remember
from .db import WalletAccount
from .domain import UserError
from .entitlements import MESSAGES, plan_for
from .profile_menu import FAQ_BACK
from .wallet import REGIONS, WalletError, minor
from .wallet_accounts import wallet_account
from .wallet_handoff import handoff_for
from .wallet_sessions import SessionService

BEGIN = "▶️ Почати підключення"
CONTINUE = "🔑 Додати ключ сесії"
SAVE = "✅ Зберегти й продовжити"
BACK = "← Назад до кроку"
EXIT = "↩️ До FAQ"
DELIVERY = "📦 До налаштування доставки"
CHECK = "🔄 Перевірити стан підключення"
PROBE = "🔎 Перевірити оформлення"
ENABLE = "✅ Активувати Buy"
COUNTRIES = {
    "DE": "🇩🇪 Німеччина · EUR",
    "FR": "🇫🇷 Франція · EUR",
    "IT": "🇮🇹 Італія · EUR",
    "UK": "🇬🇧 Велика Британія · GBP",
}


def account_settings(account):
    if account is None:
        return None
    return {
        "region": account.region,
        "currency": account.currency,
        "max_total": account.max_total,
        "enabled": account.enabled,
        "contract_hash": account.contract_hash,
    }


def revision(account):
    return hashlib.sha256(
        json.dumps(account_settings(account), sort_keys=True).encode()
    ).hexdigest()


def parse_limit(value):
    value = value.strip()
    if not re.fullmatch(r"[0-9]{1,7}(?:[.,][0-9]{1,2})?", value):
        raise UserError("Введи тільки суму, наприклад 100 або 100,50. Без адреси чи даних входу.")
    try:
        amount = minor(value.replace(",", "."))
    except WalletError as exc:
        raise UserError("Ліміт має бути від 0,01 до 1 000 000 у валюті акаунту.") from exc
    if amount <= 0:
        raise UserError("Ліміт має бути більшим за нуль.")
    return amount


class SetupService:
    def __init__(self, sessions):
        self.sessions = sessions

    async def draft(self, user_id, region=None):
        async with self.sessions() as session:
            account = await wallet_account(session, user_id, region)
            settings = account_settings(account)
            return {
                "step": "limit" if region else "region",
                "baseline": revision(account),
                "operation_id": "wallet_setup:" + uuid4().hex,
                "region": region or (settings["region"] if settings else None),
                "max_total": settings["max_total"] if settings else None,
            }

    async def save(self, user_id, draft):
        autobuy.require_available()
        region, limit = draft.get("region"), draft.get("max_total")
        if region not in REGIONS or type(limit) is not int or not 0 < limit <= 100_000_000:
            raise UserError("Повернися до вибору країни й ліміту.")
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            replay = await previous(session, user_id, draft["operation_id"], "wallet_setup")
            if replay is not None:
                return
            if not (await plan_for(session, user)).filters:
                raise UserError(MESSAGES["SubscriptionRequired"])
            account = await wallet_account(session, user_id, region)
            if account and account.active_order:
                raise UserError("Спочатку заверши попередню покупку або перевір її результат.")
            if revision(account) != draft["baseline"]:
                raise UserError("Налаштування вже змінилися. Відкрий «Підключити автобай» заново.")
            if account is None:
                account = WalletAccount(user_id=user_id, region=region, enabled=False)
                session.add(account)
            if account.region != region or account.max_total != limit:
                account.region, account.currency = region, REGIONS[region][1]
                account.max_total = limit
                # An explicit budget entry saves only this region; verification enables it later.
                account.enabled, account.contract_hash = False, None
            remember(session, user_id, draft["operation_id"], "wallet_setup", {"saved": True})


class AutobuyOnboarding:
    def __init__(self, ui):
        self.ui = ui
        self.service = SetupService(ui.catalog.sessions)
        self.sessions = SessionService(ui.catalog.sessions)

    async def open(self, state):
        await self.ui.view(state, "autobuy_setup", autobuy_draft={"step": "overview"}, draft=None)

    async def render(self, message, state):
        if not autobuy.AVAILABLE:
            return await self.ui.screen(message, state, autobuy.SOON, [[(FAQ_BACK, "ab:exit")]])
        draft = (await state.get_data()).get("autobuy_draft") or {"step": "overview"}
        step, region = draft["step"], draft.get("region")
        handoff = handoff_for(self.service.sessions)
        balance_first = bool(handoff and handoff.balance_first)
        human = bool(handoff and not balance_first)
        rows = []
        if step in {"overview", "region"} or region not in REGIONS:
            text = (
                "<b>Автобай за регіонами</b>\n\n"
                "Підключення за 3 кроки: <b>країна → ліміт → вхід</b>.\n"
                "У кожного регіону своя сесія, ліміт і перемикач.\n"
                "Доставку беремо зі збереженого вибору у Vinted. Якщо сайт не підставив "
                "адресу або пункт видачі, бот попросить вибрати їх у Vinted.\n\n"
                "Обери регіон:"
            )
            if human:
                text = (
                    "<b>Покупки у Vinted за регіонами</b>\n\n"
                    "Країна → ліміт → вхід. DE, FR, IT та UK мають окремі сесії.\n"
                    "Buy під товаром відкриває твоє вікно Vinted на телефоні або комп’ютері. "
                    "Один раз додай і збережи адресу у Vinted. Сайт підставляє збережені "
                    "адресу й доставку, якщо вони доступні для товару. "
                    "CAPTCHA та кнопку оплати натискаєш особисто.\n\nОбери регіон:"
                )
            for key, label in COUNTRIES.items():
                current = await self.service.draft(message.chat.id, key)
                if current["max_total"]:
                    async with self.service.sessions() as session:
                        account = await wallet_account(session, message.chat.id, key)
                        status = "сесію налаштовано" if human else ("увімкнено" if account.enabled else "вимкнено / підключення")
                    text += f"\n{label}: {status}"
                rows.append([(label, f"ab:region:{key}")])
        elif step == "limit":
            text = (
                f"<b>Крок 2/3 · Ліміт покупки</b>\n\n{COUNTRIES[region]}\n"
                f"Введи максимальну <b>повну суму однієї покупки</b> в {REGIONS[region][1]}: "
                "товар + доставка + захист покупця. Наприклад: 100 або 100,50.\n\n"
                "Надіслана сума зберігає ліміт цього регіону. Після входу бот автоматично "
                "перевірить готовність і ввімкне Buy. Кожна оплата потребує твого підтвердження."
            )
            if human:
                text = (
                    f"<b>Крок 2/3 · Ліміт покупки</b>\n\n{COUNTRIES[region]}\n"
                    f"Введи суму в {REGIONS[region][1]}, наприклад 100. "
                    "Бот не відкриватиме товари дорожче цього ліміту. "
                    "Повну суму з доставкою та захистом покупця перевіряєш перед оплатою у Vinted."
                )
        elif step == "login":
            current = await self.service.draft(message.chat.id, region)
            if not current.get("max_total"):
                await self.open(state)
                return await self.render(message, state)
            host, currency = REGIONS[region]
            text = (
                f"<b>Крок 3/3 · Вхід у Vinted {region}</b>\n\n"
                f"Ліміт: {current['max_total'] / 100:.2f} {currency}.\n\n"
                f"1. Увійди у свій акаунт на https://{host}/ у Chrome. "
                "Відкрий власний профіль і перевір, що бачиш свій нік.\n"
                "2. Онови сторінку після входу. Натисни F12 → Application → Cookies "
                "та вибери домен цієї сторінки Vinted.\n"
                "3. Скопіюй Value для <code>refresh_token_web</code> і надішли тут одним повідомленням.\n\n"
                "Країна сайту не змінює країну акаунту. Перевір країну в профілі та "
                "доступність потрібної адреси доставки у Vinted. "
                "<a href=\"https://www.vinted.de/help/1231\">Правила зміни країни Vinted</a>.\n\n"
                "Ключ надає доступ до акаунту. Зберігаємо його зашифрованим і пробуємо "
                "видалити повідомлення. Пароль і коди SMS не надсилай.\n\n"
                "Далі бот сам перевірить сесію, оформлення й доставку з Vinted. "
                "Якщо все готово, Buy увімкнеться для цього регіону. Підключення гроші не списує."
            )
            rows = [[(CHECK, "ab:check")]]
            if human:
                text = text.replace(
                    "Далі бот сам перевірить сесію, оформлення й доставку з Vinted. "
                    "Якщо все готово, Buy увімкнеться для цього регіону. Підключення гроші не списує.",
                    "Бот перевірить вхід. Потім натисни Buy під товаром: відкриється особисте "
                    "вікно Vinted. CAPTCHA та оплату підтверджуєш у ньому сам.",
                )
        else:
            text = f"<b>{COUNTRIES[region]}</b>\n\n"
            text += await self.sessions.status(message.chat.id, region) + "\n\n"
            text += await self.ui.wallet.connection_text(message.chat.id, region)
            rows = [[(CHECK, "ab:check")], [(CONTINUE, "ab:continue")],
                    [("💰 Змінити ліміт", "ab:limit")]]
            available = await self.sessions.actions(message.chat.id, region)
            if available.get("probe"):
                rows.insert(0, [(PROBE, "ab:probe")])
            if available.get("enable"):
                rows.insert(0, [(ENABLE, "ab:enable")])
            if available.get("human"):
                rows.insert(0, [("▶️ Продовжити покупки у Vinted", "ab:human")])
            async with self.service.sessions() as session:
                account = await wallet_account(session, message.chat.id, region)
            if account:
                rows.append([("⏹ Вимкнути цей регіон", "ab:disable")])
            text += "\n\nДоставку та повну суму перевір у вікні Vinted перед оплатою."
        if region in REGIONS:
            rows.append([("🌍 Інші регіони", "ab:begin")])
        rows.append([(EXIT, "ab:exit")])
        if balance_first:
            text = text.replace("Кожна оплата потребує твого підтвердження.",
                                "Buy запускає оплату з балансу автоматично в межах ліміту.")
            text += ("\n\nПісля Buy: якщо балансу вистачає на товар, доставку й комісію — "
                     "бот оплачує з нього. Якщо бракує — відкриває те саме оформлення "
                     "зі збереженими даними для ручного вибору оплати.")
        from .wallet_mode import button_checkout

        if button_checkout(self.service.sessions):
            text = text.replace("\n\nДоставку та повну суму перевір у вікні Vinted перед оплатою.", "")
            text += ("\n\nBuy → повна сума й збережена доставка → Approve → оплата з балансу → "
                     "результат у цьому чаті. Якщо коштів або даних бракує — зупиняємо покупку. "
                     "Відкривати вікно оформлення для кожної покупки не потрібно.")
        await self.ui.screen(message, state, text, rows)

    async def input(self, message, state, data, action):
        if not autobuy.AVAILABLE:
            if message.text == FAQ_BACK or action == "ab:exit":
                await self.ui.view(state, "faq", faq_topic="overview")
                return await self.ui.render(message, state)
            return await self.render(message, state)
        draft = dict(data.get("autobuy_draft") or {"step": "overview"})
        region = draft.get("region")
        if message.text == FAQ_BACK:
            return await self.ui.home(message, state)
        if action == "ab:exit":
            await self.ui.view(state, "faq", faq_topic="overview")
            return await self.ui.render(message, state)
        if action in {"ab:begin", "ab:back"}:
            draft = {"step": "overview"}
        elif action and action.startswith("ab:region:"):
            if not (await self.ui.catalog.plan(message.chat.id)).filters:
                raise UserError(MESSAGES["SubscriptionRequired"])
            region = action.rsplit(":", 1)[1]
            if region not in COUNTRIES:
                raise UserError("Обери країну кнопкою нижче.")
            draft = await self.service.draft(message.chat.id, region)
            draft["step"] = "check" if draft["max_total"] else "limit"
        elif region not in REGIONS:
            raise UserError("Скористайся кнопками й обери регіон автобаю.")
        elif action == "ab:limit":
            draft = await self.service.draft(message.chat.id, region)
            draft["step"] = "limit"
        elif action == "ab:continue":
            draft = {"step": "login", "region": region}
        elif action in {"ab:check", "ab:delivery"}:
            draft = {"step": "check", "region": region}
        elif action == "ab:probe":
            await self.sessions.probe(message.chat.id, region, auto_enable=True)
            draft = {"step": "check", "region": region}
        elif action == "ab:enable":
            await self.sessions.enable(message.chat.id, region)
            draft = {"step": "check", "region": region}
        elif action == "ab:disable":
            await self.sessions.disable(message.chat.id, region)
            draft = {"step": "check", "region": region}
        elif action == "ab:human":
            await self.sessions.resume_human(message.chat.id, region)
            draft = {"step": "check", "region": region}
        elif draft["step"] == "limit":
            draft["max_total"] = parse_limit(message.text or "")
            await self.service.save(message.chat.id, draft)
            available = await self.sessions.actions(message.chat.id, region)
            if available.get("human_connected"):
                await self.sessions.resume_human(message.chat.id, region)
                draft = {"step": "check", "region": region}
            elif available.get("probe"):
                await self.sessions.probe(message.chat.id, region, auto_enable=True)
                draft = {"step": "check", "region": region}
            else:
                draft = {"step": "login", "region": region}
        elif action == "ab:save" and draft["step"] == "login":
            pass  # Harmless replay from the previous five-step keyboard.
        else:
            raise UserError("Скористайся кнопками поточного кроку.")
        await self.ui.view(state, "autobuy_setup", autobuy_draft=draft)
        await self.ui.render(message, state)
