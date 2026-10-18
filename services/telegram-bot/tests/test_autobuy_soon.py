"""Release policy: old and new entry points cannot create any purchase/session work."""

from dataclasses import replace
from decimal import Decimal

import pytest
from aiogram import Bot
from aiogram.types import Update
from aiohttp.test_utils import TestClient, TestServer
from conftest import OfflineSession
from sqlalchemy import func, select
from test_reply_menu import menu as menu

from vpars_bot import autobuy_availability as autobuy
from vpars_bot.config import Settings
from vpars_bot.db import Notification, WalletAccount, WalletOrder, WalletSession
from vpars_bot.domain import Filters, UserError
from vpars_bot.formatting import Lot, format_lot
from vpars_bot.notifications import NotificationSender
from vpars_bot.profile_menu import FAQ, PAGES, faq_rows
from vpars_bot.session_secrets import SESSION_INPUT, protected_payload
from vpars_bot.subscription_payments import SUBSCRIPTION_TEXT
from vpars_bot.wallet import WalletService
from vpars_bot.wallet_balance import balance_purchase
from vpars_bot.wallet_browser import BrowserCheckout, browser_adapter
from vpars_bot.wallet_handoff import HandoffService
from vpars_bot.wallet_handoff_web import handoff_app
from vpars_bot.wallet_links import delivery_checkout_url, direct_buy_keyboard
from vpars_bot.wallet_onboarding import SetupService
from vpars_bot.wallet_pool import WarmBrowserPool
from vpars_bot.wallet_sessions import SessionService, SessionWorker
from vpars_bot.wallet_upgrade_buttons import upgraded_keyboard
from vpars_bot.wallet_worker import WalletWorker


@pytest.mark.parametrize("uid,brand,region", [(101, "Nike", "FR"), (202, "Amiri", "DE")])
async def test_old_buttons_and_setup_leave_users_filters_and_orders_untouched(menu, services, uid, brand, region):
    catalog, control, _ = services
    await menu.send("/start", user_id=uid)
    item = await catalog.save(uid, Filters(keywords=[brand], sources={f"vinted_{region.lower()}": [region]}), "soon-filter")
    await control.start(uid, "soon-start")
    user_before = (await catalog.user(uid)).delivery_epoch
    filters_before = (await catalog.get(uid, item.id)).filters
    status_before = await control.status(uid)
    for data in ["lot:buy", "wallet:approve:" + "a" * 32, "wallet:pay:" + "b" * 32,
                 "wallet:cancel:" + "c" * 32, "ab:enable", "ab:region:DE", "faq:connect",
                 "faq:autobuy", "profile:autobuy_instruction"]:
        await menu.click(data=data, user_id=uid)
        assert menu.transport.calls[-1].text == "Soon"
        assert menu.transport.calls[-1].reply_markup is None
    for text in ["Soon", "Підключити автобай", "Підключити Autobuy", "/autobuy", "/buy", "/wallet",
                 *autobuy.OLD_SETUP_LABELS]:
        await menu.send(text, user_id=uid)
        assert menu.transport.calls[-1].text == "Soon"
    state = await menu.state(uid)
    await state.update_data(reply_view="autobuy_setup", autobuy_draft={"step": "limit", "region": region})
    await menu.send("100", user_id=uid)
    assert menu.transport.calls[-1].text == "Soon"
    assert (await catalog.user(uid)).delivery_epoch == user_before
    assert (await catalog.get(uid, item.id)).filters == filters_before
    assert await control.status(uid) == status_before
    async with catalog.sessions() as session:
        for model in (WalletOrder, WalletAccount, WalletSession):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_core_services_reject_before_database_or_browser_access():
    assert not autobuy.AVAILABLE
    wallet, sessions = WalletService(None), SessionService(None)
    handoff = HandoffService(None, "https://old.example")
    for operation in [wallet.request(101, 10), wallet.decide(101, "a" * 32, pay=True, approve=True),
                      sessions.submit(101, "ignored", "op"), sessions.probe(101),
                      sessions.enable(101), sessions.resume_human(101, "DE"),
                      sessions.disable(101, "DE"), SetupService(None).save(101, {}),
                      handoff.request(101, 10), handoff.authorize(101, "a" * 32),
                      balance_purchase(None, None, None, None, None)]:
        with pytest.raises(UserError, match="^Soon$"):
            await operation
    assert await sessions.actions(101) == {}
    assert await sessions.status(101) == await wallet.connection_text(101) == "Soon"
    with pytest.raises(UserError, match="^Soon$"):
        async with browser_adapter(None):
            pytest.fail("Must not launch a browser")
    with pytest.raises(UserError, match="^Soon$"):
        await BrowserCheckout.pay(None, None, None)
    with pytest.raises(UserError, match="^Soon$"):
        await BrowserCheckout.checkout(None, None)
    with pytest.raises(UserError, match="^Soon$"):
        await BrowserCheckout.snapshot(None, expected={})
    worker = WalletWorker(None, None)
    assert not await worker.once()
    assert not await worker.execute(None)
    await worker.recover()
    await worker.run()
    session_worker = SessionWorker(None)
    assert not await session_worker.once()
    await session_worker.recover()
    await session_worker.run()
    await WarmBrowserPool(None).maintain(prime=True)


