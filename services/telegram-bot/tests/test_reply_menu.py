import pytest
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import SendMessage
from conftest import ChatHarness, OfflineSession
from sqlalchemy import select

from vpars_bot.application import Catalog
from vpars_bot.db import ControlCommand, SearchRun, User
from vpars_bot.domain import PRICE_PROMPT, Filters
from vpars_bot.reply_menu import (
    ADD,
    BACK,
    ENABLED_FILTERS,
    FILTER_SETTINGS,
    HOME,
    MARKETS,
    REMOVE,
    ReplyMenuUI,
    make_menu_dispatcher,
)
from vpars_bot.search_control import WorkToken
from vpars_bot.update_context import SEARCH_START, SEARCH_STOP


@pytest.fixture
async def menu(services):
    transport = OfflineSession()
    bot = Bot("42:offline", session=transport)
    storage = MemoryStorage()
    dp = make_menu_dispatcher(
        storage, SimpleEventIsolation(), services[0], services[1], admins=services[2]
    )
    yield ChatHarness(dp, bot, transport)
    await dp.fsm.close()
    await bot.session.close()


def labels(chat, user_id=101):
    return [b.text for row in chat.panel(user_id).reply_markup.keyboard for b in row]


@pytest.mark.parametrize("is_admin", [False, True])
async def test_menu_roles_marketplaces_toggle_persist_and_isolate(
    menu, services, db, registry, is_admin
):
    catalog = services[0]
    await menu.send("/start")
    async with catalog.sessions.begin() as session:
        (await session.get(User, 101)).is_admin = is_admin
    await menu.send("/start")
    assert ("🎟️ Видати підписку" if is_admin else "💳 Купити підписку") in labels(menu)
    assert MARKETS not in labels(menu)
    await menu.send("/start", user_id=202, username="bob")
    await menu.send(FILTER_SETTINGS)
    assert MARKETS in labels(menu)
    await menu.send(MARKETS)
    assert labels(menu) == ["✅ Vinted UK", "✅ Vinted FR", "✅ Vinted IT", "✅ Vinted DE", BACK]
    assert "PL" not in menu.panel().text
    await menu.send("✅ Vinted FR")
    toggle_id = menu.update_id
    assert "❌ Vinted FR" in labels(menu)
    await menu.send("✅ Vinted FR", update_id=toggle_id)
    remaining = {"vinted_gb", "vinted_it", "vinted_de"}
    assert await catalog.marketplaces(101) == remaining
    assert await Catalog(db[1], registry).marketplaces(101) == remaining
    assert await catalog.marketplaces(202) == set(registry.sources)
    for country in ("UK", "IT", "DE"):
        await menu.send("✅ Vinted " + country)
    await menu.send(ADD)
    assert "Спочатку увімкни" in menu.transport.calls[-1].text
    # A second tap queued from the old label still toggles the current selection.
    await menu.send("✅ Vinted FR")
    assert await catalog.marketplaces(101) == {"vinted_fr"}
    await menu.send(BACK)
    assert labels(menu) == [ADD, REMOVE, ENABLED_FILTERS, MARKETS, BACK]
    await menu.send(BACK)
    assert FILTER_SETTINGS in labels(menu)
    assert MARKETS not in labels(menu)
    assert ADD not in labels(menu) and REMOVE not in labels(menu)
    await menu.send(FILTER_SETTINGS)
    assert [[b.text for b in row] for row in menu.panel().reply_markup.keyboard] == [
        [ADD, REMOVE],
        [ENABLED_FILTERS],
        [MARKETS],
        [BACK],
    ]
    await menu.send(ADD)
    assert "Введи ключові слова" in menu.panel().text
    await menu.send("Скасувати")
    assert labels(menu) == [ADD, REMOVE, ENABLED_FILTERS, MARKETS, BACK]
    await menu.send(REMOVE)
    assert "поки немає" in menu.panel().text
    await menu.send(BACK)
    assert labels(menu) == [ADD, REMOVE, ENABLED_FILTERS, MARKETS, BACK]
    await menu.send(BACK)
    assert FILTER_SETTINGS in labels(menu) and SEARCH_START in labels(menu)
    assert await catalog.count(101) == 0


async def test_home_explains_failed_search_and_stop_clears_notice(menu, services):
    catalog, control, _ = services
    await menu.send("/start")
    item = await catalog.save(
        101,
        Filters(
            brands=["Nike"], models=["Air Max 90"], sizes=["EU 42"], sources={"vinted_fr": ["FR"]}
        ),
        "error-test",
    )
    await control.start(101, "start-error-test")
    async with catalog.sessions.begin() as session:
        run = await session.get(SearchRun, (item.id, "vinted_fr"))
        run.last_error = "UnsupportedFilter:sizes"
    await menu.send(HOME)
    assert "Nike, Air Max 90, EU 42 (Vinted FR)" in menu.panel().text
    assert "Не вдалося перевірити розміри" in menu.panel().text
    await menu.send(SEARCH_STOP)
    assert "Не вдалося перевірити" not in menu.panel().text


