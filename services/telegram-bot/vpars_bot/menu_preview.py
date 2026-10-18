"""The current menu is a visual preview; only /start and /id respond."""

import asyncio

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup

from . import autobuy_availability as autobuy
from .profile_menu import FAQ, PROFILE
from .update_context import SEARCH_START, SEARCH_STOP

WELCOME_TEXT = "👋 <b>Вітаю у Blade parser!</b>"
FILTER_SETTINGS = "⚙️ Налаштування фільтрів"


def menu_keyboard(*, is_admin: bool, search_active: bool = False) -> ReplyKeyboardMarkup:
    subscription = "🎟️ Видати підписку" if is_admin else "💳 Купити підписку"
    rows = [
        [SEARCH_STOP if search_active else SEARCH_START, subscription],
        [FILTER_SETTINGS, PROFILE],
        [FAQ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Обери пункт меню",
    )


def make_preview_dispatcher(storage, isolation, catalog, *, acknowledge_callbacks=True):
    dispatcher = Dispatcher(storage=storage, events_isolation=isolation)
    router = Router()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")

    @router.message(CommandStart())
    async def start(message: Message, event_update):
        user = await catalog.register(
            message.from_user.id,
            message.chat.id,
            message.from_user.username,
            message.from_user.full_name,
            event_update.update_id,
            start=True,
        )
        await message.answer(
            WELCOME_TEXT,
            reply_markup=menu_keyboard(is_admin=user.is_admin),
            parse_mode="HTML",
        )

    @router.message(Command("id"))
    async def own_id(message: Message):
        await message.answer(
            f"Твій Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML"
        )

    @router.callback_query()
    async def idle_button(callback: CallbackQuery):
        # Consume old functional callbacks too; no search, filter or role mutation is reachable.
        if acknowledge_callbacks:
            try:
                async with asyncio.timeout(0.7):
                    await callback.answer(request_timeout=0.7)
            except (TelegramAPIError, TimeoutError):
                pass
        if not autobuy.AVAILABLE:
            from .wallet import WalletService

            await WalletService(catalog.sessions).callback(callback)

    @router.message()
    async def idle_message(message: Message):
        # Reply buttons send text; ignore it and legacy commands in the visual preview.
        if not autobuy.AVAILABLE and autobuy.text_action(message.text or ""):
            await message.answer(autobuy.SOON)

    dispatcher.include_router(router)
    return dispatcher
