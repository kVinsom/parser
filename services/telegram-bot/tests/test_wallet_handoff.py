"""Human checkout ownership, regional isolation and restart/payment boundaries."""

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select
from test_wallet_regions import seed

from vpars_bot.config import Settings
from vpars_bot.db import WalletAccount, WalletOrder, WalletSession, utcnow
from vpars_bot.domain import UserError
from vpars_bot.wallet import REGIONS, WalletError, WalletService
from vpars_bot.wallet_handoff import HandoffService, telegram_owner
from vpars_bot.wallet_handoff_web import HandoffWindows, handoff_app, handoff_server
from vpars_bot.wallet_sessions import SessionService, check_checkout
from vpars_bot.wallet_worker import WalletWorker

TOKEN = "123:synthetic-test-token"


def init_data(uid=101, **changes):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid}), "query_id": "test"}
    fields.update(changes)
    key = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    value = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def setup(sessions, registry):
    await seed(sessions, registry)
    async with sessions.begin() as session:
        for uid in (101, 202):
            for region in REGIONS:
                session.add(WalletSession(user_id=uid, region=region, attempt_id=f"{uid}-{region}",
                    state="connected", account_key=str(uid), error="CheckoutChallenge"))

    async def resolver(uid, mid):
        if uid not in {101, 202} or mid not in range(8):
            raise UserError("Лот не знайдено серед надісланих тобі повідомлень.")
        region = list(REGIONS)[mid % 4]
        brand = "stone-island" if mid < 4 else "amiri"
        return {"item_url": f"https://{REGIONS[region][0]}/items/{1000 + mid}-{brand}",
                "price": {"amount": "10.00", "currency": REGIONS[region][1]}}
    async def delivery_resolver(uid, delivery_id):
        value = int(delivery_id, 16)
        owner, mid = divmod(value, 100)
        if owner != uid or mid not in range(8):
            raise UserError("Лот не знайдено серед надісланих тобі повідомлень.")
        return mid
    return HandoffService(sessions, "https://checkout.example", resolver=resolver,
                          delivery_resolver=delivery_resolver)


def delivery_reference(uid, mid):
    return f"{uid * 100 + mid:064x}"


@pytest.mark.parametrize("uid", [101, 202])
@pytest.mark.parametrize("mid", range(8))
async def test_direct_buy_auth_creates_owned_order_without_another_login(db, registry, uid, mid):
    service = await setup(db[1], registry)
    windows = HandoffWindows(service, factory=factory)
    async with TestClient(TestServer(handoff_app(service, TOKEN, windows=windows))) as client:
        path = "/buy/" + delivery_reference(uid, mid)
        headers = {"Origin": service.base_url}
        assert (await client.get(path)).status == 200
        # Opening/previewing a link has no order or browser side effects.
        async with db[1]() as session:
            assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
        other = 202 if uid == 101 else 101
        for payload in ({}, {"init_data": "invalid"}, {"init_data": init_data(other)}):
            assert (await client.post(path + "/auth", headers=headers, json=payload)).status == 403
        async with db[1]() as session:
            assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
        response = await client.post(path + "/auth", headers=headers,
                                     json={"init_data": init_data(uid), "user_id": other})
        assert response.status == 200
        data = await response.json()
        assert data["region"] == list(REGIONS)[mid % 4]
        assert data["stream_path"].startswith("/purchase/")
        again = await client.post(path + "/auth", headers=headers, json={"init_data": init_data(uid)})
        repeated = await again.json()
        assert repeated["stream_path"] == data["stream_path"]
        ws = await client.ws_connect(data["stream_path"], headers=headers)
        await ws.send_json({"ticket": repeated["ticket"]})
        assert (await ws.receive_json(timeout=5))["state"] == "checkout"
        assert (await ws.receive()).data == b"synthetic-image"
        await ws.close()
        for window in list(windows.windows.values()):
            await windows.close(window)
    async with db[1]() as session:
        orders = list(await session.scalars(select(WalletOrder)))
        assert len(orders) == 1 and orders[0].user_id == uid
        assert orders[0].state == "human_closed"