async def test_keywords_then_prices_save_and_remove_shows_every_word(menu, services):
    catalog = services[0]
    await menu.send("/start")
    assert "Blade parser" in menu.panel().text
    await menu.send(ADD)
    assert "Введи ключові слова через кому" in menu.panel().text
    assert labels(menu) == ["Скасувати"]
    await menu.send("Nike,,L")
    assert "Введи слова через кому" in menu.transport.calls[-1].text
    assert await catalog.count(101) == 0
    await menu.send("Nike <&>, hoodie, л, бавовна")
    save_id = menu.update_id
    await menu.send("Nike <&>, hoodie, л, бавовна", update_id=save_id)
    from vpars_bot.keyword_dialog import EXCLUSIONS_PROMPT

    assert menu.panel().text == EXCLUSIONS_PROMPT
    await menu.send("Fake, Hole, Junior, Damaged, kids.")
    await menu.send("Fake, Hole, Junior, Damaged, kids.", update_id=menu.update_id)
    assert menu.panel().text == PRICE_PROMPT
    assert labels(menu) == ["Пропустити", "Скасувати"]
    assert await catalog.count(101) == 0
    await menu.send("0 80,50")
    await menu.send("0 80,50", update_id=menu.update_id)
    items, count, _ = await catalog.list(101)
    assert count == 1
    filters = items[0].filters
    assert filters["keywords"] == ["Nike <&>", "hoodie", "L", "бавовна"]
    assert filters["keywords_all"] and filters["follow_enabled_sources"]
    assert filters["excluded_keywords"] == ["Fake", "Hole", "Junior", "Damaged", "kids"]
    assert not any(
        filters[field]
        for field in (
            "brands",
            "models",
            "sizes",
            "colors",
            "categories",
            "conditions",
        )
    )
    assert filters["price"] == {"currency": "EUR", "minimum": "0", "maximum": "80.50"}
    assert filters["native_prices"] == {}
    assert "Nike &lt;&amp;&gt;, hoodie, L, бавовна" in menu.panel().text
    assert labels(menu) == [ADD, REMOVE, ENABLED_FILTERS, MARKETS, BACK]
    await menu.send(REMOVE)
    button = "🗑 Nike <&>, hoodie, L, бавовна"
    assert button in labels(menu) and items[0].id not in str(labels(menu))
    await menu.send(button)
    assert "Ключові слова: Nike &lt;&amp;&gt;, hoodie, L, бавовна" in menu.panel().text
    assert "Ціна: 0–80.5 EUR" in menu.panel().text
    assert "Виключення: Fake, Hole, Junior, Damaged, kids" in menu.panel().text
    assert "автоматично за курсом ЄЦБ" in menu.panel().text
    await menu.send("← Назад")
    assert await catalog.count(101) == 1
    await menu.send(button)
    await menu.send("✅ Так, забрати")
    assert await catalog.count(101) == 0


async def test_cancel_restart_pagination_and_other_users_filters(menu, services):
    catalog = services[0]
    await menu.send("/start")
    await menu.send("/start", user_id=202, username="bob")
    await menu.send(ADD)
    await menu.send("Скасувати")
    assert await catalog.count(101) == 0
    for i in range(7):
        await catalog.save(
            101, Filters(models=[f"Model {i}"], sources={"vinted": ["FR"]}), f"save:{i}"
        )
    await menu.send(REMOVE)
    first_label = labels(menu)[0]
    assert "Наступні →" in labels(menu)
    await menu.send("Наступні →")
    assert "← Попередні" in labels(menu)
    assert "Наступні →" not in labels(menu)
    await menu.send(REMOVE, user_id=202, username="bob")
    assert "поки немає" in menu.panel(202).text
    await menu.send(first_label, user_id=202, username="bob")
    assert await catalog.count(101) == 7
    await menu.send(ADD)
    await menu.send("Nike")
    await menu.send("/start")
    assert (await (await menu.state()).get_data())["draft"] is None
    assert FILTER_SETTINGS in labels(menu)


