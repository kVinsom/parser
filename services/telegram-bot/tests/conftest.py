import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import ChatFullInfo, Message, Update
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpars_bot.application import AdminService, Catalog
from vpars_bot.db import Base, Guard, Subscription, User, database, utcnow
from vpars_bot.domain import SourceRegistry, UserError
from vpars_bot.search_control import PersistentSearchControl
from vpars_bot.telegram import BotUI, make_dispatcher


def pytest_addoption(parser):
    parser.addoption(
        "--integration", action="store_true", help="Use local PostgreSQL and Redis test resources"
    )


@pytest.fixture(autouse=True)
def retained_wallet_implementation(request, monkeypatch):
    """The legacy wallet suite still exercises the parked implementation.

    Default-release behavior is covered separately in test_autobuy_soon and the
    normal menu/formatting/runtime tests, without enabling this test-only switch.
    No environment setting can turn Autobuy on in a running release.
    """
    if request.module.__name__ == "test_wallet" or request.module.__name__.startswith("test_wallet_"):
        from vpars_bot import autobuy_availability

        monkeypatch.setattr(autobuy_availability, "AVAILABLE", True)


@pytest.fixture(autouse=True)
def paid_accounts_for_existing_features(request, monkeypatch):
    """Existing search/UI suites exercise subscribed accounts under the new policy.

    Subscription and entitlement suites explicitly create missing/expired/tiered accounts.
    No entitlement checks are mocked.
    """
    if request.module.__name__ in {
        "test_entitlements",
        "test_subscriptions",
        "test_subscription_payments",
    }:
        return
    register = Catalog.register

    async def registered(self, user_id, *args, **kwargs):
        async with self.sessions() as session:
            existed = await session.get(User, user_id) is not None
        user = await register(self, user_id, *args, **kwargs)
        if not existed:
            async with self.sessions.begin() as session:
                session.add(
                    Subscription(
                        user_id=user_id,
                        level="ULTRA",
                        expires_at=utcnow() + timedelta(days=90),
                        granted_by=user_id,
                    )
                )
        return user

    monkeypatch.setattr(Catalog, "register", registered)


@pytest.fixture
def registry():
    registry = SourceRegistry.read(Path("config/sources.json"))
    registry.exchange.load({"date": datetime.now(UTC).date().isoformat(), "rates": {"GBP": "0.85"}})
    return registry


@pytest.fixture
async def db(tmp_path):
    engine, sessions = database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as session:
        session.add_all([Guard(name="admins"), Guard(name="profiles")])
    yield engine, sessions
    await engine.dispose()


@pytest.fixture
async def pg_db(request):
    if not request.config.getoption("--integration"):
        pytest.skip("Pass --integration to test PostgreSQL and Redis")
    url = os.environ.get(
        "TEST_POSTGRES_URL", "postgresql+asyncpg://vpars:vpars_local@127.0.0.1:15432/vpars_bot"
    )
    schema = "test_bot_" + uuid4().hex
    root = create_async_engine(url, hide_parameters=True)
    async with root.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, hide_parameters=True, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def migrate(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    async with engine.begin() as connection:
        await connection.run_sync(migrate)
    try:
        yield engine, sessions
    finally:
        await engine.dispose()
        async with root.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()


class VerifiedProfiles:
    def __init__(self):
        self.names = {}

    async def verify(self, user_id, username):
        if self.names.get(user_id) != username:
            raise UserError("Не вдалося перевірити отримувача.")


@pytest.fixture
def profiles():
    return VerifiedProfiles()


@pytest.fixture
def services(db, registry, profiles):
    _, sessions = db
    return (
        Catalog(sessions, registry),
        PersistentSearchControl(sessions, registry),
        AdminService(sessions, profiles),
    )


class OfflineSession(BaseSession):
    """Telegram transport double: no network and no real credentials."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.counter = 1000
        self.profiles = {}

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name in {
            "sendMessage",
            "editMessageText",
            "sendPhoto",
            "sendDocument",
            "sendRichMessage",
        }:
            self.counter += 1
            return Message(
                message_id=getattr(method, "message_id", None) or self.counter,
                date=datetime.now(UTC),
                chat={"id": method.chat_id, "type": "private"},
                text=getattr(method, "text", None),
                from_user={"id": bot.id, "is_bot": True, "first_name": "Bot"},
            )
        if name == "getChat":
            return ChatFullInfo(
                id=method.chat_id,
                type="private",
                accent_color_id=1,
                max_reaction_count=1,
                accepted_gift_types={
                    "unlimited_gifts": False,
                    "limited_gifts": False,
                    "unique_gifts": False,
                    "premium_subscription": False,
                    "gifts_from_channels": False,
                },
                username=self.profiles.get(method.chat_id),
                first_name="Recipient",
            )
        return True


class ChatHarness:
    def __init__(self, dp, bot, transport):
        self.dp, self.bot, self.transport = dp, bot, transport
        self.update_id = 0

    async def send(self, text, user_id=101, username="alice", update_id=None):
        self.update_id = update_id or self.update_id + 1
        msg = {
            "message_id": self.update_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Test <&>",
                "username": username,
            },
            "text": text,
        }
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.dp.feed_update(
            self.bot, Update.model_validate({"update_id": self.update_id, "message": msg})
        )

    async def click(self, label=None, *, data=None, user_id=101, username="alice"):
        if data is None:
            data = self.button(label, user_id=user_id)
        self.update_id += 1
        update = Update.model_validate(
            {
                "update_id": self.update_id,
                "callback_query": {
                    "id": str(self.update_id),
                    "chat_instance": "offline",
                    "data": data,
                    "from": {
                        "id": user_id,
                        "is_bot": False,
                        "first_name": "Test",
                        "username": username,
                    },
                    "message": {
                        "message_id": 1001,
                        "date": int(datetime.now(UTC).timestamp()),
                        "chat": {"id": user_id, "type": "private"},
                        "text": "Menu",
                    },
                },
            }
        )
        await self.dp.feed_update(self.bot, update)
        return data

    def panel(self, user_id=101):
        return next(
            m
            for m in reversed(self.transport.calls)
            if getattr(m, "chat_id", None) == user_id and getattr(m, "reply_markup", None)
        )

    def button(self, label, user_id=101):
        return next(
            b.callback_data
            for row in self.panel(user_id).reply_markup.inline_keyboard
            for b in row
            if label in b.text
        )

    async def state(self, user_id=101):
        return self.dp.fsm.get_context(bot=self.bot, chat_id=user_id, user_id=user_id)


@pytest.fixture
async def chat(services, registry):
    catalog, control, admins = services
    transport = OfflineSession()
    bot = Bot("42:offline", session=transport)
    storage = MemoryStorage()
    dp = make_dispatcher(
        storage, SimpleEventIsolation(), BotUI(catalog, control, admins, registry, development=True)
    )
    yield ChatHarness(dp, bot, transport)
    await storage.close()
    await bot.session.close()