@pytest.mark.parametrize("mode", ["bot", "automatic", "manual", "balance_first"])
def test_new_and_upgraded_cards_ignore_configured_purchase_windows(mode):
    settings = Settings(_env_file=None, wallet_checkout_mode=mode, wallet_handoff_url="https://old.example")
    assert settings.checkout_window_url == ""
    assert delivery_checkout_url("https://old.example", "a" * 64) is None
    lot = Lot(title="Nike", brand="Nike", price=Decimal("10"), currency="EUR", source_label="Vinted",
              region="DE", url="https://www.vinted.de/items/123-nike", search_name="Nike")
    for brand in ("Nike", "Amiri"):
        card = format_lot(replace(lot, title=brand, brand=brand, search_name=brand))
        for keyboard in (direct_buy_keyboard(card.keyboard, "https://old.example/buy/old"),
                         upgraded_keyboard(lot.url, "https://old.example", "a" * 64)):
            button = keyboard.inline_keyboard[0][1]
            assert button.text == "Soon" and button.callback_data == "lot:buy"
            assert button.web_app is None and button.url is None
            assert keyboard.inline_keyboard[0][0].url == lot.url


async def test_old_public_links_are_inert_without_authentication_or_browser():
    async with TestClient(TestServer(handoff_app(None, "unused"))) as client:
        for path in ("/buy/" + "a" * 64, "/purchase/" + "b" * 32, "/assets/app.js"):
            response = await client.get(path)
            assert await response.text() == "Soon"
            assert response.headers["Cache-Control"] == "no-store"
        response = await client.post("/purchase/" + "b" * 32 + "/auth", json={"init_data": "anything"})
        assert response.status == 403 and await response.json() == {"error": "Soon"}
        assert (await (await client.get("/health")).json())["mode"] == "soon"


async def test_pending_wallet_notifications_become_soon_but_other_notifications_do_not(menu, services):
    await menu.send("/start")
    sessions = services[0].sessions
    async with sessions.begin() as session:
        for key in ("wallet:old:bot_quoted", "wallet-session:old:1", "unrelated"):
            session.add(Notification(id=key, recipient_id=101, kind="text", payload={"text": "old"}))
    transport = OfflineSession()
    async with Bot("42:offline", session=transport) as bot:
        sender = NotificationSender(sessions, bot)
        while await sender.once():
            pass
    assert sorted(call.text for call in transport.calls) == ["Soon", "Soon", "old"]
    assert all(call.reply_markup is None for call in transport.calls)


async def test_faq_and_tariffs_do_not_promise_active_autobuy(menu):
    await menu.send("/start")
    await menu.send(FAQ)
    assert PAGES["autobuy"] == ("Soon", "Soon")
    assert [label for row in faq_rows() for label, _ in row].count("Soon") == 1
    assert SUBSCRIPTION_TEXT.count("Autobuy — Soon") == 3
    assert "викуп" not in SUBSCRIPTION_TEXT and "квота" not in SUBSCRIPTION_TEXT


async def test_retired_key_input_is_redacted_and_never_stored_or_imported(menu, monkeypatch):
    import vpars_bot.session_secrets as secrets

    def unavailable_cipher():
        pytest.fail("Disabled Autobuy must not read the encryption key")

    monkeypatch.setattr(secrets, "session_cipher", unavailable_cipher)
    token = "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20
    update = Update.model_validate({"update_id": 7, "message": {
        "message_id": 7, "date": 1, "chat": {"id": 101, "type": "private"},
        "from": {"id": 101, "is_bot": False, "first_name": "Test"}, "text": token,
    }})
    payload = protected_payload(update, 42)
    assert "_wallet_secret" not in payload
    assert payload["message"]["text"] == SESSION_INPUT
    assert token not in str(payload)
    await menu.send("/start")
    await (await menu.state()).update_data(reply_view="autobuy_setup",
                                         autobuy_draft={"step": "login", "region": "DE"})
    await menu.send(token)
    assert menu.transport.calls[-1].text == "Soon"


async def test_full_menu_old_actions_are_soon_without_changing_dialog(chat):
    await chat.send("/start")
    before = await (await chat.state()).get_data()
    await chat.click(data="wallet:approve:" + "a" * 32)
    assert chat.transport.calls[-1].text == "Soon"
    await chat.send("/autobuy")
    assert chat.transport.calls[-1].text == "Soon"
    after = await (await chat.state()).get_data()
    before.pop("render_update_id", None)
    after.pop("render_update_id", None)
    assert after == before