def test_telegram_signature_requires_trusted_owner_and_recent_unique_fields():
    assert telegram_owner(init_data(), TOKEN) == 101
    for raw in ("", init_data().replace("101", "202"), init_data() + "&user=other",
                init_data(auth_date=str(int(time.time()) - 301)),
                init_data(auth_date=str(int(time.time()) + 31)), init_data(uid=True),
                init_data(uid=-1), init_data(user='{"id":101,"is_bot":true}')):
        with pytest.raises(UserError):
            telegram_owner(raw, TOKEN)
    with pytest.raises(UserError):
        telegram_owner(init_data(), "wrong-token")


@pytest.mark.parametrize("url", ["http://example.com", "https://x@y", "https://x:8086", "https://x/p",
                                     "https://x?key=secret", "https://x#fragment"])
def test_public_endpoint_requires_https_origin(url):
    with pytest.raises(ValueError):
        Settings(_env_file=None, wallet_handoff_url=url)


async def test_two_users_four_regions_and_brands_keep_their_own_orders(db, registry):
    service = await setup(db[1], registry)
    for uid in (101, 202):
        for mid, region in enumerate(REGIONS):
            order = await service.request(uid, mid if uid == 101 else mid + 4)
            assert order.state == "human_waiting"
            assert f"{REGIONS[region][0]}/items/" in order.url
            assert (await service.request(uid, mid if uid == 101 else mid + 4)).id == order.id
            with pytest.raises(UserError):
                await service.authorize(202 if uid == 101 else 101, order.id)
            _, account, identity = await service.authorize(uid, order.id)
            assert (account.user_id, account.region, identity) == (uid, region, str(uid))
            assert not account.enabled and account.contract_hash is None
            assert service.message(order)["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"].endswith(order.id)
    async with db[1]() as session:
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 8


async def test_human_orders_never_enter_automatic_worker_or_old_pay_callback(db, registry):
    service = await setup(db[1], registry)
    order = await service.request(101, 0)
    worker = WalletWorker(db[1], None)
    assert await worker.claim() is None
    service.register()
    try:
        with pytest.raises(UserError, match="Оплату тепер"):
            await WalletService(db[1]).decide(101, order.id, pay=True)
        with pytest.raises(WalletError, match="CheckoutHumanReady"):
            await check_checkout(db[1], 101, "101", region="UK")
    finally:
        service.unregister()


async def test_manual_mode_without_public_endpoint_never_falls_back_to_automatic_pay(db, registry):
    await setup(db[1], registry)
    config = Settings(_env_file=None, wallet_checkout_mode="manual", wallet_handoff_url="")
    async with handoff_server(db[1], config) as service:
        assert service is not None
        with pytest.raises(UserError, match="зовнішнього доступу"):
            await service.request(101, 0)
        with pytest.raises(UserError, match="Оплату тепер"):
            await WalletService(db[1]).decide(101, "f" * 32, pay=True)
        with pytest.raises(WalletError, match="CheckoutHumanUnavailable"):
            await check_checkout(db[1], 101, "101", region="DE")
        assert "очікує" in await SessionService(db[1]).status(101, "DE")
    async with db[1]() as session:
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0


async def test_disabled_region_is_respected_and_resume_is_explicit(db, registry):
    service = await setup(db[1], registry)
    service.register()
    sessions = SessionService(db[1])
    try:
        await sessions.disable(101, "UK")
        with pytest.raises(UserError):
            await service.request(101, 0)
        assert (await sessions.actions(101, "UK"))["human"]
        assert (await service.request(202, 0)).state == "human_waiting"
        await sessions.resume_human(101, "UK")
        assert (await service.request(101, 0)).state == "human_waiting"
    finally:
        service.unregister()


async def test_restart_never_rebuilds_or_reports_success_for_exposed_checkout(db, registry):
    service = await setup(db[1], registry)
    waiting = await service.request(101, 0)
    active = await service.request(101, 1)
    await service.authorize(101, active.id, activate=True)
    await service.recover()
    async with db[1]() as session:
        assert (await session.get(WalletOrder, waiting.id)).state == "human_expired"
        assert (await session.get(WalletOrder, active.id)).state == "human_closed"
        assert all(a.active_order is None for a in await session.scalars(select(WalletAccount)))
    assert (await service.request(101, 0)).state == "human_waiting"
    assert (await service.request(101, 1)).state == "human_closed"


