import pytest
from fastapi.testclient import TestClient

from vpars_parser.api import create_app
from vpars_parser.errors import ParserError


def test_http_contract_repeated_call_and_lifecycle(gateway):
    with TestClient(create_app(gateway=gateway)) as client:
        for _ in range(2):
            response = client.post(
                "/v1/catalog/parse", json={"region_id": "GB", "request_id": "worker:42"}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["request_id"] == response.headers["x-request-id"] == "worker:42"
            assert body["items"][0]["price"] == {"amount": "4.00", "currency": "GBP"}
            assert len(body["items"]) == 8
        assert len(gateway.calls) == 2
    assert gateway.closed


def test_health_and_capabilities_do_not_fetch_catalog(gateway):
    with TestClient(create_app(gateway=gateway)) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert gateway.region_calls == 0
        assert client.get("/health/ready").json() == {
            "status": "ready",
            "vinted_access_verified": False,
        }
        response = client.get("/v1/sources/vinted/capabilities")
        body = response.json()
        assert {r["code"] for r in body["regions"]} == {"UK", "FR", "IT", "DE"}
        assert body["filters"]["colors"]["mode"] == "unsupported"
        assert body["filters"]["keywords_all"]["mode"] == "residual"
        assert body["confirmed_empty_catalog"] is False and body["newest_first"] is True
        assert not gateway.calls


def test_unavailable_gateway_does_not_kill_liveness(gateway):
    async def fail(request_id):
        raise ParserError("GatewayUnavailable", "Dependency is unavailable", 503)

    gateway.list_regions = fail
    with TestClient(create_app(gateway=gateway)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503


@pytest.mark.parametrize(
    "overrides",
    [
        {"page": 0},
        {"page": -1},
        {"page": True},
        {"page": "2"},
        {"page": 1.5},
        {"page": 4294967296},
        {"search_text": "é" * 251},
        {"search_text": "x\n"},
        {"search_text": 1},
        {"timeout_seconds": 0},
        {"timeout_seconds": 301},
        {"request_id": "bad\nheader"},
        {"filters": {"price": {"maximum": "10"}}},
        {"filters": {"price": {"minimum": "20", "maximum": "10", "currency": "EUR"}}},
        {"filters": {"price": {"maximum": "NaN", "currency": "GBP"}}},
        {"filters": {"price": {"maximum": "1.001", "currency": "GBP"}}},
        {"filters": {"price": {"maximum": 1.5, "currency": "GBP"}}},
        {"filters": {"sizes": [""]}},
        {"filters": {"sort": "newest"}},
        {"user_id": 123},
    ],
)
def test_invalid_requests_never_call_gateway(gateway, overrides):
    with TestClient(create_app(gateway=gateway)) as client:
        response = client.post("/v1/catalog/parse", json={"region_id": "UK", **overrides})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "InvalidRequest"
        assert not gateway.calls and gateway.region_calls == 0


def test_header_correlation_body_limit_and_malformed_json(gateway):
    with TestClient(create_app(gateway=gateway)) as client:
        response = client.post(
            "/v1/catalog/parse", json={"region_id": "UK"}, headers={"X-Request-ID": "header-id"}
        )
        assert response.json()["request_id"] == "header-id"
        response = client.post("/v1/catalog/parse", content=b"{" + b" " * 65536)
        assert response.status_code == 413 and response.json()["error"]["code"] == "RequestTooLarge"
        response = client.post("/v1/catalog/parse", content=b'{"token":"secret"')
        assert response.status_code == 422 and "secret" not in response.text


def test_internal_exception_is_redacted(gateway, caplog):
    gateway.error = RuntimeError("proxy_password=secret")
    with TestClient(create_app(gateway=gateway), raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/catalog/parse", json={"region_id": "UK", "request_id": "error-1"}
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "InternalError"
        assert "secret" not in response.text
        assert response.headers["x-request-id"] == "error-1"
        assert "proxy_password" not in caplog.text and "secret" not in caplog.text


def test_deep_json_is_a_safe_validation_error(gateway):
    with TestClient(create_app(gateway=gateway)) as client:
        response = client.post("/v1/catalog/parse", content=b"[" * 2000 + b"]" * 2000)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "InvalidRequest"
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert not gateway.calls


def test_openapi_documents_decimal_strings(gateway):
    with TestClient(create_app(gateway=gateway)) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        price_input = schemas.get("PriceFilter-Input", schemas.get("PriceFilter"))
        assert {s["type"] for s in price_input["properties"]["maximum"]["anyOf"]} == {
            "string",
            "null",
        }