async def test_enabled_filters_toggle_start_empty_and_back(menu, services):
    catalog, control, _ = services
    await menu.send("/start")
    await menu.send(FILTER_SETTINGS)
    assert ENABLED_FILTERS in labels(menu)
    await menu.send(ENABLED_FILTERS)
    assert "Фільтрів поки немає" in menu.panel().text
    assert labels(menu) == [BACK]
    await menu.send(BACK)
    assert ADD in labels(menu)
    one = await catalog.save(101, Filters(keywords=["Nike <&>"], sources={"vinted": ["FR"]}), "one")
    two = await catalog.save(101, Filters(keywords=["Adidas"], sources={"vinted": ["FR"]}), "two")
    await menu.send(ENABLED_FILTERS)
    assert labels(menu) == ["✅ Nike <&>", "✅ Adidas", SEARCH_START, BACK]
    await menu.send("✅ Nike <&>")
    await menu.send("✅ Nike <&>", update_id=menu.update_id)
    assert "❌ Nike <&>" in labels(menu)
    assert "Увімкнено: 1 з 2" in menu.panel().text
    await menu.send(SEARCH_START)
    assert "Фільтрів: 1" in menu.panel().text
    assert (await control.status(101))[one.id] == "stopped"
    assert (await control.status(101))[two.id] == "requested"
    await menu.send(ENABLED_FILTERS)
    await menu.send("✅ Adidas")
    assert "Увімкни хоча б один фільтр" in menu.panel().text
    assert SEARCH_START not in labels(menu)
    await menu.send(SEARCH_START)
    assert "Увімкни хоча б один фільтр" in menu.transport.calls[-1].text
    await menu.send("/start")
    await menu.send(ENABLED_FILTERS)
    assert labels(menu) == ["❌ Nike <&>", "❌ Adidas", BACK]
    await menu.send("✅ Nike <&>")  # A queued tap can still have the old label.
    assert "✅ Nike <&>" in labels(menu)
    assert (await control.status(101))[one.id] == "stopped"
    await menu.send(SEARCH_START)
    assert (await control.status(101))[one.id] == "requested"
    assert (await control.status(101))[two.id] == "stopped"


async def test_enabled_filters_duplicates_pagination_and_user_isolation(menu, services):
    catalog = services[0]
    await menu.send("/start")
    await menu.send("/start", user_id=202, username="bob")
    for i in range(7):
        await catalog.save(101, Filters(keywords=["Nike"], sources={"vinted": ["FR"]}), f"same:{i}")
    items, _, _ = await catalog.list(101)
    await menu.send(ENABLED_FILTERS)
    assert labels(menu)[:6] == [f"✅ Nike (№{i})" for i in range(1, 7)]
    await menu.send("✅ Nike (№2)")
    assert "❌ Nike (№2)" in labels(menu)
    assert "✅ Nike (№1)" in labels(menu)
    assert not (await catalog.get(101, items[1].id)).enabled
    await menu.send("✅ Nike (№2)")
    assert "✅ Nike (№2)" in labels(menu)
    await menu.send("Наступні →")
    assert "← Попередні" in labels(menu)
    assert "Наступні →" not in labels(menu)
    await menu.send("✅ Nike")
    assert "❌ Nike" in labels(menu) and "← Попередні" in labels(menu)
    await menu.send("← Попередні")
    assert labels(menu)[:6] == [f"✅ Nike (№{i})" for i in range(1, 7)]
    await menu.send(ENABLED_FILTERS, user_id=202, username="bob")
    assert labels(menu, 202) == [BACK]
    await menu.send("✅ Nike (№1)", user_id=202, username="bob")
    assert (await catalog.get(101, items[0].id)).enabled
    await menu.send(BACK)
    assert ENABLED_FILTERS in labels(menu)


@pytest.mark.parametrize("stage", ["toggle", "filter_toggle", "open", "price", "save"])
async def test_reply_transport_retry_never_repeats_mutation(menu, services, monkeypatch, stage):
    catalog = services[0]
    await menu.send("/start")
    if stage == "toggle":
        await menu.send(MARKETS)
        text = "✅ Vinted FR"
    elif stage == "filter_toggle":
        item = await catalog.save(
            101, Filters(keywords=["Nike"], sources={"vinted": ["FR"]}), "save"
        )
        await menu.send(ENABLED_FILTERS)
        text = "✅ Nike"
    elif stage == "open":
        text = ADD
    else:
        await menu.send(ADD)
        await menu.send("Nike, hoodie, L")
        await menu.send("Пропустити")
        text = "10 80" if stage == "save" else "80"
    original = menu.transport.make_request
    fail = True

    async def flaky(bot, method, timeout=None):
        nonlocal fail
        if fail and isinstance(method, SendMessage):
            fail = False
            raise RuntimeError("simulated transport failure")
        return await original(bot, method, timeout)

    monkeypatch.setattr(menu.transport, "make_request", flaky)
    with pytest.raises(RuntimeError, match="simulated"):
        await menu.send(text)
    await menu.send(text, update_id=menu.update_id)
    if stage == "toggle":
        assert await catalog.marketplaces(101) == {"vinted_gb", "vinted_it", "vinted_de"}
    elif stage == "filter_toggle":
        assert not (await catalog.get(101, item.id)).enabled
        assert "❌ Nike" in labels(menu)
    elif stage == "open":
        assert "Введи ключові слова через кому" in menu.panel().text
        assert await catalog.count(101) == 0
    elif stage == "price":
        assert "збережено" in menu.panel().text
        assert await catalog.count(101) == 1
    else:
        assert await catalog.count(101) == 1
        assert "збережено" in menu.panel().text


