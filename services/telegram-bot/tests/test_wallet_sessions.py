import base64
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_latency import incoming
from test_reply_menu import menu as menu
from test_wallet_mock import checkout_mock as checkout_mock

from vpars_bot.application import Catalog
from vpars_bot.db import Inbox, Notification, WalletAccount, WalletOrder, utcnow
from vpars_bot.domain import UserError
from vpars_bot.profile_menu import CONNECT_AUTOBUY, FAQ
from vpars_bot.runtime import UpdateInbox
from vpars_bot.session_secrets import (
    SESSION_INPUT,
    SessionCipher,
    dpapi,
    incoming_session_key,
    protected_payload,
)
from vpars_bot.wallet import WalletError
from vpars_bot.wallet_accounts import wallet_account, wallet_session
from vpars_bot.wallet_onboarding import COUNTRIES
from vpars_bot.wallet_sessions import SessionService, SessionWorker, validate_key, verify_session


def key(region="DE", purpose="refresh", subject="123", expiry=None):
    def encoded(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return ".".join(
        [
            encoded({"alg": "PS256", "kid": "synthetic-test-key"}),
            encoded(
                {
                    "iss": "vinted-iam-service",
                    "purpose": purpose,
                    "client_id": "web",
                    "sub": subject,
                    "cc": region,
                    "exp": expiry or int(time.time()) + 3600,
                }
            ),
            "synthetic_signature_not_valid_on_vinted_" * 2,
        ]
    )


@pytest.fixture(autouse=True)
def cipher(monkeypatch):
    value = SessionCipher(Fernet.generate_key())
    for module in ("session_secrets", "wallet_sessions", "runtime"):
        monkeypatch.setattr(f"vpars_bot.{module}.session_cipher", lambda: value)
    return value


@pytest.mark.skipif(os.name != "nt", reason="Windows service key protection")
def test_windows_master_key_roundtrip():
    value = Fernet.generate_key()
    wrapped = dpapi(value)
    assert value not in wrapped and dpapi(wrapped, decrypt=True) == value


def test_ingress_redaction_encryption_and_owner_binding(cipher):
    secret = key()
    update = incoming(1, text=secret)
    payload = protected_payload(update, 42)
    assert secret not in json.dumps(payload)
    assert payload["message"]["text"] == SESSION_INPUT
    assert cipher.open(payload["_wallet_secret"], owner=101, purpose="telegram:42:1") == secret
    with pytest.raises(RuntimeError, match="SessionSecretUnavailable"):
        cipher.open(payload["_wallet_secret"], owner=202, purpose="telegram:42:1")
    with pytest.raises(RuntimeError):
        cipher.open(payload["_wallet_secret"], owner=101, purpose="telegram:42:2")
    callback = protected_payload(incoming(2, text=secret, action="old:start"), 42)
    assert secret not in json.dumps(callback) and "_wallet_secret" not in callback


@pytest.mark.parametrize(
    "token,region,error",
    [
        (key("DE"), "US", "UnsupportedCountry"),
        (key(purpose="access"), "DE", "WrongKeyType"),
        (key(expiry=1), "DE", "ExpiredKey"),
        ("not-a-key", "DE", "InvalidKey"),
    ],
)
def test_claims_only_reject_never_authenticate(token, region, error):
    with pytest.raises(WalletError, match=error):
        validate_key(token, region)


async def seed(sessions, registry, region="DE", user=101):
    await Catalog(sessions, registry).register(user, user, f"user{user}", "Test", 1, start=True)
    async with sessions.begin() as session:
        session.add(
            WalletAccount(
                user_id=user,
                region=region,
                currency="GBP" if region == "UK" else "EUR",
                max_total=10000,
                enabled=False,
            )
        )


@pytest.mark.parametrize("region", ["DE", "FR", "IT", "UK"])
async def test_session_connection_rotates_encrypts_and_never_enables_without_checkout(
    db, registry, cipher, region
):
    await seed(db[1], registry, region)
    await seed(db[1], registry, region, user=202)
    service = SessionService(db[1], cipher)
    secret = key("GB" if region == "UK" else region)
    await service.submit(101, secret, "first")
    await service.submit(101, secret, "first")
    async with db[1]() as session:
        row = await wallet_session(session, 101)
        assert secret not in row.secret and row.state == "pending"
        assert await wallet_session(session, 202) is None
    rotated = key(region, purpose="access")

    async def verifier(country, value, persist):
        assert country == region and value == secret
        await persist([{"name": "access_token_web", "value": rotated}])
        return "123", utcnow() + timedelta(hours=1)

    async def unavailable(*args):
        raise WalletError("CheckoutUnavailable")

    installer = AsyncMock()
    worker = SessionWorker(
        db[1], verifier=verifier, cipher=cipher, installer=installer, checkout=unavailable
    )
    assert await worker.once()
    assert not await worker.once()  # Never automatically replay a consumed refresh key.
    installer.assert_awaited_once()
    async with db[1]() as session:
        account = await wallet_account(session, 101)
        row = await wallet_session(session, 101)
        assert not account.enabled and account.contract_hash is None
        assert row.state == "connected" and row.error == "CheckoutUnavailable"
        assert rotated not in row.secret and secret not in row.secret
        stored = cipher.open(row.secret, owner=101, purpose=f"vinted-cookies:{row.attempt_id}")
        assert stored[0]["value"] == rotated
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
        notices = list(await session.scalars(select(Notification)))
        assert len(notices) == 1 and secret not in str(notices[0].payload)
    assert "не підтримується" in await service.status(101)


async def test_only_verified_checkout_can_be_activated_and_changed_config_is_rejected(
    db, registry, cipher, monkeypatch
):
    await seed(db[1], registry)
    service = SessionService(db[1], cipher)
    await service.submit(101, key(), "connect")

    async def verifier(region, value, persist):
        await persist([])
        return "123", utcnow() + timedelta(hours=1)

    worker = SessionWorker(
        db[1],
        verifier=verifier,
        cipher=cipher,
        installer=AsyncMock(),
        checkout=AsyncMock(return_value="digest"),
    )
    await worker.once()
    async with db[1]() as session:
        assert (await wallet_session(session, 101)).state == "checkout_ready"
        assert not (await wallet_account(session, 101)).enabled
    monkeypatch.setattr(
        "vpars_bot.wallet_browser.load_contract",
        lambda uid, region=None: (
            {"region": "DE", "expected_account": "123", "expected_delivery": "pickup-a"},
            "changed",
        ),
    )
    with pytest.raises(UserError, match="повторна перевірка"):
        await service.enable(101)
    monkeypatch.setattr(
        "vpars_bot.wallet_browser.load_contract",
        lambda uid, region=None: (
            {"region": "DE", "expected_account": "123", "expected_delivery": "pickup-a"},
            "digest",
        ),
    )
    await service.enable(101)
    async with db[1]() as session:
        assert (await wallet_account(session, 101)).enabled
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0


async def test_invalid_or_interrupted_session_cannot_activate_or_retry(db, registry, cipher):
    await seed(db[1], registry)
    service = SessionService(db[1], cipher)
    with pytest.raises(UserError, match="не refresh_token_web"):
        await service.submit(101, key(purpose="access"), "access")
    await service.submit(101, key(), "valid")
    async with db[1].begin() as session:
        (await wallet_session(session, 101)).state = "checking"
    worker = SessionWorker(db[1], verifier=AsyncMock(), cipher=cipher, installer=AsyncMock())
    await worker.recover()
    assert not await worker.once()
    with pytest.raises(UserError):
        await service.enable(101)
    async with db[1]() as session:
        row = await wallet_session(session, 101)
        assert row.error == "Interrupted" and row.secret is None
    worker.verifier.assert_not_awaited()


async def test_wizard_accepts_key_only_at_its_step_deletes_message_and_never_stores_it_in_state(
    menu, services
):
    await menu.send("/start")
    secret = key()
    await menu.send(secret)
    assert "Підключити автобай" in menu.transport.calls[-1].text
    await menu.send(FAQ)
    await menu.send(CONNECT_AUTOBUY)
    await menu.send(COUNTRIES["DE"])
    await menu.send("100")
    await menu.send(secret)
    assert "Перевіряю ключ" in menu.panel().text
    assert secret not in json.dumps(await (await menu.state()).get_data())
    assert any(m.__api_method__ == "deleteMessage" for m in menu.transport.calls)
    assert all(secret not in str(m) for m in menu.transport.calls)
    async with services[0].sessions() as session:
        assert (await wallet_session(session, 101)).state == "pending"


@pytest.mark.integration
async def test_durable_inbox_stores_ciphertext_delivers_key_in_memory_and_clears_payload(
    pg_db, cipher
):
    inbox = UpdateInbox(pg_db[1], 42)
    secret = key()
    await inbox.receive(incoming(1, text=secret))
    async with pg_db[1]() as session:
        row = await session.get(Inbox, (42, 1))
        assert secret not in json.dumps(row.payload)

    class Dispatcher:
        async def feed_update(self, bot, update):
            assert update.message.text == SESSION_INPUT
            assert incoming_session_key.get() == secret

    await inbox.once(Dispatcher(), None)
    assert incoming_session_key.get() is None
    async with pg_db[1]() as session:
        row = await session.get(Inbox, (42, 1))
        assert row.state == "processed" and row.payload is None


@pytest.mark.parametrize(
    "status,country,subject,error",
    [
        (200, "DE", "123", None),
        (400, "DE", "123", "RejectedKey"),
        (403, "DE", "123", "SessionChallenge"),
        (200, "US", "123", None),
        (200, "DE", "999", "RejectedKey"),
    ],
)
async def test_real_verifier_requires_successful_origin_response_and_matching_identity(
    monkeypatch, status, country, subject, error
):
    class Page:
        def __init__(self):
            self.handlers = {}

        async def route(self, *args):
            pass

        def on(self, event, callback):
            self.handlers[event] = callback

        async def goto(self, *args, **kwargs):
            request = SimpleNamespace(method="POST")
            self.handlers["response"](
                SimpleNamespace(
                    url="https://www.vinted.de/web/api/auth/refresh",
                    status=status,
                    request=request,
                    body=AsyncMock(return_value=b"{}"),
                )
            )

    context = SimpleNamespace(
        add_cookies=AsyncMock(),
        new_page=AsyncMock(return_value=Page()),
        close=AsyncMock(),
        cookies=AsyncMock(
            return_value=[{"name": "access_token_web", "value": key(country, "access", subject)},
                          {"name": "refresh_token_web", "value": key(country, subject=subject)}]
        ),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())

    @asynccontextmanager
    async def driver():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    monkeypatch.setattr("playwright.async_api.async_playwright", driver)
    persist = AsyncMock()
    if error:
        with pytest.raises(WalletError, match=error):
            await verify_session("DE", key(), persist)
    else:
        identity, expiry = await verify_session("DE", key(), persist)
        assert identity == "123" and expiry > datetime.now(UTC)
    if status == 200:
        persist.assert_awaited_once()
    else:
        persist.assert_not_awaited()
    context.close.assert_awaited_once()


@pytest.mark.parametrize("user_id", [101, 202, 303, 404])
@pytest.mark.parametrize("automatic", [True, False])
async def test_autonomous_connection_to_real_mock_checkout_enables_only_after_verification(
    db, registry, cipher, checkout_mock, monkeypatch, tmp_path, user_id, automatic
):
    from test_wallet_fast import contract
    from test_wallet_mock import ledger
    from test_wallet_mock import seed as seed_mock

    from vpars_bot import wallet_browser
    from vpars_bot.wallet_sessions import check_checkout

    wallet, purchase_worker = await seed_mock(db[1], registry, checkout_mock)
    region = checkout_mock.site.accounts[str(user_id)].region
    monkeypatch.setattr(wallet_browser, "ROOT", tmp_path)
    path = wallet_browser.contract_path(user_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(contract(user_id, region)), encoding="utf-8")
    monkeypatch.setattr(wallet_browser, "browser_adapter", lambda acc, probe: checkout_mock.bridge.adapter(acc))

    async def verifier(region, token, persist):
        await persist([])
        return f"mock-{user_id}", utcnow() + timedelta(hours=1)

    async def candidates(sessions, owner, country):
        return [await wallet.resolver(owner, 10), await wallet.resolver(owner, 12)]

    async def checkout(sessions, owner, identity, *, region):
        return await check_checkout(sessions, owner, identity, region=region, candidates=candidates)

    service = SessionService(db[1], cipher)
    await service.submit(user_id, key(region), "own-key", region=region, auto_enable=automatic)
    worker = SessionWorker(db[1], verifier=verifier, cipher=cipher,
                           installer=AsyncMock(), checkout=checkout)
    await worker.once()
    assert (await service.actions(user_id, region))["enable"] is not automatic
    async with db[1]() as session:
        assert (await wallet_session(session, user_id)).state == "checkout_ready"
        assert (await wallet_account(session, user_id)).enabled is automatic
    assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 0
    if not automatic:
        await service.enable(user_id, region)
    await wallet.request(user_id, 10)
    await purchase_worker.once()
    assert (await ledger(checkout_mock, user_id))["payment_attempts"] == 0


async def test_settings_change_during_verification_blocks_activation(db, registry, cipher):
    await seed(db[1], registry)
    service = SessionService(db[1], cipher)
    await service.submit(101, key(), "own-key")

    async def verifier(region, token, persist):
        await persist([])
        async with db[1].begin() as session:
            (await wallet_account(session, 101)).max_total = 500
        return "123", utcnow() + timedelta(hours=1)

    worker = SessionWorker(db[1], verifier=verifier, cipher=cipher,
                           installer=AsyncMock(), checkout=AsyncMock(return_value="digest"))
    await worker.once()
    async with db[1]() as session:
        row = await wallet_session(session, 101)
        assert row.state == "failed" and row.error == "SettingsChanged"
        assert row.secret is None
        assert not (await wallet_account(session, 101)).enabled
