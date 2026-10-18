"""Client keyboards outlive the Redis dialogue; every stale tap needs a response."""

import pytest
from test_reply_menu import labels, search_commands
from test_reply_menu import menu as menu

from vpars_bot.autobuy_availability import text_action
from vpars_bot.domain import Filters
from vpars_bot.profile_menu import CONNECT_AUTOBUY, FAQ, FAQ_BACK, PAGES
from vpars_bot.reply_menu import BACK, HOME, MARKETS
from vpars_bot.wallet_onboarding import CHECK, CONTINUE, COUNTRIES, SAVE


@pytest.mark.parametrize("uid", [101, 202])
@pytest.mark.parametrize("button", [
    CONNECT_AUTOBUY, "Підключити Autobuy", COUNTRIES["DE"], CONTINUE, CHECK,
    SAVE, FAQ_BACK, BACK, "✅ Vinted FR", PAGES["about"][0], "/menu",
])
async def test_keyboard_after_dialog_expiry_answers_without_mutating_search(menu, services, uid, button):
    catalog, control, _ = services
    await menu.send("/start", user_id=uid, username=f"user{uid}")
    for region, brand in (("DE", "Stone Island"), ("FR", "Amiri")):
        await catalog.save(uid, Filters(keywords=[brand],
                                       sources={f"vinted_{region.lower()}": [region]}),
                           f"{uid}-{region}")
    await control.start(uid, f"{uid}-start")
    before = [(c.id, c.kind, c.payload) for c in await search_commands(catalog, uid)]
    user_before = await catalog.user(uid)
    await menu.send(FAQ, user_id=uid, username=f"user{uid}")
    await (await menu.state(uid)).clear()  # Redis data TTL elapsed; Telegram kept keyboard.
    count = len(menu.transport.calls)
    await menu.send(button, user_id=uid, username=f"user{uid}")
    assert len(menu.transport.calls) > count, "A stale keyboard must never fail silently"
    assert menu.panel(uid).reply_markup is not None
    assert [(c.id, c.kind, c.payload) for c in await search_commands(catalog, uid)] == before
    user_after = await catalog.user(uid)
    assert user_before.delivery_epoch == user_after.delivery_epoch
    assert user_before.enabled_sources == user_after.enabled_sources
    assert await catalog.count(uid) == 2
    if text_action(button):
        assert menu.transport.calls[-1].text == "Soon"
    else:
        assert HOME in labels(menu, uid) or "⚙️ Налаштування фільтрів" in labels(menu, uid)


async def test_stale_button_after_other_menu_refreshes_current_screen(menu):
    await menu.send("/start")
    await menu.send(FAQ)
    count = len(menu.transport.calls)
    await menu.send("✅ Vinted FR")  # Old market keyboard; never toggle the source.
    assert len(menu.transport.calls) > count
    assert CONNECT_AUTOBUY in labels(menu)
    await menu.send(HOME)
    await menu.send(MARKETS)
    before = len(menu.transport.calls)
    await menu.send(CHECK)  # Old wallet keyboard cannot change wallet state here.
    assert len(menu.transport.calls) > before
    assert "✅ Vinted FR" in labels(menu)


async def test_unknown_command_responds_and_duplicate_stale_tap_does_not_resend(menu):
    await menu.send("/start")
    count = len(menu.transport.calls)
    await menu.send("/menu")
    assert len(menu.transport.calls) > count
    await (await menu.state()).clear()
    await menu.send(CHECK)
    count = len(menu.transport.calls)
    await menu.send(CHECK, update_id=menu.update_id)
    assert len(menu.transport.calls) == count


@pytest.mark.parametrize("action", ["old:stop", "old:start", "old:filters"])
async def test_obsolete_inline_menu_refreshes_without_executing_old_action(menu, services, action):
    await menu.send("/start")
    await (await menu.state()).clear()
    before = await search_commands(services[0])
    count = len(menu.transport.calls)
    await menu.click(data=action)
    replies = menu.transport.calls[count:]
    assert any(getattr(reply, "reply_markup", None) for reply in replies)
    assert await search_commands(services[0]) == before
    assert "⚙️ Налаштування фільтрів" in labels(menu)
