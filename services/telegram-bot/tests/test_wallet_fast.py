
"""Warm browser isolation, commit wakeups and concurrent durable wallet execution."""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from statistics import median
from time import perf_counter
from types import SimpleNamespace

import pytest
from aiogram import Bot
from conftest import OfflineSession
from pydantic import SecretStr
from test_wallet import setup
from test_wallet_mock import checkout_mock as checkout_mock
from test_wallet_mock import configure, ledger, saved, seed

from vpars_bot.notifications import NotificationSender
from vpars_bot.wallet import WalletError
from vpars_bot.wallet_accounts import wallet_account
from vpars_bot.wallet_browser import SELECTORS, profile_lock
from vpars_bot.wallet_pool import WarmBrowserPool
from vpars_bot.wallet_signals import signals
from vpars_bot.wallet_worker import WalletWorker


def contract(user_id, region="DE"):
    return {
        "version": 1,
        "region": region,
        "expected_account": f"mock-{user_id}",
        "wallet_label": "Mock balance",
        "decimal_separator": ".",
        "selectors": {name: {"css": "#" + name} for name in SELECTORS},
    }


def pool_for(mock, sessions, **kwargs):
    return WarmBrowserPool(
        sessions,
        factory=mock.bridge.adapter,
        loader=lambda key, region: (contract(key, mock.site.accounts[str(key)].region), "a" * 64),
        **kwargs,
    )


async def account(sessions, user_id=101):
    async with sessions() as session:
        return await wallet_account(session, user_id)


async def test_warm_checkout_reuses_browser_but_refreshes_quote_and_budget(
    db, registry, checkout_mock
):
    service, _ = await seed(db[1], registry, checkout_mock)
    async with pool_for(checkout_mock, db[1]) as pool:
        worker = WalletWorker(db[1], pool.adapter)
        order = await service.request(101, 10)
        await worker.once()
        first_page = pool.entries[(101, "DE")].adapter.page
        assert (await saved(db[1], order)).state == "quoted"
        await service.decide(101, order.id, pay=True)
        await worker.once()
        assert pool.entries[(101, "DE")].adapter.page is first_page
        assert (await saved(db[1], order)).state == "succeeded"
        assert (await ledger(checkout_mock))["payment_attempts"] == 1
        assert checkout_mock.site.requests.count(("GET", "/items/123")) == 1
        assert checkout_mock.site.requests.count(("GET", "/checkout")) == 2
        # Next item gets current account limits, not the account captured on first launch.
        async with db[1].begin() as session:
            (await wallet_account(session, 101)).max_total = 1300
        another = await service.request(101, 12)
        await worker.once()
        assert (await saved(db[1], another)).error == "BudgetExceeded"
        assert (await ledger(checkout_mock))["payment_attempts"] == 1
    assert first_page.is_closed()


async def test_warm_checkout_detects_delivery_change_without_relaunch(db, registry, checkout_mock):
    service, _ = await seed(db[1], registry, checkout_mock)
    async with pool_for(checkout_mock, db[1]) as pool:
        worker = WalletWorker(db[1], pool.adapter)
        order = await service.request(101, 10)
        await worker.once()
        await service.decide(101, order.id, pay=True)
        await configure(checkout_mock, delivery="pickup-b")
        await worker.once()
        assert (await saved(db[1], order)).error == "CheckoutChanged"
        assert (await ledger(checkout_mock))["payment_attempts"] == 0


async def test_warm_pool_isolation_capacity_and_idle_expiry(db, registry, checkout_mock):
    await seed(db[1], registry, checkout_mock)
    clock = [10.0]
    async with pool_for(checkout_mock, db[1], capacity=2, clock=lambda: clock[0]) as pool:
        async with pool.adapter(await account(db[1], 101)) as a:
            first = a.page
        clock[0] += 1
        async with pool.adapter(await account(db[1], 202)) as b:
            second = b.page
            assert second.context is not first.context
        clock[0] += 1
        async with pool.adapter(await account(db[1], 303)):
            assert len(pool.entries) == 2
        assert first.is_closed() and not second.is_closed()
        clock[0] += 181
        await pool.maintain()
        assert not pool.entries and second.is_closed()


