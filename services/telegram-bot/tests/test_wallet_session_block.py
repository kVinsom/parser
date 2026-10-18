"""A site challenge stops new items too, without erasing the saved connection."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from test_wallet_button import connected
from test_wallet_fast import account, pool_for
from test_wallet_mock import checkout_mock as checkout_mock
from test_wallet_mock import ledger, saved, seed
from test_wallet_sessions import cipher as cipher

from vpars_bot.wallet import ERRORS, WalletError, WalletService
from vpars_bot.wallet_accounts import wallet_session
from vpars_bot.wallet_mode import checkout_mode
from vpars_bot.wallet_sessions import SessionService, SessionWorker
from vpars_bot.wallet_worker import WalletWorker


@pytest.mark.parametrize("error", ["CheckoutChallenge", "CheckoutLoginRequired"])
@pytest.mark.parametrize("user_id", [101, 202])
async def test_block_survives_worker_restart_and_new_item_until_successful_probe(
    db, registry, checkout_mock, cipher, user_id, error,
):
    sessions = db[1]
    service, _ = await seed(sessions, registry, checkout_mock)
    await connected(sessions)
    acc = await account(sessions, user_id)
    async with sessions.begin() as session:
        (await wallet_session(session, user_id, acc.region)).secret = "encrypted-placeholder"
    calls = 0

    @asynccontextmanager
    async def blocked(_account):
        nonlocal calls
        calls += 1
        raise WalletError(error)
        yield  # pragma: no cover

    session_service = SessionService(sessions, cipher)
    check = AsyncMock(return_value="a" * 64)
    session_worker = SessionWorker(sessions, cipher=cipher, checkout=check)
    with checkout_mode(sessions, "bot"):
        async with pool_for(checkout_mock, sessions) as pool:
            async with pool.adapter(acc) as adapter:
                warm_page = adapter.page
            worker = WalletWorker(sessions, blocked)
            first = await service.request(user_id, 10)
            assert await worker.once()
            assert (await saved(sessions, first)).error == error
            await pool.maintain(prime=True)
            assert warm_page.is_closed()
            assert (user_id, acc.region) not in pool.entries
            assert not await session_worker.once()  # No automatic challenge probes.
            check.assert_not_awaited()

            # New service/worker instances cannot lose a database-persisted block.
            restarted = WalletService(sessions, service.resolver)
            restarted_worker = WalletWorker(sessions, blocked)
            await restarted_worker.recover()
            assert not await restarted_worker.once()
            assert (await restarted.request(user_id, 11)).id == first.id
            with pytest.raises(WalletError, match=f"^{error}$"):
                await restarted.request(user_id, 12)
            assert calls == 1
            assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 0
            async with sessions() as session:
                connection = await wallet_session(session, user_id, acc.region)
                assert connection.state == "checkout_ready"
                assert connection.error == error
                assert connection.attempt_id == f"session-{user_id}"
                assert connection.account_key == f"mock-{user_id}"
                assert connection.secret == "encrypted-placeholder"
            assert (await account(sessions, user_id)).enabled
            assert (await account(sessions, user_id)).max_total == acc.max_total
            assert await session_service.status(user_id, acc.region) == ERRORS[error]
            assert ERRORS[error] in await service.connection_text(user_id, acc.region)

            # Another owner's browser and checkout remain independent.
            other = 202 if user_id == 101 else 101
            other_order = await service.request(other, 12)
            normal_worker = WalletWorker(sessions, pool.adapter)
            assert await normal_worker.once()
            await service.decide(other, other_order.id, pay=True, approve=True)
            assert await normal_worker.once()
            assert (await saved(sessions, other_order)).state == "succeeded"

            # Only an explicitly requested, successful checkout probe restores Buy.
            await session_service.probe(user_id, acc.region, auto_enable=True)
            assert await session_worker.once()
            check.assert_awaited_once()
            async with sessions() as session:
                assert (await wallet_session(session, user_id, acc.region)).error is None
            next_order = await service.request(user_id, 12)
            assert await normal_worker.once()
            await service.decide(user_id, next_order.id, pay=True, approve=True)
            assert await normal_worker.once()
            assert (await saved(sessions, next_order)).state == "succeeded"
            assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 1


async def test_late_challenge_does_not_block_replacement_session(db, registry, checkout_mock):
    sessions = db[1]
    service, _ = await seed(sessions, registry, checkout_mock)
    await connected(sessions)

    @asynccontextmanager
    async def replaced(_account):
        async with sessions.begin() as session:
            (await wallet_session(session, 101, "DE")).attempt_id = "new-connection"
        raise WalletError("CheckoutChallenge")
        yield  # pragma: no cover

    with checkout_mode(sessions, "bot"):
        first = await service.request(101, 10)
        await WalletWorker(sessions, replaced).once()
        assert (await saved(sessions, first)).error == "CheckoutChallenge"
        async with sessions() as session:
            connection = await wallet_session(session, 101, "DE")
            assert connection.attempt_id == "new-connection" and connection.error is None
        assert (await service.request(101, 12)).state == "bot_requested"
