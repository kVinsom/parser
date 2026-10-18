import pytest

from vpars_bot.dialogs import input_text, new_draft, transition
from vpars_bot.domain import Filters, UserError, parse_price


def test_draft_multivalue_back_skip_and_duplicate(registry):
    draft = new_draft()
    draft = input_text(draft, "Air Max 90", 1)
    draft = input_text(draft, "Air Force 1", 2)
    assert draft["values"]["models"] == ["Air Max 90", "Air Force 1"]
    assert input_text(draft, "Other", 2) == draft
    draft = transition(draft, "next", registry)
    draft = input_text(draft, "EU 42", 3)
    draft = transition(draft, "back", registry)
    assert draft["values"]["models"][0] == "Air Max 90"
    draft = transition(draft, "skip", registry)
    assert "models" not in draft["values"]
    assert draft["values"]["sizes"] == ["EU 42"]


@pytest.mark.parametrize("value", ["кросівки", "", "a" * 101, "<>!"])
def test_invalid_value_preserves_draft(value):
    draft = new_draft()
    with pytest.raises(UserError):
        input_text(draft, value, 1)
    assert draft["values"] == {} and draft["step"] == 0


@pytest.mark.parametrize(
    "text", ["80 20", "-2 40", "NaN 30", "0 Infinity", "1.111 3", "- -", "abc", "1e100 2e100"]
)
def test_invalid_prices(text):
    with pytest.raises(UserError):
        parse_price(text, "EUR")


def test_price_currency_independent_and_decimal():
    p = parse_price("0.10 80.15", "GBP")
    assert str(p.minimum) == "0.10" and p.currency == "GBP"
    assert parse_price("- 0", "EUR").maximum == 0


def test_region_required_and_alias(registry):
    with pytest.raises(UserError):
        Filters(models=["Nike"])
    f = registry.validate(Filters(sources={"vinted": ["UK", "GB", "FR"]}))
    assert f.sources == {"vinted_gb": ["GB"], "vinted_fr": ["FR"]}
    with pytest.raises(UserError):
        registry.validate(Filters(sources={"vinted": ["PL"]}))


async def test_full_handler_dialog_and_draft_cancel(chat, services):
    catalog, control, _ = services
    await chat.send("/start")
    await chat.click("Фільтр")
    await chat.click("Додати предмет")
    await chat.send("кросівки")
    assert "англійською" in chat.panel().text
    await chat.send("Air Max 90")
    await chat.click("Додати ще")
    await chat.send("Air Force 1")
    await chat.click("Далі")
    await chat.send("EU 42")
    await chat.click("Додати ще")
    await chat.send("EU 43")
    await chat.click("Назад")
    assert "Air Force 1" in chat.panel().text
    await chat.click("Далі")
    await chat.click("Далі")
    await chat.send("Very good")
    await chat.click("Далі")
    assert "бренд" in chat.panel().text
    await chat.send("Nike")
    await chat.click("Далі")
    await chat.click("Пропустити")
    await chat.click("Далі")  # Invalid until a region is selected.
    state = await chat.state()
    assert (await state.get_data())["draft"]["step"] == 7
    await chat.click("Vinted FR")
    await chat.click("Vinted DE")
    await chat.click("Далі")
    await chat.send("20 80")
    assert "EUR" in chat.panel().text
    await chat.send("80 20")
    assert "Перевір ціну" in chat.panel().text
    await chat.send("20 80")
    await chat.click("Далі")
    assert "Перевір параметри" in chat.panel().text
    save_data = await chat.click("Зберегти")
    await chat.click(data=save_data)  # Double-click cannot duplicate the item.
    items, count, _ = await catalog.list(101)
    assert count == 1
    item = items[0]
    assert item.filters["models"] == ["Air Max 90", "Air Force 1"]
    assert item.filters["sizes"] == ["EU 42", "EU 43"]
    assert item.filters["colors"] == []
    assert item.filters["sources"] == {"vinted_fr": ["FR"], "vinted_de": ["DE"]}
    await chat.click("Редагувати")
    await chat.send("Different")
    await chat.click("Скасувати")
    assert (await catalog.get(101, item.id)).filters == item.filters
    await chat.click("Почати пошук")
    assert "запуск не підтверджено" in chat.panel().text
    assert (await control.status(101))[item.id] == "requested"
    await chat.click("Завершити пошук")
    assert "Пошук зупинено" in chat.panel().text
    assert (await control.status(101))[item.id] == "stopped"