async def test_warm_pool_serializes_same_profile_and_releases_disabled(db, registry, checkout_mock):
    await seed(db[1], registry, checkout_mock)
    acc = await account(db[1])
    waiting = asyncio.Event()
    entered = asyncio.Event()
    async with pool_for(checkout_mock, db[1]) as pool:

        async def borrow():
            waiting.set()
            async with pool.adapter(acc):
                entered.set()

        async with pool.adapter(acc) as adapter:
            task = asyncio.create_task(borrow())
            await waiting.wait()
            await asyncio.sleep(0)
            assert not entered.is_set()
        await asyncio.wait_for(task, 2)
        async with db[1].begin() as session:
            (await wallet_account(session, 101)).enabled = False
        await pool.maintain()
        assert adapter.page.is_closed() and not pool.entries


async def test_warm_pool_rechecks_contract_and_discards_cancelled_context(
    db, registry, checkout_mock
):
    await seed(db[1], registry, checkout_mock)
    acc = await account(db[1])
    async with pool_for(checkout_mock, db[1]) as pool:
        async with pool.adapter(acc) as adapter:
            page = adapter.page
        pool.loader = lambda _, region: (contract(101), "b" * 64)
        with pytest.raises(WalletError, match="ContractMismatch"):
            async with pool.adapter(acc):
                pytest.fail("Changed contract must not be leased")
        pool.loader = lambda _, region: (contract(101), "a" * 64)
        with pytest.raises(asyncio.CancelledError):
            async with pool.adapter(acc):
                raise asyncio.CancelledError
        assert page.is_closed() and not pool.entries


async def test_profile_guard_releases_after_cancel_and_blocks_parallel_setup(monkeypatch, tmp_path):
    import vpars_bot.wallet_browser as module

    monkeypatch.setattr(module, "ROOT", tmp_path)
    async with profile_lock(101):
        with pytest.raises(WalletError, match="ProfileBusy"):
            async with profile_lock(101, timeout=0):
                pytest.fail("Same profile opened twice")
        async with profile_lock(202, timeout=0):
            pass
    async with profile_lock(101, timeout=0):
        pass


async def stop(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_commit_wakes_wallet_and_notifications_without_polling(db, registry, monkeypatch):
    import vpars_bot.notifications as notifier
    import vpars_bot.wallet_worker as processor

    service, site, worker = await setup(db[1], registry)
    wallet_idle, notifications_idle = asyncio.Event(), asyncio.Event()

    async def wallet_wait(event):
        wallet_idle.set()
        await event.wait()  # No timeout fallback: only an actual commit wakeup works.

    async def notification_wait(event):
        notifications_idle.set()
        await event.wait()

    monkeypatch.setattr(processor, "wait_for_work", wallet_wait)
    monkeypatch.setattr(notifier, "wait_for_work", notification_wait)
    delivered = asyncio.Event()
    transport = OfflineSession()
    original = transport.make_request

    async def recording(*args, **kwargs):
        result = await original(*args, **kwargs)
        delivered.set()
        return result

    transport.make_request = recording
    async with Bot("42:offline", session=transport) as bot:
        send_task = asyncio.create_task(NotificationSender(db[1], bot).run())
        work_task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(wallet_idle.wait(), 3)
            await asyncio.wait_for(notifications_idle.wait(), 3)
            order = await service.request(101, 10)
            await asyncio.wait_for(delivered.wait(), 3)
            assert (await saved(db[1], order)).state == "quoted"
            delivered.clear()
            await service.decide(101, order.id, pay=True)
            await asyncio.wait_for(delivered.wait(), 3)
            assert (await saved(db[1], order)).state == "succeeded"
            assert site.payments == 1
        finally:
            await stop(work_task)
            await stop(send_task)


@pytest.mark.integration
async def test_slow_account_does_not_block_another_or_duplicate_claim(
    pg_db, registry, checkout_mock
):
    service, _ = await seed(pg_db[1], registry, checkout_mock)
    slow_started, release_slow, fast_quoted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with pool_for(checkout_mock, pg_db[1]) as pool:

        @asynccontextmanager
        async def delayed(acc):
            async with pool.adapter(acc) as adapter:
                if acc.user_id == 101:
                    slow_started.set()
                    await release_slow.wait()
                yield adapter
                if acc.user_id == 202:
                    fast_quoted.set()

        worker = WalletWorker(pg_db[1], delayed, concurrency=2)
        task = asyncio.create_task(worker.run())
        try:
            first = await service.request(101, 10)
            await asyncio.wait_for(slow_started.wait(), 5)
            second = await service.request(202, 12)
            await asyncio.wait_for(fast_quoted.wait(), 5)
            assert (await saved(pg_db[1], second)).state == "quoted"
            assert (await saved(pg_db[1], first)).state == "checking"
            assert (await service.request(101, 11)).id == first.id
            await service.decide(202, second.id, pay=True)
            for _ in range(100):
                if (await saved(pg_db[1], second)).state == "succeeded":
                    break
                await asyncio.sleep(0.02)
            assert (await saved(pg_db[1], second)).state == "succeeded"
            assert (await ledger(checkout_mock, 202))["debits"] == 1
            assert (await ledger(checkout_mock, 101))["payment_attempts"] == 0
        finally:
            release_slow.set()
            await stop(task)


async def test_delivery_setup_pins_choice_without_payment(
    db, registry, checkout_mock, monkeypatch, tmp_path
):
    import vpars_bot.wallet_browser as browser_module
    import vpars_bot.wallet_setup as setup_module

    await seed(db[1], registry, checkout_mock)
    monkeypatch.setattr(browser_module, "ROOT", tmp_path)
    path = browser_module.contract_path(101)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(contract(101)), encoding="utf-8")
    monkeypatch.setattr(
        setup_module, "Settings", lambda: SimpleNamespace(database_url=SecretStr("unused-test-db"))
    )
    monkeypatch.setattr(setup_module, "database", lambda _: db)
    monkeypatch.setattr(
        setup_module,
        "browser_adapter",
        lambda account, probe, interactive=False: checkout_mock.bridge.adapter(account),
    )
    monkeypatch.setattr("builtins.input", lambda _: "")
    await configure(checkout_mock, delivery="home")
    await setup_module.main_async(
        SimpleNamespace(
            command="delivery",
            user_id=101,
            url="https://www.vinted.de/items/123",
            price="10.00",
            enable=False,
        )
    )
    config, _ = browser_module.load_contract(101)
    assert "Demo Street 1" in config["expected_delivery"]
    assert not (await account(db[1])).enabled
    assert (await ledger(checkout_mock))["payment_attempts"] == 0
    await configure(checkout_mock, delivery="pickup-b")
    async with checkout_mock.bridge.adapter(await account(db[1])) as adapter:
        adapter.contract = config
        with pytest.raises(WalletError, match="DeliveryChanged"):
            await adapter.checkout(
                SimpleNamespace(
                    item_id="123",
                    url="https://www.vinted.de/items/123",
                )
            )