async def test_expiry_owner_and_active_binding_checked_before_input(db, registry):
    service = await setup(db[1], registry)
    order = await service.request(101, 0)
    async with db[1].begin() as session:
        (await session.get(WalletOrder, order.id)).expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(UserError):
        await service.authorize(101, order.id, activate=True)
    with pytest.raises(UserError):
        await service.request(101, 99)


class FakeBrowser:
    pointer_down = False

    def __init__(self):
        self.inputs = []
        self.closed = False

    async def frame(self):
        return "checkout", b"synthetic-image"

    async def input(self, event):
        self.inputs.append(event)

    async def prepare(self):
        raise AssertionError("No automatic retry")


@asynccontextmanager
async def factory(order, account, identity):
    browser = FakeBrowser()
    try:
        yield browser
    finally:
        browser.closed = True


async def test_one_window_reconnects_without_recreating_browser_and_close_does_not_release_pointer(db, registry):
    service = await setup(db[1], registry)
    order = await service.request(101, 0)
    windows = HandoffWindows(service, factory=factory)
    first = await windows.attach(101, order.id)
    with pytest.raises(UserError):
        await windows.attach(101, order.id)
    await windows.detach(first)
    assert await windows.attach(101, order.id) is first
    first.browser.pointer_down = True
    await windows.detach(first)
    assert first.browser.closed and first.browser.inputs == []
    with pytest.raises(UserError):
        await windows.attach(101, order.id)


async def test_http_and_websocket_reject_foreign_owner_origin_replayed_ticket_and_input(db, registry):
    service = await setup(db[1], registry)
    order = await service.request(101, 0)
    windows = HandoffWindows(service, factory=factory)
    async with TestClient(TestServer(handoff_app(service, TOKEN, windows=windows))) as client:
        path = f"/purchase/{order.id}"
        for headers, data in [({}, init_data()), ({"Origin": "https://evil.example"}, init_data()),
                              ({"Origin": service.base_url}, init_data(202))]:
            response = await client.post(path + "/auth", headers=headers, json={"init_data": data})
            assert response.status == 403
        headers = {"Origin": service.base_url}
        response = await client.post(path + "/auth", headers=headers, json={"init_data": init_data()})
        ticket = (await response.json())["ticket"]
        assert response.headers["Cache-Control"] == "no-store"
        ws = await client.ws_connect(path + "/stream", headers=headers)
        await ws.send_json({"ticket": ticket})
        frame = await ws.receive_json(timeout=5)
        assert frame["state"] == "checkout"
        assert (await ws.receive()).data == b"synthetic-image"
        window = windows.windows[order.id]
        await ws.send_json({"type": "key", "value": "Enter", "seq": 1, "frame": frame["frame"]})
        for _ in range(20):
            if window.browser.inputs:
                break
            await asyncio.sleep(0.02)
        assert len(window.browser.inputs) == 1
        await ws.send_json({"type": "key", "value": "Enter", "seq": 1, "frame": frame["frame"]})
        await asyncio.sleep(0.05)
        assert len(window.browser.inputs) == 1
        await ws.close()
        again = await client.ws_connect(path + "/stream", headers=headers)
        await again.send_json({"ticket": ticket})
        assert (await again.receive_json(timeout=5))["type"] == "error"
        await again.close()
        await windows.close(window)


@pytest.mark.integration
async def test_double_click_and_four_region_requests_are_serialized_in_postgres(pg_db, registry):
    service = await setup(pg_db[1], registry)
    a, b = await asyncio.gather(service.request(101, 0), service.request(101, 0))
    assert a.id == b.id
    others = await asyncio.gather(*(service.request(101, mid) for mid in (1, 2, 3)))
    assert len({o.id for o in [a, *others]}) == 4
