"""Approval authorizes only the displayed price, delivery and saved account."""

from datetime import timedelta

import pytest
from test_wallet_button import connected
from test_wallet_mock import checkout_mock as checkout_mock
from test_wallet_mock import configure, ledger, saved, seed

from vpars_bot.db import WalletOrder, utcnow
from vpars_bot.domain import UserError
from vpars_bot.wallet import WalletError
from vpars_bot.wallet_accounts import wallet_account, wallet_session
from vpars_bot.wallet_mode import checkout_mode


@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_or_expiry_blocks_approve_and_releases_order(db, registry, checkout_mock, cancel):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        await worker.once()
        if cancel:
            assert (await service.decide(101, order.id, pay=False)).state == "cancelled"
        else:
            async with db[1].begin() as session:
                (await session.get(WalletOrder, order.id)).expires_at = utcnow() - timedelta(seconds=1)
        result = await service.decide(101, order.id, pay=True, approve=True)
        assert result.state == ("cancelled" if cancel else "failed")
        if not cancel:
            assert result.error == "Expired"
        assert not await worker.once()
        assert (await ledger(checkout_mock))["payment_attempts"] == 0
        async with db[1]() as session:
            assert (await wallet_account(session, 101, "DE")).active_order is None


@pytest.mark.parametrize("changes,error", [
    ({"shipping": 250}, "CheckoutChanged"),
    ({"balance": 1000}, "InsufficientBalance"),
    ({"delivery": "pickup-b"}, "CheckoutChanged"),
    ({"payment_method": "Card"}, "WalletOnly"),
])
async def test_approve_never_pays_changed_quote(db, registry, checkout_mock, changes, error):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        await worker.once()
        assert (await saved(db[1], order)).state == "bot_quoted"
        await service.decide(101, order.id, pay=True, approve=True)
        await configure(checkout_mock, **changes)
        await worker.once()
        result = await saved(db[1], order)
        assert (result.state, result.error) == ("failed", error)
        assert (await ledger(checkout_mock))["payment_attempts"] == 0


@pytest.mark.parametrize("after_approve", [False, True])
async def test_new_session_cannot_use_previous_approval(db, registry, checkout_mock, after_approve):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        await worker.once()
        if after_approve:
            await service.decide(101, order.id, pay=True, approve=True)
        async with db[1].begin() as session:
            (await wallet_session(session, 101, "DE")).attempt_id = "replacement"
        if after_approve:
            await worker.once()
            assert (await saved(db[1], order)).error == "NotConnected"
        else:
            with pytest.raises(WalletError, match="NotConnected"):
                await service.decide(101, order.id, pay=True, approve=True)
        assert (await ledger(checkout_mock))["payment_attempts"] == 0


async def test_approval_owner_and_legacy_callbacks_cannot_be_confused(db, registry, checkout_mock):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        # An early callback does not confer consent on a later quote.
        assert (await service.decide(101, order.id, pay=True, approve=True)).state == "bot_requested"
        await worker.once()
        for pay in (False, True):
            with pytest.raises(UserError, match="не знайдено"):
                await service.decide(202, order.id, pay=pay, approve=True)
        with pytest.raises(UserError, match="попереднє підтвердження"):
            await service.decide(101, order.id, pay=True)
        assert not await worker.once()
        assert (await saved(db[1], order)).state == "bot_quoted"
        assert (await ledger(checkout_mock))["payment_attempts"] == 0


@pytest.mark.parametrize("approve", [False, True])
async def test_restart_preserves_approval_boundary(db, registry, checkout_mock, approve):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        await worker.once()
        if approve:
            await service.decide(101, order.id, pay=True, approve=True)
        await worker.recover()
        assert bool(await worker.once()) == approve
        assert (await ledger(checkout_mock))["payment_attempts"] == int(approve)
        assert not await worker.once()


async def test_no_approve_expires_without_payment_and_unblocks_next_item(db, registry, checkout_mock):
    service, worker = await seed(db[1], registry, checkout_mock)
    await connected(db[1])
    with checkout_mode(db[1], "bot"):
        order = await service.request(101, 10)
        await worker.once()
        async with db[1].begin() as session:
            (await session.get(WalletOrder, order.id)).expires_at = utcnow() - timedelta(seconds=1)
        assert not await worker.once()
        assert (await saved(db[1], order)).error == "Expired"
        assert (await service.request(101, 12)).state == "bot_requested"
        assert (await ledger(checkout_mock))["payment_attempts"] == 0