async def test_request_signal_occurs_after_commit(db, registry):
    service, _, _ = await setup(db[1], registry)
    signals(db[1]).wallet.clear()
    order = await service.request(101, 10)
    assert signals(db[1]).wallet.is_set()
    assert (await saved(db[1], order)).state == "requested"


@pytest.mark.integration
async def test_fast_worker_latency_report(pg_db, registry, checkout_mock):
    service, _ = await seed(pg_db[1], registry, checkout_mock)
    samples = []

    async def until_state(order, state):
        async with asyncio.timeout(10):
            while (await saved(pg_db[1], order)).state != state:
                await asyncio.sleep(0.005)

    async with pool_for(checkout_mock, pg_db[1]) as pool:
        worker = WalletWorker(pg_db[1], pool.adapter)
        task = asyncio.create_task(worker.run())
        try:
            for user_id, message_id in ((101, 10), (202, 12), (303, 10), (404, 12)):
                order = await service.request(user_id, message_id)
                await until_state(order, "quoted")
                started = perf_counter()
                await service.decide(user_id, order.id, pay=True)
                await until_state(order, "succeeded")
                samples.append(
                    {
                        "region": checkout_mock.site.accounts[str(user_id)].region,
                        "confirmation_to_pay_ms": round(
                            1000
                            * (checkout_mock.site.payment_received_times[str(user_id)] - started)
                        ),
                    }
                )
                assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 1
        finally:
            await stop(task)
    values = [row["confirmation_to_pay_ms"] for row in samples]
    report = {
        "scope": "Actual WalletService.decide + PostgreSQL + event-driven WalletWorker + "
        "WarmBrowserPool + BrowserCheckout + local HTTP mock; excludes Telegram/WAN.",
        "median_ms": median(values),
        "min_ms": min(values),
        "max_ms": max(values),
        "samples": samples,
    }
    path = Path(__file__).resolve().parents[3] / ".release-checks/autobuy-fast-timing.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