async def test_subscription_reply_preserves_search_and_old_callbacks_stay_inert(menu, services):
    from vpars_bot.reply_menu import BUY_SUBSCRIPTION, SUBSCRIPTION_TEXT

    catalog, control, _ = services
    await menu.send("/start")
    item = await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "existing")
    await control.start(101, "existing-start")
    await menu.send(BUY_SUBSCRIPTION)
    assert menu.transport.calls[-1].text == SUBSCRIPTION_TEXT
    from vpars_bot.subscription_payments import offer_rows

    assert labels(menu) == [label for row in offer_rows() for label, _ in row] + [BACK]
    for level in ("LITE", "PRO", "ULTRA"):
        await menu.send(level + " · 1 місяць")
        assert level in menu.panel().text and "Після оплати" in menu.panel().text
        await menu.send(BACK)
    await menu.send("🎟️ Видати підписку")
    assert "лише адміністратору" in menu.transport.calls[-1].text
    await menu.send(BACK)
    assert FILTER_SETTINGS in labels(menu)
    await menu.click(data="old:stop")
    await menu.click(data="old:start")
    await menu.click(data="lot:buy")
    assert (await control.status(101))[item.id] == "requested"
    assert (await catalog.user(101)).delivery_epoch == 0


@pytest.mark.parametrize("is_admin", [False, True])
async def test_profile_status_and_faq_autobuy_preserve_search(menu, services, is_admin):
    from aiogram.methods import AnswerCallbackQuery

    from vpars_bot.profile_menu import (
        CONNECT_AUTOBUY,
        FAQ,
        FAQ_BACK,
        FAQ_TEXT,
        INSTRUCTION_CALLBACK,
        PAGES,
        PROFILE,
    )
    from vpars_bot.subscriptions import profile_text

    catalog, control, _ = services
    await menu.send("/start")
    async with catalog.sessions.begin() as session:
        (await session.get(User, 101)).is_admin = is_admin
    item = await catalog.save(
        101, Filters(keywords=["Nike"], sources={"vinted_fr": ["FR"]}), "profile-existing"
    )
    await control.start(101, "profile-existing-start")
    before = await search_commands(catalog)
    await menu.send(HOME)
    main_rows = [[b.text for b in row] for row in menu.panel().reply_markup.keyboard]
    assert [len(row) for row in main_rows] == [2, 2, 1]
    assert main_rows[-1] == [FAQ]
    await menu.send(PROFILE)
    from vpars_bot.subscriptions import SubscriptionService

    assert menu.transport.calls[-1].text == profile_text(
        await SubscriptionService(catalog.sessions).get(101), is_admin=is_admin
    )
    assert menu.transport.calls[-1].reply_markup is None
    assert (await (await menu.state()).get_data())["reply_view"] == "home"
    assert [[b.text for b in row] for row in menu.panel().reply_markup.keyboard] == main_rows
    await menu.send(FAQ)
    assert menu.panel().text == FAQ_TEXT
    expected = [page[0] for page in PAGES.values()] + [FAQ_BACK]
    assert labels(menu) == expected
    assert len(menu.panel().reply_markup.keyboard[-1]) == 1
    for label, message in PAGES.values():
        await menu.send(label)
        assert menu.transport.calls[-1].text == message
        assert labels(menu) == expected
    await menu.send(CONNECT_AUTOBUY)
    assert menu.transport.calls[-1].text == "Soon"
    await menu.click(data=INSTRUCTION_CALLBACK)
    assert isinstance(menu.transport.calls[-2], AnswerCallbackQuery)
    assert menu.transport.calls[-1].text == "Soon"
    assert (await (await menu.state()).get_data())["reply_view"] == "faq"
    assert labels(menu) == expected
    await menu.send(FAQ_BACK)
    assert [[b.text for b in row] for row in menu.panel().reply_markup.keyboard] == main_rows
    assert PROFILE in labels(menu) and SEARCH_STOP in labels(menu)
    assert SEARCH_START not in labels(menu)
    assert ("🎟️ Видати підписку" if is_admin else "💳 Купити підписку") in labels(menu)
    assert (await control.status(101))[item.id] == "requested"
    assert len(await search_commands(catalog)) == len(before)
    assert await catalog.count(101) == 1
    assert (await catalog.user(101)).delivery_epoch == 0


