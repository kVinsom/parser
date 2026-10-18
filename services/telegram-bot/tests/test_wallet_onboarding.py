import asyncio

import pytest
from sqlalchemy import func, select
from test_reply_menu import labels, search_commands
from test_reply_menu import menu as menu_fixture

from vpars_bot.application import Catalog
from vpars_bot.db import Operation, Subscription, User, WalletAccount, WalletOrder
from vpars_bot.domain import Filters, UserError
from vpars_bot.profile_menu import CONNECT_AUTOBUY, FAQ, INSTRUCTION_CALLBACK
from vpars_bot.reply_menu import HOME
from vpars_bot.wallet import WalletService
from vpars_bot.wallet_accounts import wallet_account
from vpars_bot.wallet_onboarding import (
    CHECK,
    CONTINUE,
    COUNTRIES,
    EXIT,
    SetupService,
    account_settings,
)

menu = menu_fixture


@pytest.mark.parametrize("region", list(COUNTRIES))
async def test_complete_wizard_saves_only_explicit_budget_and_keeps_search(menu, services, region):
    catalog, control, _ = services
    for uid, name in ((101, "alice"), (202, "bob")):
        await menu.send("/start", user_id=uid, username=name)
        for brand in ("Amiri", "Stone Island"):
            await catalog.save(
                uid, Filters(keywords=[brand], sources={"vinted_de": ["DE"]}), f"{uid}-{brand}"
            )
        await control.start(uid, f"{uid}-start")
    before = {
        uid: sorted((c.id, c.kind, c.payload) for c in await search_commands(catalog, uid))
        for uid in (101, 202)
    }
    before_users = {uid: await catalog.user(uid) for uid in (101, 202)}
    await menu.send(FAQ)
    await menu.send(CONNECT_AUTOBUY)
    await menu.send(COUNTRIES[region])
    await menu.send("100,50")
    assert "100.50" in menu.panel().text
    async with catalog.sessions() as session:
        assert (await wallet_account(session, 101, region)).max_total == 10050
    assert "Крок 3/3" in menu.panel().text and "refresh_token_web" in menu.panel().text
    assert "Application" in menu.panel().text
    await menu.send(CHECK)
    assert "Викуп вимкнений" in menu.panel().text
    # Buttons are navigation, never a claim that the user logged in or verified checkout.
    await menu.send(CHECK)
    await menu.send(EXIT)
    async with catalog.sessions() as session:
        account = await wallet_account(session, 101)
        assert account.region == region and account.max_total == 10050
        assert account.currency == ("GBP" if region == "UK" else "EUR")
        assert not account.enabled and account.contract_hash is None
        assert await wallet_account(session, 202) is None
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
    for uid in (101, 202):
        assert (
            sorted((c.id, c.kind, c.payload) for c in await search_commands(catalog, uid))
            == before[uid]
        )
        assert await catalog.count(uid) == 2
        after = await catalog.user(uid)
        assert after.delivery_epoch == before_users[uid].delivery_epoch
        assert after.enabled_sources == before_users[uid].enabled_sources


@pytest.mark.parametrize("bad", ["0", "-1", "1.234", "nan", "1e3", "1000001", "cookie=secret"])
async def test_invalid_budget_stays_on_same_step_without_storing_input(menu, services, bad):
    await menu.send("/start")
    await menu.send(FAQ)
    await menu.send(CONNECT_AUTOBUY)
    await menu.send(COUNTRIES["DE"])
    before = (await (await menu.state()).get_data())["autobuy_draft"]
    await menu.send(bad)
    assert (await (await menu.state()).get_data())["autobuy_draft"] == before
    async with services[0].sessions() as session:
        assert await wallet_account(session, 101) is None
    assert "cookie=secret" not in str(await (await menu.state()).get_data())


async def test_existing_account_browse_cancel_and_other_user_drafts_are_independent(menu, services):
    for uid in (101, 202):
        await menu.send("/start", user_id=uid)
    async with services[0].sessions.begin() as session:
        session.add(
            WalletAccount(
                user_id=101,
                region="DE",
                currency="EUR",
                max_total=12000,
                enabled=True,
                contract_hash="original",
            )
        )
    await menu.send(FAQ)
    await menu.send("Підключити Autobuy")  # Old reply keyboard still opens the new wizard.
    assert COUNTRIES["DE"] in labels(menu)
    await menu.send(COUNTRIES["DE"])
    await menu.send(CONTINUE)
    await menu.send("🌍 Інші регіони")
    await menu.send(EXIT)
    await menu.send(CONNECT_AUTOBUY)
    await menu.send(COUNTRIES["UK"])
    first_draft = (await (await menu.state()).get_data())["autobuy_draft"]
    await menu.send(FAQ, user_id=202)
    await menu.send(CONNECT_AUTOBUY, user_id=202)
    await menu.send(COUNTRIES["IT"], user_id=202)
    await menu.send("55", user_id=202)
    assert (await (await menu.state()).get_data())["autobuy_draft"] == first_draft
    await menu.send(HOME)
    assert (await (await menu.state()).get_data())["autobuy_draft"] is None
    async with services[0].sessions() as session:
        account = await wallet_account(session, 101)
        assert account_settings(account) == {
            "region": "DE",
            "currency": "EUR",
            "max_total": 12000,
            "enabled": True,
            "contract_hash": "original",
        }
        assert (await wallet_account(session, 202)).max_total == 5500


