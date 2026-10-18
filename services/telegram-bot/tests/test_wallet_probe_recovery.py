"""Checkout interruption retains verified sessions without reusing refresh keys."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_wallet_regions import seed
from test_wallet_sessions import key

from vpars_bot.db import Notification, WalletAccount, WalletOrder, WalletSession, utcnow
from vpars_bot.domain import UserError
from vpars_bot.session_secrets import SessionCipher
from vpars_bot.wallet import ERRORS, REGIONS, WalletError
from vpars_bot.wallet_accounts import wallet_session
from vpars_bot.wallet_sessions import SessionService, SessionWorker, claims


@pytest.fixture
async def connected(db, registry):
    sessions = db[1]
    await seed(sessions, registry)
    cipher = SessionCipher(Fernet.generate_key())
    service = SessionService(sessions, cipher)
    async with sessions.begin() as session:
        for uid in (101, 202):
            for region in REGIONS:
                attempt = f"{uid}-{region}"
                session.add(WalletSession(
                    user_id=uid, region=region, attempt_id=attempt, state="connected",
                    error="CheckoutChallenge", account_key=str(uid), auto_enable=True,
                    expires_at=utcnow() + timedelta(hours=1),
                    secret=cipher.seal([{"name": "access_token_web", "value": key(
                        region, purpose="access", subject=str(uid))}],
                        owner=uid, purpose=f"vinted-cookies:{attempt}"),
                ))
    return sessions, cipher, service


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("owner", [101, 202])
async def test_restart_during_probe_keeps_session_and_never_retries_automatically(connected, region, owner):
    sessions, cipher, service = connected
    async with sessions() as session:
        before = {(r.user_id, r.region): (r.secret, r.state, r.error) for r in
                  await session.scalars(select(WalletSession))}
    await service.probe(owner, region, auto_enable=True)
    verifier, installer = AsyncMock(), AsyncMock()
    checkout = AsyncMock(side_effect=asyncio.CancelledError())
    worker = SessionWorker(sessions, verifier=verifier, cipher=cipher, installer=installer, checkout=checkout)
    with pytest.raises(asyncio.CancelledError):
        await worker.once()
    assert "оформлення" in await service.status(owner, region)
    assert "Перевіряю ключ" not in await service.status(owner, region)
    await worker.recover()
    assert not await worker.once()
    async with sessions() as session:
        for row in await session.scalars(select(WalletSession)):
            expected = before[row.user_id, row.region]
            assert row.secret == expected[0]
            if (row.user_id, row.region) == (owner, region):
                assert (row.state, row.error) == ("connected", "CheckoutInterrupted")
            else:
                assert (row.state, row.error) == expected[1:]
        assert all(not r.enabled for r in await session.scalars(select(WalletAccount)))
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
    verifier.assert_not_awaited()
    installer.assert_not_awaited()
    assert "без повторного надсилання ключа" in await service.status(owner, region)


@pytest.mark.parametrize("old_state", ["probe_pending", "probing", "checking"])
async def test_disable_preserves_verified_session_but_cancels_activation(connected, old_state):
    sessions, _, service = connected
    async with sessions.begin() as session:
        row = await wallet_session(session, 101, "DE")
        row.state = old_state
        saved = row.secret
    await service.disable(101, "DE")
    async with sessions() as session:
        row = await wallet_session(session, 101, "DE")
        assert (row.state, row.error, row.secret) == ("connected", "CheckoutPaused", saved)
        assert not row.auto_enable
    assert (await service.actions(101, "DE"))["probe"]


async def test_new_connection_checkpoints_verified_session_before_checkout(db, registry):
    sessions = db[1]
    await seed(sessions, registry)
    cipher = SessionCipher(Fernet.generate_key())
    service = SessionService(sessions, cipher)
    async def verifier(region, token, persist):
        await persist([{"name": "synthetic", "value": "rotated"}])
        return claims(token)["sub"], utcnow() + timedelta(hours=1)
    installer = AsyncMock()
    worker = SessionWorker(sessions, verifier, cipher, installer,
                           AsyncMock(side_effect=asyncio.CancelledError()))
    for region in REGIONS:
        await service.submit(101, key(region), f"initial-{region}", region=region, auto_enable=True)
        with pytest.raises(asyncio.CancelledError):
            await worker.once()
        await worker.recover()
        async with sessions() as session:
            row = await wallet_session(session, 101, region)
            assert (row.state, row.error, row.account_key) == ("connected", "CheckoutInterrupted", "123")
            assert cipher.open(row.secret, owner=101, purpose=f"vinted-cookies:{row.attempt_id}")[0]["value"] == "rotated"
    assert installer.await_count == 4
    assert not await worker.once()


async def test_cancelled_old_probe_cannot_overwrite_new_probe_for_same_session(connected):
    sessions, cipher, service = connected
    first_started, first_finish = asyncio.Event(), asyncio.Event()
    second_started, second_finish = asyncio.Event(), asyncio.Event()
    async def old_checkout(*args, **kwargs):
        first_started.set()
        await first_finish.wait()
        return "outdated-success"
    async def new_checkout(*args, **kwargs):
        second_started.set()
        await second_finish.wait()
        raise WalletError("CheckoutChallenge")
    first = SessionWorker(sessions, cipher=cipher, checkout=old_checkout)
    second = SessionWorker(sessions, cipher=cipher, checkout=new_checkout)
    await service.probe(101, "DE", auto_enable=True)
    task1 = asyncio.create_task(first.once())
    await asyncio.wait_for(first_started.wait(), 3)
    await service.disable(101, "DE")
    await service.probe(101, "DE", auto_enable=True)
    task2 = asyncio.create_task(second.once())
    try:
        await asyncio.wait_for(second_started.wait(), 3)
        first_finish.set()
        await asyncio.wait_for(task1, 3)
        async with sessions() as session:
            assert (await wallet_session(session, 101, "DE")).state == "probing"
            account = await session.get(WalletAccount, (101, "DE"))
            assert not account.enabled and account.contract_hash is None
        second_finish.set()
        await asyncio.wait_for(task2, 3)
        async with sessions() as session:
            assert (await wallet_session(session, 101, "DE")).error == "CheckoutChallenge"
        assert not await first.once()
    finally:
        first_finish.set()
        second_finish.set()
        await asyncio.gather(task1, task2, return_exceptions=True)


async def test_challenge_never_auto_retries_or_clears_key_for_any_region(connected):
    sessions, cipher, service = connected
    verifier, installer = AsyncMock(), AsyncMock()
    worker = SessionWorker(sessions, verifier, cipher, installer,
                           AsyncMock(side_effect=WalletError("CheckoutChallenge")))
    for uid in (101, 202):
        for region in REGIONS:
            await service.probe(uid, region, auto_enable=True)
            assert await worker.once()
            assert not await worker.once()
            async with sessions() as session:
                row = await wallet_session(session, uid, region)
                assert row.state == "connected" and row.secret
            assert ERRORS["CheckoutChallenge"] in await service.status(uid, region)
    verifier.assert_not_awaited()
    installer.assert_not_awaited()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
        assert all("CAPTCHA" in n.payload["text"] for n in await session.scalars(select(Notification)))


async def test_initial_access_expiry_does_not_request_another_key_or_hide_challenge(connected):
    sessions, _, service = connected
    async with sessions.begin() as session:
        (await wallet_session(session, 101, "DE")).expires_at = utcnow() - timedelta(seconds=1)
    status = await service.status(101, "DE")
    assert "CAPTCHA" in status and "повторна перевірка оформлення" in status
    assert "Додай свіжий ключ" not in status


async def test_probe_blocks_new_key_until_current_checkout_finishes(connected):
    sessions, _, service = connected
    async with sessions.begin() as session:
        (await wallet_session(session, 101, "DE")).state = "probing"
    with pytest.raises(UserError, match="вже триває"):
        await service.submit(101, key(), "replacement", region="DE")
