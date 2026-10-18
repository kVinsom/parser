"""Regional connection isolation, purchase routing and safe legacy migration."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import func, select, text
from test_reply_menu import labels
from test_reply_menu import menu as menu
from test_wallet import good_quote
from test_wallet_sessions import key

from vpars_bot.application import Catalog
from vpars_bot.db import Notification, WalletAccount, WalletOrder, WalletSession, utcnow
from vpars_bot.domain import UserError
from vpars_bot.profile_menu import CONNECT_AUTOBUY, FAQ
from vpars_bot.session_secrets import SessionCipher
from vpars_bot.wallet import REGIONS, WalletError, WalletService
from vpars_bot.wallet_accounts import wallet_account
from vpars_bot.wallet_onboarding import COUNTRIES, SetupService
from vpars_bot.wallet_pool import WarmBrowserPool
from vpars_bot.wallet_sessions import SessionService, SessionWorker
from vpars_bot.wallet_worker import WalletWorker


async def seed(sessions, registry):
    setup = SetupService(sessions)
    for uid in (101, 202):
        await Catalog(sessions, registry).register(uid, uid, f"user{uid}", "Test", uid, start=True)
        for region in REGIONS:
            draft = await setup.draft(uid, region)
            draft["max_total"] = 10000 + uid
            await setup.save(uid, draft)


async def test_all_regions_keep_settings_and_changes_independent(db, registry):
    sessions = db[1]
    await seed(sessions, registry)
    async with sessions.begin() as session:
        for row in await session.scalars(select(WalletAccount)):
            row.enabled, row.contract_hash = True, f"{row.user_id}-{row.region}"
    setup = SetupService(sessions)
    draft = await setup.draft(101, "FR")
    draft["max_total"] = 7777
    await setup.save(101, draft)
    async with sessions() as session:
        rows = list(await session.scalars(select(WalletAccount)))
        assert len(rows) == 8
        for row in rows:
            assert row.currency == REGIONS[row.region][1]
            if (row.user_id, row.region) == (101, "FR"):
                assert row.max_total == 7777 and not row.enabled and row.contract_hash is None
            else:
                assert row.max_total == 10000 + row.user_id and row.enabled
                assert row.contract_hash == f"{row.user_id}-{row.region}"
        with pytest.raises(UserError, match="Обери регіон"):
            await wallet_account(session, 101)


async def test_three_step_menu_connects_four_regions_for_same_user(menu, services):
    await menu.send("/start")
    await menu.send(FAQ)
    await menu.send(CONNECT_AUTOBUY)
    for number, region in enumerate(COUNTRIES):
        await menu.send(COUNTRIES[region])
        await menu.send(str(50 + number))
        assert "Крок 3/3" in menu.panel().text
        assert "refresh_token_web" in menu.panel().text
        assert "📦 До налаштування доставки" not in labels(menu)
        await menu.send("🌍 Інші регіони")
    async with services[0].sessions() as session:
        rows = list(await session.scalars(select(WalletAccount)))
        assert len(rows) == 4 and all(row.user_id == 101 for row in rows)
        assert {row.max_total for row in rows} == {5000, 5100, 5200, 5300}
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0


async def test_sessions_activate_only_their_region_and_reconnect_isolated(db, registry):
    sessions = db[1]
    await seed(sessions, registry)
    cipher = SessionCipher(Fernet.generate_key())
    service = SessionService(sessions, cipher)

    async def verifier(region, token, persist):
        from vpars_bot.wallet_sessions import claims

        await persist([{"name": "test-cookie", "value": "rotated"}])
        return claims(token)["sub"], utcnow() + timedelta(hours=1)

    checkout = AsyncMock(return_value="verified-digest")
    installer = AsyncMock()
    worker = SessionWorker(sessions, verifier, cipher, installer, checkout)
    for uid in (101, 202):
        for region in REGIONS:
            await service.submit(uid, key("GB" if region == "UK" else region, subject=str(uid)),
                                 f"{uid}-{region}", region=region, auto_enable=True)
    for _ in range(8):
        assert await worker.once()
    assert not await worker.once()
    assert {(c.args[0], c.args[1]) for c in installer.await_args_list} == {
        (uid, region) for uid in (101, 202) for region in REGIONS
    }
    await service.disable(101, "DE")
    await service.submit(202, key("FR", subject="202"), "reconnect", region="FR", auto_enable=True)
    async with sessions() as session:
        for row in await session.scalars(select(WalletAccount)):
            assert row.enabled == ((row.user_id, row.region) not in {(101, "DE"), (202, "FR")})
        assert (await session.get(WalletSession, (202, "FR"))).state == "pending"
        assert (await session.get(WalletSession, (202, "DE"))).state == "checkout_ready"
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
    await service.submit(101, key("US"), "reconnect-de", region="DE")
    async with sessions() as session:
        assert (await session.get(WalletSession, (101, "DE"))).state == "pending"
        assert (await session.get(WalletSession, (101, "FR"))).state == "checkout_ready"


async def test_disabling_inflight_verification_cannot_reenable_region(db, registry):
    await seed(db[1], registry)
    cipher = SessionCipher(Fernet.generate_key())
    service = SessionService(db[1], cipher)
    await service.submit(101, key(), "connect", region="DE", auto_enable=True)

    async def verifier(region, token, persist):
        await persist([])
        await service.disable(101, "DE")
        return "101", utcnow() + timedelta(hours=1)

    worker = SessionWorker(db[1], verifier, cipher, AsyncMock(), AsyncMock(return_value="digest"))
    await worker.once()
    async with db[1]() as session:
        assert not (await session.get(WalletAccount, (101, "DE"))).enabled
        assert (await session.get(WalletSession, (101, "DE"))).state == "failed"


async def test_no_listing_retries_only_checkout_and_stops_when_disabled(db, registry):
    await seed(db[1], registry)
    cipher = SessionCipher(Fernet.generate_key())
    service = SessionService(db[1], cipher)
    async with db[1].begin() as session:
        session.add(WalletSession(user_id=101, region="DE", attempt_id="test", state="connected",
                                  error="NoListing", auto_enable=True, account_key="101",
                                  updated_at=utcnow() - timedelta(minutes=2)))
    checkout = AsyncMock(side_effect=WalletError("NoListing"))
    verifier = AsyncMock()
    worker = SessionWorker(db[1], verifier, cipher, AsyncMock(), checkout)
    assert await worker.once()
    assert not await worker.once()
    verifier.assert_not_awaited()
    async with db[1].begin() as session:
        assert await session.scalar(select(func.count()).select_from(Notification)) == 0
        (await session.get(WalletSession, (101, "DE"))).updated_at -= timedelta(minutes=2)
    checkout.side_effect = None
    checkout.return_value = "digest"
    assert await worker.once()
    assert "готовий" in await service.status(101, "DE")
    await service.disable(101, "DE")
    assert not await worker.once()


async def test_parallel_orders_route_quote_cancel_and_recovery_to_own_region(db, registry):
    sessions = db[1]
    await seed(sessions, registry)
    async with sessions.begin() as session:
        for row in await session.scalars(select(WalletAccount)):
            row.enabled, row.contract_hash = True, "verified"
    regions = list(REGIONS)

    async def resolver(uid, mid):
        region = regions[mid]
        return {"item_url": f"https://{REGIONS[region][0]}/items/{1000 + mid}",
                "price": {"amount": "10.00", "currency": REGIONS[region][1]}}

    bindings = []

    @asynccontextmanager
    async def factory(account):
        bindings.append((account.user_id, account.region))
        async def quote(order):
            return replace(good_quote(), item_id=order.item_id, currency=account.currency,
                           account_key=f"{account.user_id}-{account.region}")
        yield SimpleNamespace(checkout=quote)

    service = WalletService(sessions, resolver)
    orders = [await service.request(uid, mid) for uid in (101, 202) for mid in range(4)]
    worker = WalletWorker(sessions, factory)
    for _ in orders:
        assert await worker.once()
    assert set(bindings) == {(uid, region) for uid in (101, 202) for region in REGIONS}
    await service.decide(101, orders[0].id, pay=False)
    async with sessions.begin() as session:
        for i, order in enumerate(orders):
            region = regions[i % 4]
            row = await session.get(WalletAccount, (order.user_id, region))
            assert row.active_order == (None if i == 0 else order.id)
        (await session.get(WalletOrder, orders[1].id)).state = "checking"
        (await session.get(WalletOrder, orders[2].id)).state = "paying"
    await worker.recover()
    async with sessions() as session:
        assert (await session.get(WalletAccount, (101, regions[1]))).active_order is None
        assert (await session.get(WalletAccount, (101, regions[2]))).active_order == orders[2].id
        assert (await session.get(WalletOrder, orders[2].id)).state == "unknown"
        assert (await session.get(WalletOrder, orders[3].id)).state == "quoted"


async def test_profiles_contracts_locks_and_pool_do_not_share_regions(tmp_path, monkeypatch):
    from vpars_bot import wallet_browser as browser

    monkeypatch.setattr(browser, "ROOT", tmp_path)
    old = browser.contract_path(101)
    old.parent.mkdir()
    old.write_text(json.dumps({"region": "DE"}), "utf-8")
    assert browser.contract_path(101, "DE") == old
    assert browser.profile_dir(101, "DE") == browser.profile_dir(101)
    assert len({browser.profile_dir(101, region) for region in REGIONS}) == 4
    assert len({browser.contract_path(101, region) for region in REGIONS}) == 4
    assert browser.profile_dir(101, "FR") != browser.profile_dir(202, "FR")
    async with browser.profile_lock(101, "DE"):
        async with browser.profile_lock(101, "FR", timeout=0):
            pass
        with pytest.raises(WalletError, match="ProfileBusy"):
            async with browser.profile_lock(101, "DE", timeout=0):
                pass

    @asynccontextmanager
    async def factory(account):
        yield SimpleNamespace(page=SimpleNamespace(is_closed=lambda: False))

    async with WarmBrowserPool(None, factory=factory,
                               loader=lambda uid, region: ({"region": region}, "digest")) as pool:
        accounts = [SimpleNamespace(user_id=101, region=r, currency=REGIONS[r][1],
                                    enabled=True, contract_hash="digest") for r in REGIONS]
        async def lease(account):
            async with pool.adapter(account) as adapter:
                await asyncio.sleep(.01)
                return adapter.page
        pages = await asyncio.wait_for(asyncio.gather(*(lease(acc) for acc in accounts)), 2)
        assert len({id(page) for page in pages}) == 4
        assert set(pool.entries) == {(101, region) for region in REGIONS}


@pytest.mark.integration
async def test_migration_preserves_existing_account_and_session(pg_db, registry):
    engine, sessions = pg_db
    await Catalog(sessions, registry).register(101, 101, "migration", "Test", 1, start=True)

    def migrate(connection, revision, down=False):
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(cfg, revision)

    async with engine.begin() as connection:
        await connection.run_sync(lambda c: migrate(c, "0011", True))
        await connection.execute(text("INSERT INTO bot_wallet_accounts "
            "(user_id,region,currency,max_total,enabled,contract_hash,active_order) "
            "VALUES (101,'DE','EUR',7654,true,'old-digest','unresolved')"))
        await connection.execute(text("INSERT INTO bot_wallet_sessions "
            "(user_id,region,attempt_id,state,secret,account_key,updated_at) "
            "VALUES (101,'DE','attempt','connected','encrypted-fixture','42',CURRENT_TIMESTAMP)"))
        before = (await connection.execute(text("SELECT * FROM bot_wallet_accounts"))).one()
        await connection.run_sync(lambda c: migrate(c, "head"))
        assert (await connection.execute(text("SELECT * FROM bot_wallet_accounts"))).one() == before
    async with sessions.begin() as session:
        row = await session.get(WalletSession, (101, "DE"))
        assert row.secret == "encrypted-fixture" and row.account_key == "42"
        assert not row.auto_enable
        session.add(WalletAccount(user_id=101, region="FR", currency="EUR", max_total=9000))
    async with sessions() as session:
        assert (await session.get(WalletAccount, (101, "DE"))).active_order == "unresolved"
        assert (await session.get(WalletAccount, (101, "FR"))).max_total == 9000
