import pytest
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, SendMessage
from conftest import ChatHarness, OfflineSession
from sqlalchemy import func, select

from vpars_bot.db import ControlCommand, Operation, RoleEvent, Search, User
from vpars_bot.domain import Filters
from vpars_bot.menu_preview import WELCOME_TEXT, make_preview_dispatcher


@pytest.mark.parametrize("is_admin", [False, True])
async def test_preview_role_menu_and_old_buttons_never_mutate_search_or_draft(services, is_admin):
    catalog, control, _ = services
    await catalog.register(101, 101, "alice", "Test", 1, start=True)
    item = await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "saved-filter")
    await control.start(101, "existing-search")
    async with catalog.sessions.begin() as session:
        (await session.get(User, 101)).is_admin = is_admin

    async def snapshot():
        async with catalog.sessions() as session:
            return [
                await session.scalar(select(func.count()).select_from(model))
                for model in (Search, Operation, ControlCommand, RoleEvent)
            ]

    before = await snapshot()
    transport = OfflineSession()
    bot = Bot("42:offline", session=transport)
    storage = MemoryStorage()
    dispatcher = make_preview_dispatcher(storage, SimpleEventIsolation(), catalog)
    chat = ChatHarness(dispatcher, bot, transport)
    state = await chat.state()
    await state.set_state("filters")
    draft = {"draft": {"values": {"models": ["Nike"]}, "step": 2}}
    await state.set_data(draft)
    try:
        await chat.send("/start")
        panel = chat.panel()
        assert panel.text == WELCOME_TEXT
        assert panel.reply_markup.is_persistent is True
        assert panel.reply_markup.resize_keyboard is True
        assert panel.reply_markup.one_time_keyboard is False
        labels = [b.text for row in panel.reply_markup.keyboard for b in row]
        assert any(("Видати" if is_admin else "Купити") in label for label in labels)
        assert not any(("Купити" if is_admin else "Видати") in label for label in labels)
        for label in labels:
            await chat.send(label)
        for callback in [
            "preview:search_stop",
            "old:stop",
            "old:start",
            "old:confirm_admin",
            "anything",
        ]:
            await chat.click(data=callback)
            assert isinstance(transport.calls[-1], AnswerCallbackQuery)
        for text in ("/stop", "/search " + item.id, "Nike", "Додати фільтр", "/example"):
            await chat.send(text)
        assert sum(isinstance(call, SendMessage) for call in transport.calls) == 1
        assert await snapshot() == before
        assert await state.get_data() == draft and await state.get_state() == "filters"
        user = await catalog.user(101)
        assert user.is_admin == is_admin and user.delivery_epoch == 0
        assert (await control.status(101))[item.id] == "requested"
    finally:
        await storage.close()
        await bot.session.close()
