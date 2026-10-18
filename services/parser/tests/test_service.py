import asyncio

import pytest

from vpars_parser.errors import ParserError
from vpars_parser.models import ParseRequest, Region
from vpars_parser.service import OUTCOMES, ParserService
from vpars_parser.settings import Settings


@pytest.mark.parametrize("outcome", [*OUTCOMES, "CATALOG_OUTCOME_UNSPECIFIED", "UNKNOWN"])
async def test_gateway_outcomes_never_become_empty_success(gateway, outcome):
    gateway.changes["outcome"] = outcome
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert raised.value.detail.source_status == outcome
    assert raised.value.detail.code == OUTCOMES.get(outcome, ("GatewayContractError",))[0]
    assert len(gateway.calls) == 1


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"html": b""}, "GatewayContractError"),
        ({"html": b"\xff"}, "GatewayContractError"),
        ({"region": "FR"}, "GatewayContractError"),
        ({"final_url": "https://www.vinted.fr/catalog"}, "UnexpectedRedirect"),
        ({"final_url": "https://www.vinted.co.uk/login"}, "UnexpectedRedirect"),
        ({"final_url": "https://user:secret@www.vinted.co.uk/catalog"}, "UnexpectedRedirect"),
        ({"url": "https://www.vinted.co.uk/catalog?page=2"}, "GatewayContractError"),
        ({"upstream_status": 403}, "GatewayContractError"),
    ],
)
async def test_contract_validation(gateway, changes, code):
    gateway.changes = changes
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert raised.value.detail.code == code


async def test_html_limit(gateway):
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings(max_html_bytes=10)).parse(
            ParseRequest(region_id="UK")
        )
    assert raised.value.detail.code == "ResponseTooLarge"


@pytest.mark.parametrize("region", ["PL", "US", "all"])
async def test_disabled_unknown_regions_fail_without_network(gateway, region):
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id=region))
    assert raised.value.detail.code == "UnsupportedRegion"
    assert gateway.region_calls == 0 and not gateway.calls


async def test_unknown_source_fails_without_network(gateway):
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(
            ParseRequest(source_id="other", region_id="UK")
        )
    assert raised.value.detail.code == "UnsupportedSource"
    assert gateway.region_calls == 0


async def test_gateway_region_registry_is_authoritative_and_cannot_enable_pl(gateway):
    gateway.regions = [Region(code="PL", name="Poland", base_url="https://www.vinted.pl")]
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert raised.value.detail.code == "UnsupportedRegion" and not gateway.calls


async def test_cancellation_stops_network_work_and_releases_capacity(gateway):
    gateway.started, gateway.release = asyncio.Event(), asyncio.Event()
    service = ParserService(gateway, Settings(max_inflight=1))
    caller = asyncio.create_task(service.parse(ParseRequest(region_id="UK")))
    await gateway.started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not service.active
    assert len(gateway.calls) == 1
    gateway.release.set()
    assert (await service.parse(ParseRequest(region_id="FR"))).items
    await service.close()
    assert gateway.closed and not service.active


async def test_timeout_has_no_retries(gateway):
    gateway.error = TimeoutError("secret internal address")
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert raised.value.detail.code == "GatewayTimeout" and raised.value.status == 504
    assert "secret" not in str(raised.value)
    assert len(gateway.calls) == 1


async def test_source_retry_after_preserved_in_error(gateway):
    gateway.changes = {"outcome": "RATE_LIMITED", "retry_after": 900}
    with pytest.raises(ParserError) as raised:
        await ParserService(gateway, Settings()).parse(ParseRequest(region_id="UK"))
    assert raised.value.detail.code == "SourceRateLimited"
    assert raised.value.detail.retry_after == 900


async def test_total_deadline_cancels_network_work(gateway, monkeypatch):
    # Accelerate only the outer budget; production gRPC deadlines are tested separately.
    original = asyncio.wait_for

    async def fast_wait(awaitable, timeout):  # noqa: ASYNC109 - emulate asyncio.wait_for
        return await original(awaitable, timeout=0.02)

    monkeypatch.setattr("vpars_parser.service.asyncio.wait_for", fast_wait)
    gateway.started, gateway.release = asyncio.Event(), asyncio.Event()
    service = ParserService(gateway, Settings(max_inflight=1))
    with pytest.raises(ParserError) as raised:
        await service.parse(ParseRequest(region_id="UK"))
    assert raised.value.detail.code == "ParserTimeout" and not service.active
    gateway.release.set()
    await service.close()