async def test_admin_grants_after_three_prompts_and_profile_is_real(menu, services, profiles):
    from vpars_bot.db import Operation, Subscription
    from vpars_bot.profile_menu import PROFILE
    from vpars_bot.subscriptions import GRANT_SUBSCRIPTION

    catalog, control, _ = services
    profiles.names.update({101: "bladebotsupport", 202: "bob"})
    await menu.send("/start", user_id=202, username="bob")
    await menu.send("/start", username="BladeBotSupport")
    assert GRANT_SUBSCRIPTION in labels(menu)
    assert "💳 Купити підписку" not in labels(menu)
    item = await catalog.save(202, Filters(sources={"vinted_fr": ["FR"]}), "existing-recipient")
    await control.start(202, "existing-recipient-search")
    before_commands = await search_commands(catalog, 202)
    await menu.send(GRANT_SUBSCRIPTION, username="BladeBotSupport")
    assert menu.panel().text == "Введи @username або ID користувача."
    await menu.send("@missing", username="BladeBotSupport")
    assert "Користувача не знайдено" in menu.transport.calls[-1].text
    await menu.send("@bob", username="BladeBotSupport")
    assert menu.panel().text == "Обери рівень підписки."
    await menu.send("PRO", username="BladeBotSupport")
    assert "-5 — відняти" in menu.panel().text
    for value in ("0", "-0", "1.5", "3651", "-3651", "--5", "+", "- 5"):
        await menu.send(value, username="BladeBotSupport")
        assert "від -3650 до 3650" in menu.transport.calls[-1].text
    await menu.send("+30", username="BladeBotSupport")
    await menu.send("+30", username="BladeBotSupport", update_id=menu.update_id)
    assert "Підписку видано: @bob" in menu.panel().text
    assert "PRO · +30 днів" in menu.panel().text
    async with catalog.sessions() as session:
        assert (await session.get(Subscription, 202)).level == "PRO"
        assert (
            len(
                list(
                    await session.scalars(
                        select(Operation).where(Operation.kind == "grant_subscription")
                    )
                )
            )
            == 1
        )
    await menu.send(PROFILE, user_id=202, username="bob")
    assert "PRO" in menu.transport.calls[-1].text
    assert "неактивний" not in menu.transport.calls[-1].text
    assert menu.transport.calls[-1].reply_markup is None
    assert (await control.status(202))[item.id] == "requested"
    assert len(await search_commands(catalog, 202)) == len(before_commands)


async def test_custom_days_subtract_in_menu_once_and_update_profile(menu, services, profiles):
    from datetime import timedelta

    from vpars_bot.db import Subscription
    from vpars_bot.subscriptions import GRANT_SUBSCRIPTION

    catalog, _, _ = services
    profiles.names.update({101: "bladebotsupport", 202: "bob"})
    await menu.send("/start", user_id=202, username="bob")
    await menu.send("/start", username="BladeBotSupport")
    for value in (GRANT_SUBSCRIPTION, "@bob", "PRO", "17"):
        await menu.send(value, username="BladeBotSupport")
    async with catalog.sessions() as session:
        initial = (await session.get(Subscription, 202)).expires_at
    for value in (GRANT_SUBSCRIPTION, "@bob", "PRO", "-5"):
        await menu.send(value, username="BladeBotSupport")
    await menu.send("-5", username="BladeBotSupport", update_id=menu.update_id)
    assert "Термін підписки скорочено" in menu.panel().text
    assert "PRO · -5 днів" in menu.panel().text
    async with catalog.sessions() as session:
        assert (await session.get(Subscription, 202)).expires_at == initial - timedelta(days=5)


@pytest.mark.parametrize("failure", ["response", "state"])
async def test_grant_recovers_without_extending_twice(
    menu, services, profiles, monkeypatch, failure
):
    from vpars_bot.db import Subscription
    from vpars_bot.subscriptions import GRANT_SUBSCRIPTION

    catalog, _, _ = services
    profiles.names.update({101: "bladebotsupport", 202: "bob"})
    await menu.send("/start", user_id=202, username="bob")
    await menu.send("/start", username="BladeBotSupport")
    for value in (GRANT_SUBSCRIPTION, "@bob", "ULTRA"):
        await menu.send(value, username="BladeBotSupport")
    owner, method = (
        (menu.transport, "make_request") if failure == "response" else (ReplyMenuUI, "home")
    )
    original = getattr(owner, method)
    fail = True

    async def flaky(*args, **kwargs):
        nonlocal fail
        if fail:
            fail = False
            raise RuntimeError("after grant commit")
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, method, flaky)
    with pytest.raises(RuntimeError, match="after grant"):
        await menu.send("7", username="BladeBotSupport")
    async with catalog.sessions() as session:
        expires = (await session.get(Subscription, 202)).expires_at
    await menu.send("7", username="BladeBotSupport", update_id=menu.update_id)
    async with catalog.sessions() as session:
        assert (await session.get(Subscription, 202)).expires_at == expires
    assert "Підписку видано" in menu.panel().text


async def test_grant_back_cancel_and_revoked_admin(menu, services, profiles):
    from vpars_bot.db import Subscription
    from vpars_bot.subscriptions import GRANT_SUBSCRIPTION

    catalog, _, _ = services
    profiles.names.update({101: "bladebotsupport", 202: "bob"})
    await menu.send("/start", user_id=202, username="bob")
    async with catalog.sessions.begin() as session:
        await session.delete(await session.get(Subscription, 202))
    await menu.send("/start", username="BladeBotSupport")
    for value in (GRANT_SUBSCRIPTION, "@bob", "LITE", BACK):
        await menu.send(value, username="BladeBotSupport")
    assert menu.panel().text == "Обери рівень підписки."
    await menu.send("Скасувати", username="BladeBotSupport")
    assert (await (await menu.state()).get_data())["subscription_draft"] is None
    for value in (GRANT_SUBSCRIPTION, "@bob", "PRO"):
        await menu.send(value, username="BladeBotSupport")
    async with catalog.sessions.begin() as session:
        (await session.get(User, 101)).is_admin = False
    await menu.send("30", username="BladeBotSupport")
    assert "лише адміністратору" in menu.transport.calls[-1].text
    async with catalog.sessions() as session:
        assert await session.get(Subscription, 202) is None


