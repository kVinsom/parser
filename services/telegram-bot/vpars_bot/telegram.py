import asyncio
import hashlib
import json
import re
from decimal import Decimal
from secrets import token_hex
from uuid import uuid4

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .application import AdminService, Catalog
from .dialogs import input_text, new_draft, skip_unavailable, transition
from .domain import (
    FIELDS,
    PRICE_PROMPT,
    STEPS,
    Filters,
    SourceRegistry,
    UserError,
    normalize_username,
)
from .formatting import Lot, filter_summary, html, short
from .lot_messages import prepare_lot, send_lot_message
from .search_control import SearchControlPort
from .update_context import current_update

STATUS = {
    "stopped": "зупинено",
    "requested": "запит збережено; запуск не підтверджено",
    "active": "працює",
    "demo": "демонстрація — реального пошуку немає",
}
ICONS = {"stopped": "⏹", "requested": "⏳", "active": "▶️", "demo": "🧪"}
QUESTIONS = {
    "models": "👟 <b>Яку модель шукаємо?</b>\n\nВведи назву англійською.\nНаприклад: Air Max 90",
    "sizes": "📏 <b>Який розмір?</b>\n\nВведи розмір, наприклад: EU 42",
    "conditions": "✨ <b>У якому стані?</b>\n\nВведи значення англійською, наприклад: Very good",
    "colors": "🎨 <b>Який колір?</b>\n\nВведи колір англійською, наприклад: Black",
    "brands": "🏷 <b>Який бренд?</b>\n\nВведи бренд, наприклад: Nike",
    "keywords": "🔤 <b>Які ключові слова?</b>\n\nВведи одну фразу англійською.",
    "categories": "📂 <b>Яка категорія?</b>\n\nВведи назву англійською.",
}


class TelegramProfileVerifier:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def verify(self, user_id: int, username: str | None):
        try:
            async with asyncio.timeout(5):
                chat = await self.bot.get_chat(user_id)
            if (
                chat.id != user_id
                or chat.type != "private"
                or (normalize_username(chat.username) if chat.username else None)
                != (normalize_username(username) if username else None)
            ):
                raise UserError(
                    "Нік отримувача змінився. Нехай він виконає /start, потім повтори вибір."
                )
        except (TelegramAPIError, TimeoutError):
            raise UserError(
                "Не вдалося перевірити отримувача. Права не змінено. Спробуй пізніше."
            ) from None


