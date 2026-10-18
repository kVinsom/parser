"""Persistent reply menu: marketplace selection and saved filter dialogs."""

import asyncio
from collections import Counter
from datetime import datetime

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup

from .domain import Filters, SourceRegistry, UserError
from .entitlements import MESSAGES
from .formatting import filter_name, filter_summary, html
from .keyword_dialog import (
    build_filters,
    input_value,
    new_filter_draft,
    render_draft,
    upgrade_draft,
)
from .menu_preview import FILTER_SETTINGS, WELCOME_TEXT, menu_keyboard
from .profile_menu import (
    CONNECT_AUTOBUY,
    FAQ,
    FAQ_TEXT,
    INSTRUCTION_CALLBACK,
    PAGES,
    PROFILE,
    faq_rows,
)
from .session_secrets import SESSION_INPUT, incoming_session_key, private_session_key
from .subscription_payments import (
    RECEIPT_PENDING,
    RECEIPT_PROMPT,
    SUBSCRIPTION_TEXT,
    SubscriptionPayments,
    checkout_text,
    offer_rows,
)
from .subscriptions import (
    GRANT_SUBSCRIPTION,
    LEVELS,
    SubscriptionService,
    expiry_label,
    parse_days,
    profile_text,
)
from .telegram import STATUS, BotUI
from .update_context import SEARCH_START, SEARCH_START_LABELS, SEARCH_STOP_LABELS
from .wallet import WalletService
from .wallet_onboarding import AutobuyOnboarding

HOME = "🏠 Головне меню"
MARKETS = "🛍️ Маркетплейси"
ADD = "🟢 Додати фільтр"
REMOVE = "❌ Забрати фільтр"
ENABLED_FILTERS = "✅ Включені фільтри"
BACK = "← Назад"
BUY_SUBSCRIPTION = "💳 Купити підписку"
MENU_REFRESHED = "Меню оновлено. Обери потрібну дію ще раз."


