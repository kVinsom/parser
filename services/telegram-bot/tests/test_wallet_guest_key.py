"""A complete anonymous refresh key is not a signed-in user's session."""

import base64
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_reply_menu import menu as menu
from test_wallet_regions import seed
from test_wallet_sessions import key

from vpars_bot.db import WalletAccount, WalletOrder, WalletSession, utcnow
from vpars_bot.domain import UserError
from vpars_bot.profile_menu import CONNECT_AUTOBUY, FAQ
from vpars_bot.session_secrets import SessionCipher
from vpars_bot.wallet import REGIONS, WalletError
from vpars_bot.wallet_onboarding import COUNTRIES
from vpars_bot.wallet_sessions import SESSION_ERRORS, SessionService, SessionWorker, validate_key


def guest_key(*, absent=True, **changes):
    parts = key().split(".")
    data = json.loads(base64.urlsafe_b64decode(parts[1] + "==="))
    data.update(iss="vinted-iam-oauth", scope="public", sub=None)
    if absent:
        data.pop("sub")
    data.pop("cc")
    data.update(changes)
    parts[1] = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return ".".join(parts)


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("absent", [True, False])
def test_complete_guest_key_has_specific_login_error(region, absent):
    with pytest.raises(WalletError, match="^GuestKey$"):
        validate_key(guest_key(absent=absent), region)


@pytest.mark.parametrize("changes,error", [
    ({"purpose": "access"}, "WrongKeyType"),
    ({"client_id": "mobile"}, "WrongKeyType"),
    ({"exp": 1}, "ExpiredKey"),
    ({"sub": 123}, "InvalidKey"),
    ({"sub": "not-numeric"}, "InvalidKey"),
    ({"iss": "unknown"}, "InvalidKey"),
])
def test_guest_detection_does_not_mask_other_invalid_input(changes, error):
    with pytest.raises(WalletError, match=f"^{error}$"):
        validate_key(guest_key(**changes), "DE")


@pytest.mark.parametrize("user_id", [101, 202])
@pytest.mark.parametrize("region", list(REGIONS))
async def test_guest_input_stays_at_login_and_never_queues_purchase(menu, services, user_id, region):
    for label in ("/start", FAQ, CONNECT_AUTOBUY, COUNTRIES[region], "100"):
        await menu.send(label, user_id=user_id)
    instructions = menu.panel(user_id).text
    assert "власний профіль" in instructions
    assert "Країна сайту не змінює країну акаунту" in instructions
    secret = guest_key()
    await menu.send(secret, user_id=user_id)
    assert SESSION_ERRORS["GuestKey"] in menu.transport.calls[-1].text
    assert "повне значення" not in menu.transport.calls[-1].text
    state = await (await menu.state(user_id)).get_data()
    assert state["autobuy_draft"]["step"] == "login"
    assert state["autobuy_draft"]["region"] == region
    assert secret not in json.dumps(state)
    assert any(call.__api_method__ == "deleteMessage" for call in menu.transport.calls)
    assert all(secret not in str(call) for call in menu.transport.calls)
    async with services[0].sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WalletSession)) == 0
        assert await session.scalar(select(func.count()).select_from(WalletOrder)) == 0
        assert not (await session.get(WalletAccount, (user_id, region))).enabled


async def test_guest_reconnect_preserves_all_existing_verified_sessions(db, registry):
    sessions = db[1]
    await seed(sessions, registry)
    service = SessionService(sessions, SessionCipher(Fernet.generate_key()))

    async def verified(region, value, persist):
        await persist([])
        return validate_key(value, region)["sub"], utcnow() + timedelta(hours=1)

    worker = SessionWorker(sessions, verified, service.cipher, AsyncMock(),
                           AsyncMock(return_value="verified-digest"))
    for uid in (101, 202):
        for region in REGIONS:
            await service.submit(uid, key(subject=str(uid)), f"connect-{uid}-{region}",
                                 region=region, auto_enable=True)
            assert await worker.once()

    async def snapshot():
        async with sessions() as session:
            return {
                "sessions": [(row.user_id, row.region, row.attempt_id, row.state, row.secret,
                              row.account_key, row.error, row.expires_at)
                             for row in await session.scalars(
                                 select(WalletSession).order_by(WalletSession.user_id, WalletSession.region))],
                "accounts": [(row.user_id, row.region, row.enabled, row.max_total, row.contract_hash)
                             for row in await session.scalars(
                                 select(WalletAccount).order_by(WalletAccount.user_id, WalletAccount.region))],
            }

    before = await snapshot()
    for uid in (101, 202):
        for region in REGIONS:
            with pytest.raises(UserError, match="гостьової сесії"):
                await service.submit(uid, guest_key(), f"guest-{uid}-{region}",
                                     region=region, auto_enable=True)
    assert await snapshot() == before
    assert not await worker.once()
