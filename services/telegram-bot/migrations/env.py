import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from vpars_bot.config import Settings
from vpars_bot.db import Base

config = context.config
url = config.attributes.get("database_url") or Settings().database_url.get_secret_value()


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(url, hide_parameters=True)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if config.attributes.get("connection") is not None:
    migrate(config.attributes["connection"])
elif context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