async def test_recovering_grant_prompt_rechecks_role(menu, services, profiles, monkeypatch):
    from vpars_bot.subscriptions import GRANT_SUBSCRIPTION

    catalog, _, _ = services
    profiles.names[101] = "bladebotsupport"
    await menu.send("/start", username="BladeBotSupport")
    original = menu.transport.make_request

    async def offline(*args, **kwargs):
        raise RuntimeError("prompt not sent")

    monkeypatch.setattr(menu.transport, "make_request", offline)
    with pytest.raises(RuntimeError):
        await menu.send(GRANT_SUBSCRIPTION, username="BladeBotSupport")
    async with catalog.sessions.begin() as session:
        (await session.get(User, 101)).is_admin = False
    monkeypatch.setattr(menu.transport, "make_request", original)
    await menu.send(GRANT_SUBSCRIPTION, username="BladeBotSupport", update_id=menu.update_id)
    assert "лише адміністратору" in menu.transport.calls[-1].text
    assert (await (await menu.state()).get_data())["reply_done_update"] == menu.update_id


async def search_commands(catalog, user_id=101):
    async with catalog.sessions() as session:
        return list(
            await session.scalars(select(ControlCommand).where(ControlCommand.user_id == user_id))
        )


@pytest.mark.parametrize("is_admin", [False, True])
@pytest.mark.parametrize(
    ("start_label", "stop_label"),
    [(SEARCH_START, SEARCH_STOP), ("▶️ Почати пошук", "⏹ Завершити пошук")],
)
async def test_search_buttons_use_saved_filters_and_enabled_markets(
    menu, services, is_admin, start_label, stop_label
):
    catalog, control, _ = services
    await menu.send("/start")
    assert [b.text for b in menu.panel().reply_markup.keyboard[0]] == [
        SEARCH_START,
        "💳 Купити підписку",
    ]
    assert SEARCH_STOP not in labels(menu)
    async with catalog.sessions.begin() as session:
        (await session.get(User, 101)).is_admin = is_admin
    subscription = "🎟️ Видати підписку" if is_admin else "💳 Купити підписку"
    items = [
        await catalog.save(
            101,
            Filters(
                brands=["Nike"],
                models=[model],
                sources={"vinted_gb": ["GB"]},
                follow_enabled_sources=True,
            ),
            "save-" + model,
        )
        for model in ("Air Max", "Tech Fleece", "Archived")
    ]
    await catalog.archive(101, items[-1].id, True, items[-1].version, "archive")
    for source in ("vinted_gb", "vinted_it", "vinted_de"):
        await catalog.toggle_marketplace(101, source, "disable-" + source)
    await menu.send("/start", user_id=202, username="bob")
    other = await catalog.save(202, Filters(sources={"vinted_fr": ["FR"]}), "other")
    await control.start(202, "other-start")

    await menu.send(start_label)
    await menu.send(start_label, update_id=menu.update_id)
    await menu.send(start_label)
    assert [b.text for b in menu.panel().reply_markup.keyboard[0]] == [SEARCH_STOP, subscription]
    assert SEARCH_START not in labels(menu)
    starts = [c for c in await search_commands(catalog) if c.kind == "start"]
    assert len(starts) == 2  # No duplicates, archived filters or disabled markets.
    assert {c.payload["token"]["search_id"] for c in starts} == {i.id for i in items[:2]}
    assert all(c.payload["filters"]["sources"] == {"vinted_fr": ["FR"]} for c in starts)
    assert menu.panel().text == "✅ Пошук початий\n\nФільтрів: 2\nМаркетплейси: Vinted FR"
    assert "Пошук: працює" not in menu.panel().text
    assert "Фільтрів: 2" in menu.panel().text

    tokens = [WorkToken(**c.payload["token"]) for c in starts]
    for token in tokens:
        assert await control.acknowledge(token)
    await menu.send(HOME)
    assert "Пошук: працює" in menu.panel().text
    assert [b.text for b in menu.panel().reply_markup.keyboard[0]] == [SEARCH_STOP, subscription]
    await menu.send("/start")
    assert SEARCH_STOP in labels(menu) and SEARCH_START not in labels(menu)

    await menu.send(stop_label)
    await menu.send(stop_label, update_id=menu.update_id)
    assert [b.text for b in menu.panel().reply_markup.keyboard[0]] == [SEARCH_START, subscription]
    assert SEARCH_STOP not in labels(menu)
    assert set((await control.status(101)).values()) == {"stopped"}
    assert (await control.status(202))[other.id] == "requested"
    assert (await catalog.user(101)).delivery_epoch == 1
    assert menu.panel().text == "❌ Пошук завершений\n\nФільтрів: 2\nМаркетплейси: Vinted FR"
    assert await catalog.count(101) == 2
    assert await catalog.marketplaces(101) == {"vinted_fr"}
    for item in items:
        assert (await catalog.get(101, item.id)).filters == item.filters
    for token in tokens:
        assert not await control.can_deliver(token)
        assert not await control.acknowledge(token)

    await menu.send(SEARCH_START)
    assert SEARCH_STOP in labels(menu) and SEARCH_START not in labels(menu)
    assert set((await control.status(101)).values()) == {"requested"}
    starts = [c for c in await search_commands(catalog) if c.kind == "start"]
    assert len(starts) == 4
    await menu.send(MARKETS)
    await menu.send("/stop")
    assert SEARCH_START in labels(menu) and SEARCH_STOP not in labels(menu)


