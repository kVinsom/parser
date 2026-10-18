import os
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from tools.benchmark_total import LiveRequestCap, Trace, distribution, measure


def test_tail_percentiles_do_not_hide_a_slow_outlier_in_a_small_sample():
    stats = distribution([100, 101, 5000])
    assert stats["p50"] == 101
    assert stats["p95"] == stats["p99"] == stats["max"] == 5000
    assert stats["samples"] == 3


def test_empty_measurement_cannot_be_reported_as_zero_latency():
    with pytest.raises(ValueError, match="empty"):
        distribution([])
    with pytest.raises(RuntimeError, match="Incomplete"):
        Trace(started=100).result()


async def test_live_measurement_cannot_exceed_authorized_outgoing_requests():
    cap = LiveRequestCap(5)
    transport = AsyncMock(return_value=True)
    method = SendMessage(chat_id=101, text="Test")
    for _ in range(5):
        await cap(transport, None, method)
    with pytest.raises(RuntimeError, match="allowance"):
        await cap(transport, None, method)
    assert transport.await_count == 5


async def test_live_measurement_does_not_repeat_an_uncertain_delivery():
    cap = LiveRequestCap(5)
    method = SendMessage(chat_id=101, text="Test")
    transport = AsyncMock(side_effect=TelegramNetworkError(method, "uncertain delivery"))
    with pytest.raises(TelegramNetworkError):
        await cap(transport, None, method)
    with pytest.raises(RuntimeError, match="uncertain"):
        await cap(transport, None, method)
    assert transport.await_count == 1


@pytest.mark.integration
async def test_full_benchmark_observes_http_budget_retries_and_persisted_completion(request):
    if not request.config.getoption("--integration"):
        pytest.skip("Pass --integration to test PostgreSQL and Redis")
    result = await measure(
        os.environ.get(
            "TEST_POSTGRES_URL", "postgresql+asyncpg://vpars:vpars_local@127.0.0.1:15432/vpars_bot"
        ),
        os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:16379/0"),
        {"kind": "one_chat", "count": 2, "delay": 0.01},
    )
    samples = result["samples"]
    assert len(samples) == 2
    for sample in samples:
        assert sample["completed_ms"] >= sample["response_ms"] >= sample["api_ms"] > 0
        assert sample["initial_queue_ms"] >= 0
        assert sample["handler_without_api_ms"] > 0
        assert sample["wait_and_inbox_commit_ms"] >= 0
        assert sample["api_calls"] == 1
    # Concurrent webhook POSTs may reach the server in either order; pacing must still apply.
    response_times = sorted(sample["response_ms"] for sample in samples)
    assert response_times[1] - response_times[0] >= 900
    assert result["metrics"]["counters"]["outbound_deferred"] >= 1