class BotUI:
    def __init__(
        self,
        catalog: Catalog,
        control: SearchControlPort,
        admins: AdminService,
        registry: SourceRegistry,
        *,
        development=False,
    ):
        self.catalog, self.control, self.admins, self.registry = catalog, control, admins, registry
        self.development = development

    async def screen(self, message: Message, state: FSMContext, text: str, rows):
        data = await state.get_data()
        signature = hashlib.sha256(
            json.dumps(
                [text, rows, (data.get("draft") or {}).get("operation_id")], ensure_ascii=False
            ).encode()
        ).hexdigest()
        if data.get("panel_signature") == signature and data.get("panel_id"):
            return
        nonce = token_hex(4)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=label, callback_data=f"{nonce}:{action}")
                    for label, action in row
                ]
                for row in rows
            ]
        )
        await state.update_data(
            pending_panel={
                "text": text,
                "markup": markup.model_dump(mode="json"),
                "nonce": nonce,
                "signature": signature,
                "update_id": data.get("render_update_id"),
            }
        )
        await self.flush_screen(message, state)

    async def flush_screen(self, message, state):
        data = await state.get_data()
        pending = data["pending_panel"]
        text = pending["text"]
        markup = InlineKeyboardMarkup.model_validate(pending["markup"])
        panel_id = data.get("panel_id")
        delivered = False
        if panel_id:
            try:
                await message.bot.edit_message_text(
                    text,
                    chat_id=message.chat.id,
                    message_id=panel_id,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
                delivered = True
            except TelegramBadRequest as exc:
                if "message is not modified" in exc.message.lower():
                    delivered = True
                elif not any(
                    reason in exc.message.lower()
                    for reason in (
                        "message to edit not found",
                        "message can't be edited",
                        "message_id_invalid",
                    )
                ):
                    raise
        if not delivered:
            sent = await message.answer(text, reply_markup=markup, parse_mode="HTML")
            panel_id = sent.message_id
        await state.update_data(
            panel_id=panel_id,
            screen=pending["nonce"],
            panel_signature=pending["signature"],
            pending_panel=None,
        )

    async def clear_draft(self, state):
        await state.set_state(None)
        await state.update_data(draft=None)

    async def home(self, message, state, notice=""):
        user_id = message.chat.id
        user, count, states = await asyncio.gather(
            self.catalog.user(user_id), self.catalog.count(user_id), self.control.status(user_id)
        )
        values = set(states.values())
        status = (
            "requested"
            if "requested" in values
            else "demo"
            if "demo" in values
            else "active"
            if "active" in values
            else "stopped"
        )
        text = (notice + "\n\n" if notice else "") + (
            "👋 <b>Вітаю!</b>\n\nНалаштуй речі, які шукаєш, і запускай пошук.\n\n"
            f"Предметів: {count}\nПошук: {STATUS[status]}"
        )
        rows = [
            [("🔎 Фільтр", "list:0:0")],
            [("▶️ Почати пошук", "start"), ("⏹ Завершити пошук", "stop")],
        ]
        if user.is_admin:
            rows += [
                [("👤 Додати адміністратора", "admin:1")],
                [("🚫 Зняти права адміністратора", "admin:0")],
            ]
        await self.screen(message, state, text, rows)

    async def listing(self, message, state, page=0, archived=False):
        items, count, page = await self.catalog.list(message.chat.id, page, archived)
        statuses = await self.control.status(message.chat.id)
        title = "📦 <b>Архів предметів</b>" if archived else "🔎 <b>Твої предмети</b>"
        rows = []
        for item in items:
            name = (item.filters.get("models") or item.filters.get("brands") or ["Предмет"])[0]
            status = "📦" if archived else ICONS[statuses.get(item.id, "stopped")]
            rows.append([(f"{status} {short(name, 40)} · {item.id[:8]}", f"card:{item.id}")])
        paging = []
        if page:
            paging.append(("←", f"list:{page - 1}:{int(archived)}"))
        if (page + 1) * 6 < count:
            paging.append(("→", f"list:{page + 1}:{int(archived)}"))
        if paging:
            rows.append(paging)
        rows += [
            [("➕ Додати предмет", "new")],
            [("🔎 Поточні" if archived else "📦 Архів", f"list:0:{int(not archived)}")],
            [("🏠 Меню", "home")],
        ]
        await self.screen(
            message,
            state,
            f"{title}\n\nПредметів: {count} · Сторінка {page + 1}\nВідкрити за ID: /search ID",
            rows,
        )

    async def card(self, message, state, search_id):
        item = await self.catalog.get(message.chat.id, search_id)
        statuses = await self.control.status(message.chat.id)
        status = "в архіві" if item.archived else STATUS[statuses.get(item.id, "stopped")]
        text = f"🔎 <b>Картка предмета</b>\nID: <code>{item.id}</code>\nСтан: {status}\n\n"
        text += filter_summary(Filters.model_validate(item.filters), self.registry)
        args = f"{item.id}:{item.version}"
        rows = (
            [[("♻️ Відновити", f"restore:{args}")]]
            if item.archived
            else [
                [("✏️ Редагувати", f"edit:{args}"), ("📦 Архівувати", f"archive:{args}")],
                [("▶️ Почати пошук", f"run:{args}"), ("⏹ Завершити пошук", "stop")],
            ]
        )
        rows += [[("← До предметів", "list:0:0"), ("🏠 Меню", "home")]]
        await self.screen(message, state, text, rows)

    async def draft_screen(self, message, state, notice="", *, registry=None):
        registry = registry or self.registry
        draft = (await state.get_data())["draft"]
        draft = skip_unavailable(draft, registry)
        await state.update_data(draft=draft)
        if draft["values"].get("sources"):
            draft["values"]["sources"] = registry.normalize_sources(draft["values"]["sources"])
            await state.update_data(draft=draft)
        step = STEPS[draft["step"]]
        rows = []
        if step in FIELDS:
            text = QUESTIONS[step]
            model = draft["values"].get("models", [])
            if step != "models" and model:
                text += f"\n\nМодель: {html(', '.join(model), 140)}"
            current = draft["values"].get(step, [])
            if current:
                text += f"\n\nДодано: {html(', '.join(current), 500)}"
            unsupported = [s.label for s in registry.sources.values() if step in s.unsupported]
            if unsupported:
                text += f"\n\n⚠️ {html(', '.join(unsupported), 100)} не підтримує цей параметр. Натисни «Пропустити», щоб явно прибрати старе обмеження перед збереженням."
            rows += [
                [("➕ Додати ще", "d:more"), ("Далі →", "d:next")],
                [("Пропустити", "d:skip"), ("← Назад", "d:back")],
            ]
        elif step == "regions":
            text = "🛍️ <b>Де шукаємо?</b>\n\nВибери хоча б один маркетплейс."
            for key, source in registry.sources.items():
                chosen = draft["values"].get("sources", {}).get(key, [])
                buttons = [
                    (
                        f"{'✅ ' if r in chosen else ''}{registry.region_label(key, r)}",
                        f"d:region|{key}|{r}",
                    )
                    for r in source.regions
                ]
                rows += [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows += [[("Далі →", "d:next"), ("← Назад", "d:back")]]
        elif step == "price":
            text = PRICE_PROMPT
            if draft["values"].get("price"):
                p = draft["values"]["price"]
                low = p["minimum"] if p["minimum"] is not None else "0"
                high = p["maximum"] if p["maximum"] is not None else "∞"
                text += f"\nДодано: {low}–{high} {p['currency']}"
            rows += [
                [("Далі →", "d:next"), ("Пропустити", "d:skip")],
                [("← Назад", "d:back")],
            ]
        else:
            filters = registry.validate(Filters.model_validate(draft["values"]))
            text = "🔎 <b>Перевір параметри</b>\n\n" + filter_summary(filters, registry)
            issues = await registry.issues(filters)
            text += "\n\n" + html(" ".join(issues) if issues else registry.notice(), 400)
            rows += [[("✅ Зберегти", "save"), ("✏️ Змінити", "d:change")], [("← Назад", "d:back")]]
        rows += [[("Скасувати", "cancel")]]
        if notice:
            text = html(notice, 350) + "\n\n" + text
        await self.screen(message, state, text, rows)

    async def callback(self, callback, state, update_id):
        message = callback.message
        data = await state.get_data()
        parts = (callback.data or "").split(":")
        admitted_stop = len(parts) == 2 and parts[1] == "stop" and current_update.get().stop_key
        if len(parts) < 2 or (parts[0] != data.get("screen") and not admitted_stop):
            raise UserError("Ця кнопка застаріла. Виконай /start, щоб відкрити меню.")
        action, args = parts[1], parts[2:]
        arity = {
            "list": 2,
            "card": 1,
            "edit": 2,
            "archive": 2,
            "restore": 2,
            "run": 2,
            "admin": 1,
            "d": 1,
        }
        invalid = len(args) != arity.get(action, 0)
        if not invalid and action in {"edit", "archive", "restore", "run"}:
            invalid = re.fullmatch(r"[0-9]{1,10}", args[1]) is None
        if not invalid and action == "list":
            invalid = re.fullmatch(r"[0-9]{1,10}", args[0]) is None or args[1] not in {"0", "1"}
        if not invalid and action == "admin":
            invalid = args[0] not in {"0", "1"}
        if not invalid and action == "d":
            invalid = (
                args[0]
                not in {"back", "change", "more", "skip", "next", "currency-EUR", "currency-GBP"}
                and re.fullmatch(r"region\|[a-z0-9_-]{1,24}\|[A-Z]{2}", args[0]) is None
            )
        if invalid:
            raise UserError("Кнопка недійсна. Відкрий меню командою /start.")
        user_id = callback.from_user.id
        key = f"button:{callback.data}"
        if action == "stop":
            await self.control.stop(user_id, key)
            await self.clear_draft(state)
            await self.home(
                message,
                state,
                "⏹ <b>Пошук зупинено</b>\n\nНові повідомлення про лоти більше не надсилатимуться.\n"
                "Усі предмети та фільтри збережені.\nВже розпочате надсилання може ще завершитися.",
            )
        elif action in {"home", "cancel"}:
            await self.clear_draft(state)
            await self.home(message, state, "Зміни скасовано." if action == "cancel" else "")
        elif action == "list":
            await self.clear_draft(state)
            await self.listing(message, state, int(args[0]), bool(int(args[1])))
        elif action == "card":
            await self.clear_draft(state)
            await self.card(message, state, args[0])
        elif action in {"new", "edit"}:
            item = await self.catalog.get(user_id, args[0]) if action == "edit" else None
            if item and (item.archived or item.version != int(args[1])):
                raise UserError("Картка змінилася. Відкрий її знову.")
            await state.set_state("filters")
            await state.update_data(
                draft=new_draft(
                    item.filters if item else None,
                    item.id if item else None,
                    item.version if item else None,
                )
            )
            await self.draft_screen(message, state)
        elif action == "d":
            if not data.get("draft") or data["draft"]["kind"] != "filters":
                raise UserError("Чернетка завершилася. Додай або відкрий предмет знову.")
            draft = transition(data["draft"], args[0], self.registry)
            await state.update_data(draft=draft)
            await self.draft_screen(message, state)
        elif action == "save":
            draft = data.get("draft")
            if not draft or draft["kind"] != "filters" or STEPS[draft["step"]] != "review":
                raise UserError("Чернетка завершилася. Відкрий предмет знову.")
            item = await self.catalog.save(
                user_id,
                Filters.model_validate(draft["values"]),
                draft["operation_id"],
                search_id=draft["search_id"],
                version=draft["version"],
            )
            await self.clear_draft(state)
            await self.card(message, state, item.id)
        elif action in {"archive", "restore", "run"}:
            item = await self.catalog.get(user_id, args[0])
            if item.version != int(args[1]):
                raise UserError("Картка змінилася. Відкрий її знову.")
            if action == "run":
                await self.control.start(user_id, key, item.id)
            else:
                await self.catalog.archive(user_id, item.id, action == "archive", item.version, key)
            await self.card(message, state, item.id)
        elif action == "start":
            await self.control.start(user_id, key)
            await self.home(message, state, "Запит на пошук збережено.")
        elif action == "admin":
            await self.admins.require(user_id)
            await state.set_state("admin")
            await state.update_data(
                draft={"kind": "admin", "new_role": args[0] == "1", "operation_id": str(uuid4())}
            )
            await self.screen(
                message,
                state,
                "👤 <b>Введи @username отримувача</b>\n\nВін має спочатку виконати /start.",
                [[("Скасувати", "cancel")]],
            )
        elif action == "confirm_admin":
            draft = data.get("draft")
            if not draft or draft["kind"] != "admin" or "recipient_id" not in draft:
                raise UserError("Підтвердження застаріло. Повтори вибір отримувача.")
            await self.admins.change(
                user_id,
                draft["recipient_id"],
                draft["username"],
                draft["new_role"],
                draft["old_role"],
                draft["operation_id"],
            )
            await self.clear_draft(state)
            await self.home(message, state, "✅ Права адміністратора оновлено.")
        else:
            raise UserError("Відкрий меню командою /start.")

    async def text(self, message, state, update_id):
        from . import autobuy_availability as autobuy

        if not autobuy.AVAILABLE and autobuy.text_action(message.text or ""):
            await message.answer(autobuy.SOON)
            return
        data = await state.get_data()
        draft = data.get("draft")
        if not draft:
            await self.home(message, state, "Вибери дію в меню.")
        elif draft["kind"] == "filters":
            try:
                changed = input_text(draft, message.text or "", update_id)
                await state.update_data(draft=changed)
                await self.draft_screen(message, state)
            except UserError as exc:
                await self.draft_screen(message, state, str(exc))
        elif draft["kind"] == "admin":
            recipient = await self.admins.resolve(message.from_user.id, message.text or "")
            draft.update(
                recipient_id=recipient.id,
                username=recipient.username_key,
                old_role=recipient.is_admin,
            )
            await state.update_data(draft=draft)
            action = "Надати" if draft["new_role"] else "Зняти"
            await self.screen(
                message,
                state,
                f"👤 <b>Перевір отримувача</b>\n\n@{html(recipient.username)}\n{html(recipient.full_name)}\n"
                f"ID: <code>{recipient.id}</code>\n\n{action} права адміністратора?",
                [[("✅ Підтвердити", "confirm_admin"), ("Скасувати", "cancel")]],
            )


class PanelRecoveryMiddleware(BaseMiddleware):
    def __init__(self, ui):
        self.ui = ui

    async def __call__(self, handler, event, data):
        state = data["state"]
        update_id = data["event_update"].update_id
        saved = await state.get_data()
        pending = saved.get("pending_panel")
        message = event.message if isinstance(event, CallbackQuery) else event
        if pending and pending.get("update_id") == update_id and isinstance(message, Message):
            await self.ui.flush_screen(message, state)
            return
        # A later event after a failed/expired update supersedes only that obsolete panel.
        await state.update_data(render_update_id=update_id, pending_panel=None)
        return await handler(event, data)


def make_dispatcher(storage, isolation, ui: BotUI, *, acknowledge_callbacks=True):
    dp = Dispatcher(storage=storage, events_isolation=isolation)
    router = Router()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    router.message.middleware(PanelRecoveryMiddleware(ui))
    router.callback_query.middleware(PanelRecoveryMiddleware(ui))

    async def refresh(message, update_id):
        await ui.catalog.register(
            message.from_user.id,
            message.chat.id,
            message.from_user.username,
            message.from_user.full_name,
            update_id,
        )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext, event_update):
        await ui.catalog.register(
            message.from_user.id,
            message.chat.id,
            message.from_user.username,
            message.from_user.full_name,
            event_update.update_id,
            start=True,
        )
        await ui.clear_draft(state)
        await state.update_data(panel_signature=None)
        await ui.home(message, state)

    @router.message(Command("id"))
    async def own_id(message: Message, event_update):
        try:
            await refresh(message, event_update.update_id)
        except UserError:
            pass  # /id also works before registration; only /start creates a user.
        await message.answer(
            f"Твій Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML"
        )

    @router.message(Command("example"))
    async def example(message: Message):
        if not ui.development:
            raise UserError("Приклади доступні лише в режимі розробки.")
        lot = Lot(
            title="Nike Air Max 90",
            brand="Nike",
            price=Decimal("45"),
            currency="EUR",
            source_label="Vinted",
            region="FR",
            url="https://www.vinted.fr/",
            search_name="Air Max 90",
            size="EU 42",
            condition="Very good",
            color="Black",
        )
        await send_lot_message(message.bot, message.chat.id, await prepare_lot(lot, demo=True))

    @router.message(Command("search"))
    async def search(message: Message, command: CommandObject, state: FSMContext, event_update):
        await refresh(message, event_update.update_id)
        await ui.clear_draft(state)
        await ui.card(message, state, (command.args or "").strip())

    @router.message(Command("stop"))
    async def stop_command(message: Message, state: FSMContext, event_update):
        await refresh(message, event_update.update_id)
        await ui.control.stop(message.from_user.id, f"update:{event_update.update_id}:stop")
        await ui.clear_draft(state)
        await ui.home(message, state, "⏹ Пошук зупинено. Усі предмети та фільтри збережені.")

    @router.callback_query()
    async def callback(callback: CallbackQuery, state: FSMContext, event_update):
        if acknowledge_callbacks:
            try:
                async with asyncio.timeout(0.7):
                    await callback.answer(request_timeout=0.7)
            except (TelegramAPIError, TimeoutError):
                pass  # An expired/slow acknowledgement must never prevent Stop or a DB action.
        from .wallet import WalletService

        if await WalletService(ui.catalog.sessions).callback(callback):
            return
        if not isinstance(callback.message, Message):
            return
        await ui.catalog.register(
            callback.from_user.id,
            callback.message.chat.id,
            callback.from_user.username,
            callback.from_user.full_name,
            event_update.update_id,
        )
        await ui.callback(callback, state, event_update.update_id)

    @router.message()
    async def text(message: Message, state: FSMContext, event_update):
        await ui.catalog.register(
            message.from_user.id,
            message.chat.id,
            message.from_user.username,
            message.from_user.full_name,
            event_update.update_id,
        )
        await ui.text(message, state, event_update.update_id)

    @router.errors()
    async def errors(event):
        if isinstance(event.exception, UserError):
            message = event.update.message or (
                event.update.callback_query and event.update.callback_query.message
            )
            if isinstance(message, Message):
                await message.answer(html(str(event.exception), 500), parse_mode="HTML")
            return True
        if isinstance(event.exception, TelegramForbiddenError):
            # No retries can deliver menus to a user who blocked the bot.
            return True
        return UNHANDLED

    dp.include_router(router)
    return dp