class ReplyMenuUI(BotUI):
    def __init__(self, catalog, control, admins=None):
        super().__init__(catalog, control, admins, catalog.registry)
        self.subscriptions = SubscriptionService(catalog.sessions, admins)
        self.payments = SubscriptionPayments(catalog.sessions)
        self.wallet = WalletService(catalog.sessions)
        self.onboarding = AutobuyOnboarding(self)

    async def view(self, state, name, **values):
        data = await state.get_data()
        if name != "grant_subscription":
            values.setdefault("subscription_draft", None)
        if name != "autobuy_setup":
            values.setdefault("autobuy_draft", None)
        await state.update_data(
            reply_view=name,
            reply_applied_update=data.get("reply_update_id"),
            reply_notice="",
            **values,
        )

    async def screen(self, message, state, text, rows):
        actions = {label: action for row in rows for label, action in row}
        # A queued second tap may carry the label shown before the first toggle.
        for label, action in list(actions.items()):
            if action.startswith(("market:", "filter:", "d:region|")):
                plain = label.removeprefix("✅ ").removeprefix("❌ ").removeprefix("⬜ ")
                # Keep old square labels usable until the user's keyboard refreshes.
                actions.update({prefix + plain: action for prefix in ("", "✅ ", "❌ ", "⬜ ")})
        await state.update_data(reply_actions=actions)
        await message.answer(
            text,
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=label) for label, _ in row] for row in rows],
                resize_keyboard=True,
                is_persistent=True,
                one_time_keyboard=False,
            ),
            parse_mode="HTML",
        )

    async def home(self, message, state, notice=""):
        await self.view(state, "home", draft=None)
        await state.update_data(reply_notice=notice)
        await self.render(message, state)

    async def filter_settings(self, message, state, notice=""):
        await self.view(state, "filter_settings", draft=None)
        await state.update_data(reply_notice=notice)
        await self.render(message, state)

    async def enabled_registry(self, user_id):
        if not (await self.catalog.plan(user_id)).filters:
            raise UserError(MESSAGES["SubscriptionRequired"])
        chosen = await self.catalog.marketplaces(user_id)
        if not chosen:
            raise UserError("Спочатку увімкни хоча б один майданчик у меню «Маркетплейси».")
        return SourceRegistry(
            {k: v for k, v in self.registry.sources.items() if k in chosen},
            exchange=self.registry.exchange,
        )

    async def stop(self, message, state, update_id):
        await self.control.stop(message.chat.id, f"reply:{update_id}:stop")
        await self.view(state, "search_status", draft=None, search_started=False)
        await self.render(message, state)

    async def render(self, message, state):
        data = await state.get_data()
        view = data.get("reply_view", "home")
        notice = data.get("reply_notice", "")
        prefix = html(notice, 500) + "\n\n" if notice else ""
        if view in {"home", "search_status"}:
            user, count, states, problems = await asyncio.gather(
                self.catalog.user(message.chat.id),
                self.catalog.count(message.chat.id, enabled_only=view == "search_status"),
                self.control.status(message.chat.id),
                self.control.problems(message.chat.id),
            )
            status = next(
                (value for value in ("requested", "demo", "active") if value in states.values()),
                "stopped",
            )
            await state.update_data(reply_actions={})
            issues = (
                "\n\n" + "\n".join("⚠️ " + html(problem) for problem in problems) if problems else ""
            )
            if view == "search_status":
                chosen = await self.catalog.marketplaces(message.chat.id)
                markets = (
                    ", ".join(
                        source.label
                        for key, source in self.registry.sources.items()
                        if key in chosen
                    )
                    or "—"
                )
                heading = (
                    "✅ Пошук початий" if data.get("search_started") else "❌ Пошук завершений"
                )
                body = f"{heading}\n\nФільтрів: {count}\nМаркетплейси: {html(markets, 500)}"
            else:
                body = (
                    prefix
                    + WELCOME_TEXT
                    + f"\n\nФільтрів: {count}\nПошук: {STATUS[status]}"
                    + issues
                )
            await message.answer(
                body,
                reply_markup=menu_keyboard(
                    is_admin=user.is_admin, search_active=status != "stopped"
                ),
                parse_mode="HTML",
            )
        elif view == "subscriptions":
            await self.screen(
                message,
                state,
                SUBSCRIPTION_TEXT,
                offer_rows() + [[(BACK, "home")]],
            )
        elif view == "subscription_checkout":
            order = await self.payments.get(message.chat.id, data["subscription_order_id"])
            await self.screen(
                message,
                state,
                checkout_text(order),
                [[(BACK, "subscriptions")]],
            )
        elif view == "subscription_received":
            await self.screen(message, state, RECEIPT_PENDING, [[(BACK, "home")]])
        elif view == "grant_subscription":
            await self.subscriptions.require_admin(message.chat.id)
            step = data["subscription_draft"]["step"]
            rows = []
            if step == "recipient":
                text = "Введи @username або ID користувача."
            elif step == "level":
                text = "Обери рівень підписки."
                rows = [[("LITE", "grant:LITE"), ("PRO", "grant:PRO")], [("ULTRA", "grant:ULTRA")]]
            else:
                text = (
                    "Введи кількість днів: 10 або +10 — додати, -5 — відняти.\n"
                    "Для активної підписки дні рахуються від поточної дати завершення.\n"
                    "Якщо відняти весь залишок або більше, підписка завершиться одразу."
                )
            if step != "recipient":
                rows.append([(BACK, "grant_back")])
            rows.append([("Скасувати", "grant_cancel")])
            await self.screen(message, state, text, rows)
        elif view == "autobuy_setup":
            await self.onboarding.render(message, state)
        elif view in {"faq", "profile"}:
            # Existing profile help screens can survive deployment in Redis.
            topic = data.get("faq_topic" if view == "faq" else "profile_topic", "overview")
            rows = faq_rows()
            if topic == "connect":
                await self.onboarding.open(state)
                await self.onboarding.render(message, state)
            else:
                text = FAQ_TEXT if topic not in PAGES else PAGES[topic][1]
                await self.screen(message, state, text, rows)
        elif view == "filter_settings":
            plan = await self.catalog.plan(message.chat.id)
            count = await self.catalog.count(message.chat.id)
            await self.screen(
                message,
                state,
                prefix
                + f"⚙️ <b>Налаштування фільтрів</b>\n{plan.level} · Фільтри: {count}/{plan.filters}",
                [
                    [(ADD, "add"), (REMOVE, "remove")],
                    [(ENABLED_FILTERS, "enabled_filters")],
                    [(MARKETS, "markets")],
                    [(BACK, "home")],
                ],
            )
        elif view == "markets":
            chosen = await self.catalog.marketplaces(message.chat.id)
            plan = await self.catalog.plan(message.chat.id)
            lines, rows = [], []
            for key, source in self.registry.sources.items():
                active = key in chosen
                label = ("✅ " if active else "❌ ") + source.label
                lines.append(html(label))
                rows.append([(label, "market:" + key)])
            text = prefix + "🛍️ <b>Маркетплейси</b>\n\n" + "\n".join(lines)
            text += "\n\nНатисни, щоб увімкнути / вимкнути."
            text += f"\n{plan.level} · Регіони: {len(chosen)}/{plan.regions or ('усі' if plan.filters else 0)}"
            if not chosen:
                text += "\n\nЖоден маркетплейс не вибрано."
            rows.append([(BACK, "filter_settings")])
            await self.screen(message, state, text, rows)
        elif view == "filters":
            registry = await self.enabled_registry(message.chat.id)
            draft = upgrade_draft(data["draft"], self.registry)
            plan = await self.catalog.plan(message.chat.id)
            if draft["step"] == "excluded_keywords" and not plan.exclusions:
                draft = input_value(draft, "", 0, skip=True)
            await state.update_data(draft=draft)
            text, rows = render_draft(draft, registry)
            await self.screen(message, state, prefix + text, rows)
        elif view in {"remove", "enabled_filters"}:
            items, count, page = await self.catalog.list(message.chat.id, data.get("reply_page", 0))
            rows = []
            names = [filter_name(item.filters) for item in items]
            duplicates = Counter(names)
            selecting = view == "enabled_filters"
            for index, (item, name) in enumerate(zip(items, names, strict=True)):
                # Reply buttons send their labels: identical names need a visible row number.
                if duplicates[name] > 1:
                    name += f" (№{page * 6 + index + 1})"
                marker = ("✅ " if item.enabled else "❌ ") if selecting else "🗑 "
                action = "filter" if selecting else "remove"
                rows.append([(marker + name, f"{action}:{item.id}")])
            paging = []
            page_action = "enabled_page" if selecting else "page"
            if page:
                paging.append(("← Попередні", f"{page_action}:{page - 1}"))
            if (page + 1) * 6 < count:
                paging.append(("Наступні →", f"{page_action}:{page + 1}"))
            if paging:
                rows.append(paging)
            if selecting:
                enabled_count = await self.catalog.count(message.chat.id, enabled_only=True)
                plan = await self.catalog.plan(message.chat.id)
                text = (
                    f"Увімкнено: {enabled_count} з {count}.\n\n"
                    "✅ — увімкнений, ❌ — вимкнений.\n"
                    "Натисни на фільтр, щоб змінити його стан.\n"
                    "Вимкнення одразу зупиняє пошук за цим фільтром.\n"
                    "Для запуску за вибраними фільтрами натисни «Почати пошук»."
                    if count
                    else "Фільтрів поки немає. Натисни «Додати фільтр» у налаштуваннях."
                )
                if count and not enabled_count:
                    text += "\n\nУвімкни хоча б один фільтр, щоб почати пошук."
                if count > plan.filters:
                    text += f"\n\nЛіміт {plan.level}: {plan.filters}. Збережені фільтри понад ліміт призупинені; їх можна прибрати."
                if enabled_count:
                    rows.append([(SEARCH_START, "start")])
                heading = "✅ <b>Включені фільтри</b>"
            else:
                text = (
                    f"Фільтрів: {count}. Обери для видалення." if count else "Фільтрів поки немає."
                )
                heading = "❌ <b>Забрати фільтр</b>"
            rows.append([(BACK, "filter_settings")])
            await self.screen(message, state, prefix + heading + "\n\n" + text, rows)
        elif view == "remove_confirm":
            item = await self.catalog.get(message.chat.id, data["reply_remove_id"])
            text = "🗑 <b>Забрати фільтр?</b>\n\n"
            text += filter_summary(Filters.model_validate(item.filters), self.registry)
            await self.screen(
                message,
                state,
                prefix + text,
                [
                    [("✅ Так, забрати", "remove_confirm")],
                    [("← Назад", "remove_back"), (HOME, "home")],
                ],
            )

    async def text(self, message, state, update_id):
        from . import autobuy_availability as autobuy

        token = incoming_session_key.get() or private_session_key(message)
        if token or message.text == SESSION_INPUT:
            # This branch precedes all free-text dialogs so a key cannot become a filter.
            try:
                await message.delete()
            except TelegramAPIError:
                pass  # Do not echo a key if Telegram cannot remove the original message.
            if not autobuy.AVAILABLE:
                await message.answer(autobuy.SOON)
                return
            data = await state.get_data()
            if data.get("reply_view") != "autobuy_setup" or (
                data.get("autobuy_draft") or {}
            ).get("step") != "login":
                raise UserError("Відкрий FAQ → Підключити автобай → Додати ключ сесії. Ключ не збережено в налаштуваннях.")
            if not token:
                raise UserError("Не вдалося прочитати ключ. Відкрий підключення заново.")
            region = (data.get("autobuy_draft") or {}).get("region")
            if region not in {"DE", "FR", "IT", "UK"}:
                raise UserError("Обери регіон у меню підключення автобаю заново.")
            await self.onboarding.sessions.submit(
                message.chat.id, token, f"session:{update_id}", region=region, auto_enable=True,
            )
            await self.view(state, "autobuy_setup", autobuy_draft={"step": "check", "region": region})
            await self.render(message, state)
            return
        if message.photo or message.document:
            await self.receipt(message, state)
            return
        text = message.text or ""
        if not autobuy.AVAILABLE:
            data = await state.get_data()
            action = data.get("reply_actions", {}).get(text, "")
            if autobuy.text_action(text) or autobuy.callback_action(action):
                await message.answer(autobuy.SOON)
                return
        if text.startswith("/"):
            await self.home(message, state, "Скористайся кнопками меню нижче.")
            return
        if text == BUY_SUBSCRIPTION:
            await self.view(state, "subscriptions", draft=None, subscription_level=None)
            await self.render(message, state)
            return
        if text == GRANT_SUBSCRIPTION:
            await self.subscriptions.require_admin(message.chat.id)
            await self.view(
                state,
                "grant_subscription",
                draft=None,
                subscription_draft={
                    "step": "recipient",
                    "operation_id": f"reply:{update_id}:grant",
                },
            )
            await self.render(message, state)
            return
        if text in SEARCH_START_LABELS:
            await self.control.start(message.chat.id, f"reply:{update_id}:start")
            await self.view(state, "search_status", draft=None, search_started=True)
            await self.render(message, state)
            return
        if text in SEARCH_STOP_LABELS:
            await self.stop(message, state, update_id)
            return
        data = await state.get_data()
        if text == HOME:
            await self.home(message, state)
            return
        if text == FILTER_SETTINGS:
            await self.filter_settings(message, state)
            return
        if text == PROFILE:
            user = await self.catalog.user(message.chat.id)
            await message.answer(
                profile_text(await self.subscriptions.get(message.chat.id), is_admin=user.is_admin),
                parse_mode="HTML",
            )
            return
        if text == FAQ:
            await self.view(state, "faq", draft=None, faq_topic="overview")
            await self.render(message, state)
            return
        if text in {CONNECT_AUTOBUY, "Підключити Autobuy"} and data.get("reply_view") in {
            None, "home", "search_status", "faq", "profile", "autobuy_setup",
        }:
            # Telegram retains the keyboard after Redis expires the dialog. This is
            # navigation only: restore the country picker, never a stale wallet action.
            await self.onboarding.open(state)
            await self.render(message, state)
            return
        if data.get("reply_view") == "autobuy_setup":
            await self.onboarding.input(
                message, state, data, data.get("reply_actions", {}).get(text)
            )
            return
        if text == MARKETS:
            await self.view(state, "markets", draft=None)
        elif text in {ADD, "➕ Додати фільтр"}:
            await self.enabled_registry(message.chat.id)
            plan = await self.catalog.plan(message.chat.id)
            if await self.catalog.count(message.chat.id) >= plan.filters:
                raise UserError(
                    f"{plan.level}: можна зберегти до {plan.filters} фільтрів. Прибери зайвий або зміни тариф."
                )
            await self.view(state, "filters", draft=new_filter_draft(self.registry))
        elif text in {REMOVE, "➖ Забрати фільтр"}:
            await self.view(state, "remove", draft=None, reply_page=0)
        elif text == ENABLED_FILTERS:
            await self.view(state, "enabled_filters", draft=None, reply_page=0)
        else:
            action = data.get("reply_actions", {}).get(text)
            view = data.get("reply_view")
            # Reply keyboards saved before this release used the English label.
            if view in {"faq", "profile"} and text in {"Підключити Autobuy", "Підключити автобай"}:
                action = "faq:connect"
            if view == "grant_subscription":
                await self.grant_input(message, state, data, text)
                return
            if action == "home":
                await self.home(message, state)
                return
            if action in {"filter_settings", "cancel"}:
                await self.filter_settings(
                    message, state, "Зміни скасовано." if action == "cancel" else ""
                )
                return
            if action == "subscriptions":
                await self.view(state, "subscriptions")
                await self.render(message, state)
                return
            if view == "subscriptions" and action and action.startswith("offer:"):
                _, level, months = action.split(":")
                order = await self.payments.choose(
                    message.chat.id,
                    level,
                    int(months),
                    f"reply:{update_id}:order",
                )
                await self.view(state, "subscription_checkout", subscription_order_id=order.id)
                await self.render(message, state)
                return
            if view == "subscription_checkout":
                raise UserError(RECEIPT_PROMPT)
            if view == "subscriptions":
                # Refresh keyboards left over from the old tier-only placeholder.
                await self.view(state, "subscriptions")
                await self.render(message, state)
                return
            if view in {"faq", "profile"} and action and action.startswith(("faq:", "profile:")):
                topic = action.split(":", 1)[1]
                if topic in PAGES or topic == "connect":
                    await self.view(state, "faq", faq_topic=topic)
                    await self.render(message, state)
                return
            if view == "markets" and action and action.startswith("market:"):
                if action.split(":", 1)[1] not in self.registry.sources:
                    await self.view(state, "markets")
                    await state.update_data(reply_notice="Вибери окремий маркетплейс за країною.")
                    await self.render(message, state)
                    return
                await self.catalog.toggle_marketplace(
                    message.chat.id, action.split(":", 1)[1], f"reply:{update_id}:market"
                )
                await self.view(state, "markets")
            elif view == "enabled_filters" and action and action.startswith("filter:"):
                await self.catalog.toggle_filter(
                    message.chat.id, action.split(":", 1)[1], f"reply:{update_id}:filter"
                )
                await self.view(state, "enabled_filters")
            elif view == "enabled_filters" and action and action.startswith("enabled_page:"):
                await self.view(state, "enabled_filters", reply_page=int(action.split(":")[1]))
            elif view == "filters":
                draft = data.get("draft")
                if not draft:
                    raise UserError("Чернетка завершилася. Натисни «Додати фільтр».")
                registry = await self.enabled_registry(message.chat.id)
                if draft.get("kind") != "keyword_filter":
                    await self.view(state, "filters", draft=upgrade_draft(draft, self.registry))
                    await state.update_data(
                        reply_notice="Діалог оновлено. Введи ключові слова заново."
                    )
                    await self.render(message, state)
                    return
                if ":" in draft["step"]:
                    # Never reinterpret an answer to the old GBP prompt as EUR.
                    await self.view(state, "filters", draft=upgrade_draft(draft, self.registry))
                    await state.update_data(reply_notice="Тепер ціну вводимо один раз у євро.")
                    await self.render(message, state)
                    return
                if text in {
                    "Далі →",
                    "➕ Додати ще",
                    "✅ Зберегти",
                    "✏️ Змінити",
                    "← Назад",
                }:
                    raise UserError("Введи ключові слова через кому або натисни «Скасувати».")
                draft = upgrade_draft(draft, self.registry)
                plan = await self.catalog.plan(message.chat.id)
                if draft["step"] == "excluded_keywords" and not plan.exclusions:
                    # Redis may still contain a prompt from a more permissive plan.
                    draft = input_value(draft, "", update_id, skip=True)
                    await self.view(state, "filters", draft=draft)
                    await self.render(message, state)
                    return
                if text == "Пропустити" and action not in {"price_skip", "exclusions_skip"}:
                    raise UserError("Введи ключові слова через кому.")
                draft = input_value(
                    draft, text, update_id, skip=action in {"price_skip", "exclusions_skip"}
                )
                if draft["step"] != "complete":
                    await self.view(state, "filters", draft=draft)
                    await self.render(message, state)
                    return
                filters = build_filters(draft, registry)
                await self.catalog.save(message.chat.id, filters, draft["operation_id"])
                await self.filter_settings(
                    message, state, "✅ Фільтр збережено: " + ", ".join(filters.keywords)
                )
                return
            elif view == "remove" and action and action.startswith("remove:"):
                item = await self.catalog.get(message.chat.id, action.split(":", 1)[1])
                if item.archived:
                    raise UserError("Цей набір уже прибрано.")
                await self.view(
                    state,
                    "remove_confirm",
                    reply_remove_id=item.id,
                    reply_remove_version=item.version,
                )
            elif view == "remove" and action and action.startswith("page:"):
                await self.view(state, "remove", reply_page=int(action.split(":")[1]))
            elif view == "remove_confirm" and action == "remove_back":
                await self.view(state, "remove")
            elif view == "remove_confirm" and action == "remove_confirm":
                await self.catalog.archive(
                    message.chat.id,
                    data["reply_remove_id"],
                    True,
                    data["reply_remove_version"],
                    f"reply:{update_id}:remove",
                )
                await self.view(state, "remove")
                await state.update_data(reply_notice="✅ Фільтр видалено.")
            else:
                if data.get("reply_view") is None:
                    await self.home(message, state, MENU_REFRESHED)
                else:
                    await self.render(message, state)
                return
        await self.render(message, state)

    async def receipt(self, message, state):
        data = await state.get_data()
        order_id = data.get("subscription_order_id")
        if not order_id:
            order = await self.payments.awaiting_receipt(message.chat.id)
            order_id = order.id if order else None
        if not order_id:
            raise UserError("Спочатку обери підписку в «💳 Купити підписку».")
        if message.photo:
            kind, file_id = "photo", message.photo[-1].file_id
        elif message.document and (
            message.document.mime_type == "application/pdf"
            or (message.document.mime_type or "").startswith("image/")
        ):
            kind, file_id = "document", message.document.file_id
        else:
            raise UserError(RECEIPT_PROMPT)
        await self.payments.submit(message.chat.id, order_id, kind, file_id, message.message_id)
        await self.view(state, "subscription_received", subscription_order_id=order_id)
        await self.render(message, state)

    async def grant_input(self, message, state, data, text):
        await self.subscriptions.require_admin(message.chat.id)
        if text == "Скасувати":
            await self.home(message, state, "Видачу скасовано.")
            return
        draft = dict(data["subscription_draft"])
        step = draft["step"]
        if text == BACK and step != "recipient":
            draft["step"] = "recipient" if step == "level" else "level"
        elif step == "recipient":
            recipient = await self.subscriptions.resolve_recipient(message.chat.id, text)
            draft.update(recipient_id=recipient.id, username=recipient.username_key, step="level")
        elif step == "level":
            level = text.strip().upper()
            if level not in LEVELS:
                raise UserError("Обери LITE, PRO або ULTRA.")
            draft.update(level=level, step="days")
        else:
            days = parse_days(text)
            result = await self.subscriptions.grant(
                message.chat.id,
                draft["recipient_id"],
                draft["username"],
                draft["level"],
                days,
                draft["operation_id"],
            )
            expiry = expiry_label(datetime.fromisoformat(result["expires_at"]))
            recipient_label = (
                "@" + result["username"] if result["username"] else str(result["recipient_id"])
            )
            outcome = (
                "Підписку видано"
                if result["days"] > 0
                else "Термін підписки скорочено"
                if result["active"]
                else "Підписку завершено"
            )
            await self.home(
                message,
                state,
                f"✅ {outcome}: {recipient_label}\n"
                f"{result['level']} · {result['days']:+d} днів\nДо {expiry}",
            )
            return
        await self.view(state, "grant_subscription", subscription_draft=draft)
        await self.render(message, state)


