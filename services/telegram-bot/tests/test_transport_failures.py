import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage, SendRichMessage
from aiogram.types import InputRichMessage
from redis.exceptions import ConnectionError, TimeoutError

from vpars_bot.latency import LatencyMetrics
from vpars_bot.transport import TelegramDeferred, TelegramRequestPolicy


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError])
async def test_budget_failure_defers_before_calling_telegram(rich, failure):
    budget, request = AsyncMock(), AsyncMock()
    budget.acquire.side_effect = failure("temporarily unavailable")
    policy = TelegramRequestPolicy(budget, LatencyMetrics())
    method = (
        SendRichMessage(chat_id=101, rich_message=InputRichMessage(html="<p>Test</p>"))
        if rich
        else SendMessage(chat_id=101, text="Test")
    )
    with pytest.raises(TelegramDeferred) as caught:
        await policy(request, None, method)
    assert 0 < caught.value.retry_after <= 5
    request.assert_not_awaited()
    budget.acquire.side_effect = None
    await policy(request, None, method)
    request.assert_awaited_once()


@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError])
async def test_failed_shared_cooldown_keeps_definite_telegram_429(failure):
    budget, request = AsyncMock(), AsyncMock()
    method = SendMessage(chat_id=101, text="Test")
    refusal = TelegramRetryAfter(method, "limited", retry_after=90)
    request.side_effect = refusal
    budget.cooldown.side_effect = failure("temporarily unavailable")
    policy = TelegramRequestPolicy(budget, LatencyMetrics())
    with pytest.raises(TelegramRetryAfter) as caught:
        await policy(request, None, method)
    assert caught.value is refusal and caught.value.retry_after == 90
    request.assert_awaited_once()
    # Other messages must still honor the server's pause if Redis recovers
    # without retaining the failed shared write.
    with pytest.raises(TelegramDeferred) as deferred:
        await policy(request, None, method)
    assert deferred.value.retry_after > 88
    request.assert_awaited_once()


async def test_error_after_request_started_is_not_reclassified_as_deferral():
    budget, request = AsyncMock(), AsyncMock()
    error = ConnectionError("failure after request started")
    request.side_effect = error
    policy = TelegramRequestPolicy(budget, LatencyMetrics())
    with pytest.raises(ConnectionError) as caught:
        await policy(request, None, SendMessage(chat_id=101, text="Test"))
    assert caught.value is error
    request.assert_awaited_once()


@pytest.mark.parametrize("stage", ["acquire", "cooldown"])
async def test_unresponsive_budget_has_bounded_wait(stage):
    budget, request = AsyncMock(), AsyncMock()
    method = SendMessage(chat_id=101, text="Test")
    policy = TelegramRequestPolicy(budget, LatencyMetrics(), budget_timeout=0.01)

    async def unresponsive(*args, **kwargs):
        await asyncio.Event().wait()

    getattr(budget, stage).side_effect = unresponsive
    if stage == "cooldown":
        request.side_effect = TelegramRetryAfter(method, "limited", retry_after=90)
    expected = TelegramDeferred if stage == "acquire" else TelegramRetryAfter
    async with asyncio.timeout(1):
        with pytest.raises(expected):
            await policy(request, None, method)
    assert request.await_count == int(stage == "cooldown")