async def test_setup_refuses_expired_access_active_order_and_concurrent_settings(menu, services):
    await menu.send("/start")
    sessions = services[0].sessions
    setup = SetupService(sessions)
    draft = await setup.draft(101)
    draft.update(region="DE", max_total=10000)
    async with sessions.begin() as session:
        await session.delete(await session.get(Subscription, 101))
    with pytest.raises(UserError, match="активна підписка"):
        await setup.save(101, draft)
    async with sessions.begin() as session:
        # Re-use the existing administrator entitlement policy, not a wizard exception.
        (await session.get(User, 101)).is_admin = True
        session.add(
            WalletAccount(
                user_id=101,
                region="DE",
                currency="EUR",
                max_total=5000,
                enabled=False,
                active_order="pending",
            )
        )
    with pytest.raises(UserError, match="попередню покупку"):
        await setup.save(101, draft)
    async with sessions.begin() as session:
        (await wallet_account(session, 101)).active_order = None
    with pytest.raises(UserError, match="вже змінилися"):
        await setup.save(101, draft)
    async with sessions() as session:
        assert (await wallet_account(session, 101)).max_total == 5000


async def test_idempotent_save_does_not_revoke_later_verification(menu, services):
    await menu.send("/start")
    setup = SetupService(services[0].sessions)
    draft = await setup.draft(101)
    draft.update(region="DE", max_total=10000)
    await setup.save(101, draft)
    async with services[0].sessions.begin() as session:
        account = await wallet_account(session, 101)
        account.enabled, account.contract_hash = True, "verified"
    await setup.save(101, draft)  # Replay after commit / interrupted response.
    unchanged = await setup.draft(101)
    await setup.save(101, unchanged)
    async with services[0].sessions() as session:
        account = await wallet_account(session, 101)
        assert account.enabled and account.contract_hash == "verified"
    changed = await setup.draft(101)
    changed["max_total"] = 15000
    await setup.save(101, changed)
    async with services[0].sessions() as session:
        account = await wallet_account(session, 101)
        assert not account.enabled and account.contract_hash is None


async def test_status_uses_verified_config_and_current_access_not_guide_buttons(
    menu, services, monkeypatch
):
    await menu.send("/start")
    sessions = services[0].sessions
    async with sessions.begin() as session:
        session.add(
            WalletAccount(
                user_id=101,
                region="DE",
                currency="EUR",
                max_total=10000,
                enabled=True,
                contract_hash="verified",
            )
        )
    from vpars_bot import wallet_browser

    monkeypatch.setattr(wallet_browser, "load_contract", lambda uid, region=None: ({"region": "DE"}, "changed"))
    wallet = WalletService(sessions)
    assert "повторна перевірка" in await wallet.connection_text(101)
    monkeypatch.setattr(wallet_browser, "load_contract", lambda uid, region=None: ({"region": "DE"}, "verified"))
    assert "✅ Підключення перевірене" in await wallet.connection_text(101)
    async with sessions.begin() as session:
        await session.delete(await session.get(Subscription, 101))
    assert "активна підписка" in await wallet.connection_text(101)
    await menu.click(data=INSTRUCTION_CALLBACK)
    assert "Підключення за 3 кроки" in menu.panel().text


@pytest.mark.integration
async def test_pg_concurrent_setup_confirmations_commit_once(pg_db, registry):
    sessions = pg_db[1]
    await Catalog(sessions, registry).register(101, 101, "alice", "Alice", 1, start=True)
    setup = SetupService(sessions)
    draft = await setup.draft(101)
    draft.update(region="UK", max_total=10050)
    await asyncio.gather(setup.save(101, draft), setup.save(101, draft))
    async with sessions() as session:
        account = await wallet_account(session, 101)
        assert account.max_total == 10050 and account.currency == "GBP" and not account.enabled
        assert (
            await session.scalar(
                select(func.count()).select_from(Operation).where(Operation.kind == "wallet_setup")
            )
            == 1
        )