async def test_ownership_archive_restore_and_state_loss(chat, services):
    catalog, _, _ = services
    await chat.send("/start")
    item = await catalog.save(101, Filters(models=["Nike <&>"], sources={"vinted": ["FR"]}), "item")
    await chat.send(f"/search {item.id}")
    assert "Nike &lt;&amp;&gt;" in chat.panel().text
    old = chat.button("Архівувати")
    await chat.click("Архівувати")
    assert (await catalog.get(101, item.id)).archived
    await chat.click("Відновити")
    assert not (await catalog.get(101, item.id)).archived
    await chat.send("/start", user_id=202, username="bob")
    await chat.send(f"/search {item.id}", user_id=202, username="bob")
    assert "не знайдено" in chat.transport.calls[-1].text
    await chat.click(data=old, user_id=202, username="bob")
    assert not (await catalog.get(101, item.id)).archived
    await (await chat.state()).clear()
    await chat.click(data=old)
    assert "застаріла" in chat.transport.calls[-1].text
    await chat.send("/start")
    assert "Предметів: 1" in chat.panel().text


async def test_paginated_preserved_over_limit_cards(chat, services):
    from uuid import uuid4

    from vpars_bot.db import Search

    catalog, _, _ = services
    await chat.send("/start")
    for i in range(10):
        await catalog.save(
            101, Filters(models=[f"Item {i}"], sources={"vinted": ["GB"]}), f"item-{i}"
        )
    # Old over-limit records remain visible and removable after the policy upgrade.
    async with catalog.sessions.begin() as session:
        for i in range(10, 13):
            session.add(
                Search(
                    id=str(uuid4()),
                    user_id=101,
                    filters=Filters(models=[f"Item {i}"], sources={"vinted_gb": ["GB"]}).model_dump(
                        mode="json"
                    ),
                )
            )
    await chat.click("Фільтр")
    assert "Предметів: 13" in chat.panel().text
    await chat.click("→")
    assert "Сторінка 2" in chat.panel().text
    await chat.click("→")
    assert "Сторінка 3" in chat.panel().text
    assert (
        len(
            [
                b
                for row in chat.panel().reply_markup.inline_keyboard
                for b in row
                if ":card:" in b.callback_data
            ]
        )
        == 1
    )


async def test_fsm_reconstruction_keeps_draft(chat):
    await chat.send("/start")
    await chat.click("Фільтр")
    await chat.click("Додати предмет")
    await chat.send("Nike")
    from aiogram.fsm.context import FSMContext

    original = await chat.state()
    reconstructed = FSMContext(storage=original.storage, key=original.key)
    assert (await reconstructed.get_data())["draft"]["values"]["models"] == ["Nike"]


async def test_unknown_handler_errors_propagate_for_inbox_retry(chat, services, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(services[0], "register", AsyncMock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError):
        await chat.send("/start")


async def test_id_refreshes_registered_profile_but_never_registers_stranger(chat, services):
    await chat.send("/id")
    assert "101" in chat.transport.calls[-1].text
    with pytest.raises(UserError):
        await services[0].user(101)
    await chat.send("/start")
    await chat.send("/id", username="newname")
    assert (await services[0].user(101)).username == "newname"


async def test_malformed_callback_does_not_poison_inbox(chat):
    await chat.send("/start")
    state = await chat.state()
    nonce = (await state.get_data())["screen"]
    for action in [
        "list:bad:0",
        "list:0",
        "admin:wrong",
        "edit:uuid:not-a-version",
        "d:region|vinted",
    ]:
        await chat.click(data=f"{nonce}:{action}")
        assert "Кнопка недійсна" in chat.transport.calls[-1].text
    await chat.click("Фільтр")
    assert "Твої предмети" in chat.panel().text