async def test_search_buttons_explain_missing_filters_or_markets(menu, services):
    catalog, control, _ = services
    await menu.send("/start")
    await menu.send(SEARCH_START)
    assert "Додати фільтр" in menu.transport.calls[-1].text
    assert SEARCH_START in labels(menu) and SEARCH_STOP not in labels(menu)
    await catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), "saved")
    for source in catalog.registry.sources:
        await catalog.toggle_marketplace(101, source, "disable-" + source)
    await menu.send(SEARCH_START)
    assert "Увімкни хоча б один" in menu.transport.calls[-1].text
    assert not any(c.kind == "start" for c in await search_commands(catalog))
    # Stop still works with every marketplace disabled and from inside a dialog.
    await menu.send(MARKETS)
    await menu.send("/stop")
    assert set((await control.status(101)).values()) == {"stopped"}
    assert "❌ Пошук завершений" in menu.panel().text
    assert SEARCH_START in labels(menu)


@pytest.mark.parametrize("button", [SEARCH_START, SEARCH_STOP])
@pytest.mark.parametrize("failure", ["response", "state"])
async def test_search_retries_after_commit_never_duplicate_actions(
    menu, services, monkeypatch, button, failure
):
    catalog, control, _ = services
    await menu.send("/start")
    item = await catalog.save(101, Filters(sources={"vinted_fr": ["FR"]}), "saved")
    if button == SEARCH_STOP:
        await control.start(101, "existing")
    if failure == "response":
        owner, method = menu.transport, "make_request"
    else:
        owner, method = ReplyMenuUI, "view"
    original = getattr(owner, method)
    fail = True

    async def flaky(*args, **kwargs):
        nonlocal fail
        if fail:
            fail = False
            raise RuntimeError("after commit")
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, method, flaky)
    with pytest.raises(RuntimeError, match="after commit"):
        await menu.send(button)
    await menu.send(button, update_id=menu.update_id)
    commands = await search_commands(catalog)
    assert sum(c.kind == "start" for c in commands) == 1
    assert sum(c.kind == "stop" for c in commands) == (button == SEARCH_STOP)
    assert (await catalog.user(101)).delivery_epoch == (button == SEARCH_STOP)
    assert (await control.status(101))[item.id] == (
        "requested" if button == SEARCH_START else "stopped"
    )


async def test_euro_price_is_asked_once_even_when_gb_is_disabled(menu, services):
    await menu.send("/start")
    await menu.send(MARKETS)
    await menu.send("✅ Vinted UK")
    await menu.send("✅ Vinted IT")
    await menu.send(HOME)
    await menu.send(ADD)
    await menu.send("Adidas, Samba, EU 42")
    await menu.send("Пропустити")
    assert menu.panel().text == PRICE_PROMPT
    await menu.send("Пропустити")
    items, _, _ = await services[0].list(101)
    assert items[0].filters["sources"] == {"vinted_fr": ["FR"], "vinted_de": ["DE"]}
    assert items[0].filters["keywords"] == ["Adidas", "Samba", "EU 42"]
    assert items[0].filters["price"] is None and items[0].filters["native_prices"] == {}


async def test_old_grouped_marketplace_keyboard_refreshes_without_toggling_all(menu, services):
    await menu.send("/start")
    state = await menu.state()
    await state.update_data(reply_view="markets", reply_actions={"✅ Vinted": "market:vinted"})
    await menu.send("✅ Vinted")
    assert "✅ Vinted" not in labels(menu)
    assert "✅ Vinted UK" in labels(menu) and "✅ Vinted FR" in labels(menu)
    assert await services[0].marketplaces(101) == set(services[0].registry.sources)


