"""Restart a cleaned-up service after a temporary dependency outage."""

import asyncio
import logging

from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from asyncpg import CannotConnectNowError, PostgresConnectionError, TooManyConnectionsError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import DBAPIError, PendingRollbackError

TRANSIENT = (
    OSError,
    TimeoutError,
    RedisConnectionError,
    RedisTimeoutError,
    DBAPIError,
    PendingRollbackError,
    TelegramNetworkError,
    TelegramServerError,
    PostgresConnectionError,
    CannotConnectNowError,
    TooManyConnectionsError,
)


def retryable(error):
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(retryable(e) for e in error.exceptions)
    return isinstance(error, TRANSIENT)


async def supervise(service, *, sleep=asyncio.sleep):
    """Never restart on cancellation, invalid configuration or duplicate ownership."""
    delay = 5
    while True:
        try:
            return await service()
        except Exception as error:
            if not retryable(error):
                raise
            # Exception text can contain credentials: log only the safe class name.
            logging.getLogger("vpars.supervisor").warning(
                "Service unavailable (%s); retrying in %s seconds", type(error).__name__, delay
            )
            await sleep(delay)
            delay = min(30, delay * 2)
