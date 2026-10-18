from types import SimpleNamespace

import grpc
import pytest

from vpars_parser.gateway import rpc_error


@pytest.mark.parametrize("code", [
    "ProxyBudgetWait", "ProxyBudgetExceeded", "ProxyBudgetNotStarted", "ProxyBudgetUnavailable"
])
def test_budget_rpc_pause_retains_type_and_delay(code):
    error = SimpleNamespace(code=lambda: grpc.StatusCode.RESOURCE_EXHAUSTED,
        trailing_metadata=lambda: (("source-deferred-code", code), ("retry-after", "77")))
    result = rpc_error(error)
    assert result.detail.code == code
    assert result.detail.retry_after == 77
    assert result.status == 503


def test_unknown_metadata_cannot_change_error_type_or_expose_detail():
    error = SimpleNamespace(
        code=lambda: grpc.StatusCode.UNAVAILABLE,
        trailing_metadata=lambda: (
            ("source-deferred-code", "private-value"), ("retry-after", "bad")
        )
    )
    result = rpc_error(error)
    assert result.detail.code == "GatewayUnavailable"
    assert result.detail.retry_after is None