async def test_remove_labels_show_brand_model_and_keep_duplicate_names_distinct(menu, services):
    catalog = services[0]
    await menu.send("/start")
    items = []
    for size in ("S", "L"):
        items.append(
            await catalog.save(
                101,
                Filters(
                    brands=["Nike"],
                    models=["Tech Fleece"],
                    sizes=[size],
                    sources={"vinted_fr": ["FR"]},
                ),
                "size-" + size,
            )
        )
    other = await catalog.save(
        101,
        Filters(brands=["Adidas"], models=["Samba"], sources={"vinted_fr": ["FR"]}),
        "other-model",
    )
    await menu.send(REMOVE)
    buttons = [label for label in labels(menu) if label.startswith("🗑")]
    assert "🗑 Adidas, Samba" in buttons
    nike = [label for label in buttons if "Nike, Tech Fleece" in label]
    assert len(nike) == len(set(nike)) == 2
    assert all(item.id[:8] not in " ".join(buttons) for item in [*items, other])
    await menu.send(nike[0])
    assert "Розмір: S" in menu.panel().text
    await menu.send("✅ Так, забрати")
    assert (await catalog.get(101, items[0].id)).archived
    assert not (await catalog.get(101, items[1].id)).archived


async def test_old_draft_is_replaced_without_saving_old_button_as_keyword(menu, services):
    await menu.send("/start")
    state = await menu.state()
    old = {"kind": "native_filters", "step": "sizes", "values": {"brands": ["Nike"]}}
    await state.update_data(reply_view="filters", draft=old, reply_actions={"Далі →": "f:next"})
    await menu.send("Далі →")
    assert "Діалог оновлено" in menu.panel().text
    assert await services[0].count(101) == 0
    assert (await state.get_data())["draft"]["previous_draft"] == old
    await menu.send("Nike, hoodie, л")
    for _ in range(5):
        await menu.send("Пропустити")
    items, _, _ = await services[0].list(101)
    assert items[0].filters["keywords"] == ["Nike", "hoodie", "L"]


async def test_invalid_price_keeps_question_and_each_bound_can_be_skipped(menu, services):
    await menu.send("/start")
    await menu.send(ADD)
    await menu.send("Пропустити")
    assert await services[0].count(101) == 0
    await menu.send("Nike, L")
    await menu.send("Пропустити")
    for invalid in ("-1", "NaN", "Infinity", "80 20", "1.234"):
        await menu.send(invalid)
        assert "Перевір ціну" in menu.transport.calls[-1].text
        assert (await (await menu.state()).get_data())["draft"]["step"] == "price"
    await menu.send("20 -")
    items, count, _ = await services[0].list(101)
    assert count == 1
    assert items[0].filters["price"] == {"currency": "EUR", "minimum": "20", "maximum": None}


async def test_old_gbp_prompt_cannot_reinterpret_pounds_as_euros(menu, services):
    await menu.send("/start")
    await menu.send(ADD)
    await menu.send("Adidas, L")
    state = await menu.state()
    data = await state.get_data()
    legacy = {
        **data["draft"],
        "step": "maximum:GBP",
        "currencies": ["EUR", "GBP"],
        "bounds": {"EUR": {"maximum": "100"}},
    }
    await state.update_data(draft=legacy)
    await menu.send("60")
    assert await services[0].count(101) == 0
    assert "Яка ціна в євро" in menu.panel().text
    assert (await state.get_data())["draft"]["previous_draft"] == legacy
    await menu.send("100")
    items, count, _ = await services[0].list(101)
    assert count == 1 and items[0].filters["price"]["maximum"] == "100"


async def test_cancel_at_last_price_never_saves_partial_filter(menu, services):
    await menu.send("/start")
    await menu.send(ADD)
    for answer in ("Nike, L", "Пропустити"):
        await menu.send(answer)
    assert menu.panel().text == PRICE_PROMPT
    await menu.send("Скасувати")
    assert await services[0].count(101) == 0
    assert (await (await menu.state()).get_data())["draft"] is None


@pytest.mark.parametrize("level", [None, "LITE", "PRO", "ULTRA"])
async def test_exclusions_follow_subscription_level(menu, services, level):
    from datetime import timedelta

    from vpars_bot.db import Subscription, utcnow
    from vpars_bot.keyword_dialog import EXCLUSIONS_PROMPT

    catalog = services[0]
    await menu.send("/start")
    async with catalog.sessions.begin() as session:
        subscription = await session.get(Subscription, 101)
        if level:
            subscription.level = level
            subscription.expires_at = utcnow() + timedelta(days=1)
        else:
            await session.delete(subscription)
    await menu.send(ADD)
    if level is None:
        assert "активна підписка" in menu.transport.calls[-1].text
        return
    await menu.send("Nike, L")
    if level == "LITE":
        assert "ціна" in menu.panel().text and "excluded_keywords" not in menu.panel().text
        return
    assert menu.panel().text == EXCLUSIONS_PROMPT
    assert labels(menu) == ["Пропустити", "Скасувати"]
    await menu.send("Fake, kids")
    for _ in range(4):
        await menu.send("Пропустити")
    items, count, _ = await catalog.list(101)
    assert count == 1 and items[0].filters["excluded_keywords"] == ["Fake", "kids"]
    await menu.send(SEARCH_START)
    assert all(
        c.payload["filters"]["excluded_keywords"] == ["Fake", "kids"]
        for c in await search_commands(catalog)
        if c.kind == "start"
    )
