import asyncio

import pytest
from asyncpg import CannotConnectNowError, ConnectionDoesNotExistError, TooManyConnectionsError
from redis.exceptions import ConnectionError as RedisConnectionError

from vpars_bot.supervision import supervise


@pytest.mark.parametrize(
    "kind", [CannotConnectNowError, ConnectionDoesNotExistError, TooManyConnectionsError]
)
async def test_database_restart_before_connection_is_wrapped_recovers(kind):
    attempts, delays = [], []

    async def service():
        attempts.append(1)
        if len(attempts) == 1:
            raise ExceptionGroup("outage", [kind("private connection details")])
        return "restored"

    async def sleep(delay):
        delays.append(delay)

    assert await supervise(service, sleep=sleep) == "restored"
    assert len(attempts) == 2 and delays == [5]


async def test_dependency_outage_recovers_with_bounded_backoff_and_safe_logs(caplog):
    calls, delays = [], []

    async def service():
        calls.append(1)
        if len(calls) < 6:
            raise ExceptionGroup("secret", [RedisConnectionError("credential-do-not-log")])
        return "recovered"

    async def sleep(delay):
        delays.append(delay)

    assert await supervise(service, sleep=sleep) == "recovered"
    assert delays == [5, 10, 20, 30, 30]
    assert "credential-do-not-log" not in caplog.text and "secret" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Another receiver is already running"),
        ValueError("configuration"),
        asyncio.CancelledError(),
    ],
)
async def test_fatal_and_cancelled_services_do_not_restart(error):
    calls = []

    async def service():
        calls.append(1)
        raise error

    with pytest.raises(type(error)):
        await supervise(service)
    assert len(calls) == 1
