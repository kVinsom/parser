import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT),
    str(ROOT.parent / "telegram-bot"),
    str(ROOT.parent / "parser"),
    str(ROOT.parent / "notifier"),
]

from vpars_bot.application import Catalog  # noqa: E402
from vpars_bot.db import Base as BotBase  # noqa: E402
from vpars_bot.db import Guard, Subscription, User, utcnow  # noqa: E402
from vpars_bot.domain import SourceRegistry  # noqa: E402

from vpars_worker.db import SCHEMA, Base  # noqa: E402
from vpars_worker.monitor import Store  # noqa: E402


@pytest.fixture(autouse=True)
def subscribed_accounts_for_monitoring(monkeypatch):
    """Monitoring fixtures now require an active plan; do not bypass policy checks."""
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
async def environment():
    url = os.environ.get(
        "TEST_POSTGRES_URL", "postgresql+asyncpg://vpars:vpars_local@127.0.0.1:15432/vpars_bot"
    )
    schema = "test_monitor_" + uuid4().hex
    root = create_async_engine(url, hide_parameters=True)
    async with root.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema}},
        execution_options={"schema_translate_map": {SCHEMA: schema}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(BotBase.metadata.create_all)
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as session:
        session.add_all([Guard(name="admins"), Guard(name="profiles")])
    registry = SourceRegistry.read(ROOT.parent / "telegram-bot/config/sources.json")
    registry.exchange.load({"date": datetime.now(UTC).date().isoformat(), "rates": {"GBP": "0.85"}})
    catalog = Catalog(sessions, registry)
    await catalog.register(101, 101, "alice", "Test", 1, start=True)
    try:
        yield Store(sessions, registry), catalog
    finally:
        await engine.dispose()
        async with root.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()