class ReplyRecoveryMiddleware(BaseMiddleware):
    def __init__(self, ui):
        self.ui = ui

    async def __call__(self, handler, event, data):
        state, update_id = data["state"], data["event_update"].update_id
        saved = await state.get_data()
        if saved.get("reply_done_update", -1) >= update_id:
            return
        await state.update_data(reply_update_id=update_id)
        try:
            if saved.get("reply_applied_update") == update_id:
                # State/DB already committed; retry only the response, never the toggle or step.
                await self.ui.render(event, state)
            else:
                await handler(event, data)
        except UserError as exc:
            await event.answer(html(str(exc), 500), parse_mode="HTML")
        except TelegramForbiddenError:
            pass
        await state.update_data(reply_done_update=update_id)


def make_menu_dispatcher(
    storage, isolation, catalog, control, *, admins=None, acknowledge_callbacks=True
):
    dispatcher = Dispatcher(storage=storage, events_isolation=isolation)
    router = Router()
    ui = ReplyMenuUI(catalog, control, admins)
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    router.message.middleware(ReplyRecoveryMiddleware(ui))

    @router.message(CommandStart())
    async def start(message, state, event_update):
        await catalog.register(
            message.from_user.id,
            message.chat.id,
            message.from_user.username,
            message.from_user.full_name,
            event_update.update_id,
            start=True,
        )
        if admins is not None:
            await admins.claim_support_role(message.from_user.id, message.from_user.username)
        await ui.home(message, state)

    @router.message(Command("id"))
    async def own_id(message):
        await message.answer(
            f"Твій Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML"
        )

    @router.message(Command("stop"))
    async def stop_command(message, state, event_update):
        await ui.stop(message, state, event_update.update_id)

    @router.callback_query()
    async def old_button(callback: CallbackQuery, state):
        if acknowledge_callbacks:
            try:
                async with asyncio.timeout(0.7):
                    await callback.answer(request_timeout=0.7)
            except (TelegramAPIError, TimeoutError):
                pass
        if await ui.wallet.callback(callback):
            return
        if callback.data == INSTRUCTION_CALLBACK and isinstance(callback.message, Message):
            await ui.onboarding.open(state)
            await ui.render(callback.message, state)
        elif (isinstance(callback.message, Message)
              and callback.from_user.id == callback.message.chat.id):
            # Old inline menus must not execute their obsolete actions, but they
            # should still replace an unusable keyboard with the current menu.
            try:
                await ui.home(callback.message, state, MENU_REFRESHED)
            except UserError as exc:
                await callback.message.answer(html(str(exc), 500), parse_mode="HTML")

    @router.message()
    async def text(message, state, event_update):
        await catalog.register(
            message.from_user.id,
            message.chat.id,
            message.from_user.username,
            message.from_user.full_name,
            event_update.update_id,
        )
        await ui.text(message, state, event_update.update_id)

    dispatcher.include_router(router)
    return dispatcher
