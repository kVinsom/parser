
"""WalletService -> database -> WalletWorker -> Chromium -> real loopback HTTP server.

Only delivery-journal inputs and Telegram transport are synthetic. Checkout/Pay,
confirmation, entitlements and recovery use production code, not a fake adapter.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiohttp import ClientSession
from conftest import OfflineSession
from sqlalchemy import func, select

from tools.autobuy_mock import DELIVERIES, MockBrowser, MockCheckout, serve
from vpars_bot.application import Catalog
from vpars_bot.db import WalletAccount, WalletOrder, utcnow
from vpars_bot.domain import UserError
from vpars_bot.notifications import NotificationSender
from vpars_bot.wallet import REGIONS, WalletService
from vpars_bot.wallet_accounts import wallet_account
from vpars_bot.wallet_worker import WalletWorker


@pytest.fixture
async def checkout_mock():
    from playwright.async_api import async_playwright

    mock = MockCheckout()
    async with serve(mock) as endpoint, async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            async with ClientSession(base_url=endpoint) as http:
                yield SimpleNamespace(
                    site=mock, http=http, endpoint=endpoint, bridge=MockBrowser(browser, endpoint)
                )
        finally:
            await browser.close()


async def configure(mock, user_id=101, **changes):
    async with mock.http.post(f"/api/config?account={user_id}", json=changes) as response:
        assert response.status == 200, await response.text()


async def ledger(mock, user_id=101):
    async with mock.http.get(f"/api/state?account={user_id}") as response:
        assert response.status == 200
        return await response.json()


async def seed(sessions, registry, mock):
    catalog = Catalog(sessions, registry)
    for key, account in mock.site.accounts.items():
        user_id = int(key)
        await catalog.register(user_id, user_id, f"mock{key}", "Test", user_id, start=True)
        async with sessions.begin() as session:
            session.add(
                WalletAccount(
                    user_id=user_id,
                    region=account.region,
                    currency=REGIONS[account.region][1],
                    max_total=2000,
                    enabled=True,
                    contract_hash="a" * 64,
                )
            )

    async def delivered_item(user_id, message_id):
        # Two filters may have sent the same item. Buy still resolves ownership.
        assert message_id in {10, 11, 12}
        region = mock.site.accounts[str(user_id)].region
        item_id = "456" if message_id == 12 else "123"
        return {
            "item_url": f"https://{REGIONS[region][0]}/items/{item_id}",
            "price": {
                "amount": "12.00" if item_id == "456" else "10.00",
                "currency": REGIONS[region][1],
            },
        }

    return WalletService(sessions, delivered_item), WalletWorker(sessions, mock.bridge.adapter)


async def saved(sessions, order):
    async with sessions() as session:
        return await session.get(WalletOrder, order.id)


@pytest.mark.parametrize("user_id", [101, 202, 303, 404])
@pytest.mark.parametrize("message_id,total", [(10, 1350), (12, 1550)])
async def test_http_purchase_all_regions_brands_and_notifications(
    db,
    registry,
    checkout_mock,
    user_id,
    message_id,
    total,
):
    sessions = db[1]
    service, worker = await seed(sessions, registry, checkout_mock)
    await configure(checkout_mock, user_id, delivery="home")
    order = await service.request(user_id, message_id)
    assert await worker.once()
    quote = await saved(sessions, order)
    assert quote.state == "quoted"
    assert quote.quote["delivery_key"] == DELIVERIES["home"]
    assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 0
    transport = OfflineSession()
    async with Bot("42:offline", session=transport) as bot:
        sender = NotificationSender(sessions, bot)
        assert await sender.once()
        message = transport.calls[-1]
        assert message.chat_id == user_id
        assert f"{total / 100:.2f}" in message.text
        assert message.reply_markup.inline_keyboard[0][0].callback_data == f"wallet:pay:{order.id}"
        await service.decide(user_id, order.id, pay=True)
        await service.decide(user_id, order.id, pay=True)
        assert await worker.once()
        result = await saved(sessions, order)
        assert result.state == "succeeded"
        assert result.external_order_id == f"mock-{user_id}-{order.item_id}"
        assert await sender.once()
        assert "Покупку оформлено" in transport.calls[-1].text
        assert not await sender.once()
    state = await ledger(checkout_mock, user_id)
    assert state["balance"] == 5000 - total
    assert state["payment_attempts"] == state["debits"] == 1
    assert state["orders"][0]["total"] == total
    assert state["orders"][0]["delivery"] == DELIVERIES["home"]
    assert ("GET", "/checkout") in checkout_mock.site.requests
    assert ("POST", "/api/pay") in checkout_mock.site.requests
    await worker.recover()
    assert not await worker.once()
    assert (await service.request(user_id, message_id)).id == order.id
    if message_id == 10:
        assert (await service.request(user_id, 11)).id == order.id
    assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 1


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"balance": 100}, "InsufficientBalance"),
        ({"shipping": 1500}, "BudgetExceeded"),
        ({"external_charge": 500}, "WalletOnly"),
        ({"payment_method": "Card"}, "WalletOnly"),
        ({"delivery": None}, "ManualAction"),
        ({"scenario": "sold"}, "ContractMismatch"),
        ({"scenario": "ambiguous"}, "ContractMismatch"),
    ],
)
async def test_http_unsafe_checkout_never_sends_payment(
    db, registry, checkout_mock, changes, error
):
    service, worker = await seed(db[1], registry, checkout_mock)
    await configure(checkout_mock, **changes)
    order = await service.request(101, 10)
    await worker.once()
    result = await saved(db[1], order)
    assert (result.state, result.error) == ("failed", error)
    assert (await ledger(checkout_mock))["payment_attempts"] == 0
    # A failed checkout does not starve another account or another brand.
    other = await service.request(202, 12)
    await worker.once()
    assert (await saved(db[1], other)).state == "quoted"


@pytest.mark.parametrize(
    "changes",
    [
        {"delivery": "pickup-b"},
        {"shipping": 300},
        {"balance": 0},
    ],
)
async def test_http_change_after_confirmation_blocks_charge(db, registry, checkout_mock, changes):
    service, worker = await seed(db[1], registry, checkout_mock)
    order = await service.request(101, 10)
    await worker.once()
    await service.decide(101, order.id, pay=True)
    await configure(checkout_mock, **changes)
    await worker.once()
    assert (await saved(db[1], order)).state == "failed"
    assert (await ledger(checkout_mock))["payment_attempts"] == 0


@pytest.mark.parametrize(
    "scenario,debits", [("lost_response", 1), ("wrong_item", 1), ("decline", 0)]
)
async def test_http_uncertain_response_never_retries(db, registry, checkout_mock, scenario, debits):
    service, worker = await seed(db[1], registry, checkout_mock)
    await configure(checkout_mock, scenario=scenario)
    order = await service.request(101, 10)
    await worker.once()
    await service.decide(101, order.id, pay=True)
    await worker.once()
    result = await saved(db[1], order)
    assert result.state == "unknown"
    for _ in range(2):
        await worker.recover()
        assert not await worker.once()
        assert (await service.decide(101, order.id, pay=True)).state == "unknown"
    state = await ledger(checkout_mock)
    assert state["payment_attempts"] == 1 and state["debits"] == debits
    async with db[1]() as session:
        assert (await wallet_account(session, 101)).active_order == order.id


@pytest.mark.parametrize("action", ["cancel", "expire", "wrong_user"])
async def test_http_requires_current_owner_confirmation(db, registry, checkout_mock, action):
    service, worker = await seed(db[1], registry, checkout_mock)
    order = await service.request(101, 10)
    await worker.once()
    if action == "cancel":
        await service.decide(101, order.id, pay=False)
    elif action == "expire":
        async with db[1].begin() as session:
            (await session.get(WalletOrder, order.id)).expires_at = utcnow() - timedelta(seconds=1)
        await service.decide(101, order.id, pay=True)
    else:
        with pytest.raises(UserError, match="не знайдено"):
            await service.decide(202, order.id, pay=True)
    assert not await worker.once()
    assert (await ledger(checkout_mock))["payment_attempts"] == 0


@pytest.mark.integration
async def test_postgres_concurrent_filters_one_browser_payment(pg_db, registry, checkout_mock):
    sessions = pg_db[1]
    service, worker = await seed(sessions, registry, checkout_mock)
    first, second = await asyncio.gather(service.request(101, 10), service.request(101, 11))
    assert first.id == second.id
    await worker.once()
    await asyncio.gather(
        service.decide(101, first.id, pay=True), service.decide(101, first.id, pay=True)
    )
    await worker.once()
    assert (await saved(sessions, first)).state == "succeeded"
    state = await ledger(checkout_mock)
    assert state["payment_attempts"] == state["debits"] == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 1


async def test_mock_transport_cannot_fall_back_to_external_network(checkout_mock):
    account = SimpleNamespace(user_id=101, region="DE", currency="EUR", max_total=2000)
    async with checkout_mock.bridge.adapter(account) as adapter:
        from playwright.async_api import Error

        with pytest.raises(Error):
            await adapter.page.goto("https://external.invalid/")
        assert checkout_mock.bridge.blocked == ["https://external.invalid/"]
    for endpoint in (
        "https://www.vinted.de",
        "http://localhost:8766",
        "http://127.0.0.1:8766@external.invalid",
        "http://127.0.0.1:8766/path",
    ):
        with pytest.raises(ValueError):
            MockBrowser(checkout_mock.bridge.browser, endpoint)


async def test_mock_delivery_ui_preserves_selection(checkout_mock):
    await configure(checkout_mock, delivery="pickup-b")
    async with checkout_mock.http.get("/") as response:
        html = await response.text()
    assert 'value="pickup-b" selected' in html
    assert DELIVERIES["pickup-b"] in html
    await configure(checkout_mock, delivery=None)
    assert (await ledger(checkout_mock))["delivery"] is None
    async with checkout_mock.http.get("/") as response:
        assert 'value="" selected' in await response.text()
