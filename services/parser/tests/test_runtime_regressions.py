import asyncio

import httpx
import pytest

from vpars_parser.api import create_app
from vpars_parser.errors import ParserError
from vpars_parser.models import ParseRequest
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings


async def test_http_health_stays_responsive_and_overload_is_explicit(gateway):
    gateway.started, gateway.release = asyncio.Event(), asyncio.Event()
    app = create_app(gateway=gateway, settings=Settings(max_inflight=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            first = asyncio.create_task(client.post("/v1/catalog/parse", json={"region_id": "UK"}))
            try:
                await asyncio.wait_for(gateway.started.wait(), 2)
                live = await client.get("/health/live")
                ready = await client.get("/health/ready")
                busy = await client.post("/v1/catalog/parse", json={"region_id": "FR"})
                assert live.status_code == ready.status_code == 200
                assert busy.status_code == 503 and busy.json()["error"]["code"] == "ParserBusy"
                assert len(gateway.calls) == 1
            finally:
                gateway.release.set()
            assert (await first).status_code == 200
    assert gateway.closed


async def test_failed_call_releases_capacity_for_next_request(gateway):
    service = ParserService(gateway, Settings())
    gateway.error = ParserError("AccessBlocked", "Blocked")
    with pytest.raises(ParserError):
        await service.parse(ParseRequest(region_id="UK"))
    gateway.error = None
    result = await service.parse(ParseRequest(region_id="FR"))
    assert result.completeness == "complete" and len(gateway.calls) == 2
    await service.close()


async def test_shutdown_waits_for_inflight_work_and_rejects_new_requests(gateway):
    gateway.started, gateway.release = asyncio.Event(), asyncio.Event()
    service = ParserService(gateway, Settings())
    request = asyncio.create_task(service.parse(ParseRequest(region_id="UK")))
    await gateway.started.wait()
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not closing.done() and not gateway.closed
    with pytest.raises(ParserError) as raised:
        await service.parse(ParseRequest(region_id="FR"))
    assert raised.value.detail.code == "ParserBusy"
    gateway.release.set()
    await request
    await closing
    assert gateway.closed
