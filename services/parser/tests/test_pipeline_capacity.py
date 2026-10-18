import asyncio

import pytest

from vpars_parser.errors import ParserError
from vpars_parser.models import ItemDetailsRequest, ParseRequest
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings


async def test_slow_details_leave_catalog_capacity_available(gateway):
    release = asyncio.Event()
    entered = 0
    both_started = asyncio.Event()

    async def slow(request):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_started.set()
        await release.wait()
        raise ParserError("GatewayTimeout", "synthetic")

    gateway.fetch_item = slow
    service = ParserService(gateway, Settings(max_inflight=4))
    request = ItemDetailsRequest(region_id="FR", source_item_id="10001")
    tasks = [asyncio.create_task(service.details(request)) for _ in range(2)]
    try:
        await asyncio.wait_for(both_started.wait(), 2)
        with pytest.raises(ParserError, match="capacity"):
            await service.details(request)
        result = await asyncio.wait_for(service.parse(ParseRequest(region_id="FR")), 2)
        assert result.items and all(not task.done() for task in tasks)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()


async def test_four_stalled_catalogs_leave_two_item_slots_and_bound_capacity(gateway):
    gateway.release = asyncio.Event()
    service = ParserService(gateway, Settings(max_inflight=6))
    calls = []

    async def slow_details(request):
        calls.append(request)
        await gateway.release.wait()
        raise ParserError("GatewayTimeout", "synthetic")

    gateway.fetch_item = slow_details
    tasks = [
        asyncio.create_task(service.parse(ParseRequest(region_id=r)))
        for r in ("UK", "FR", "IT", "DE")
    ]
    try:
        for _ in range(100):
            if len(gateway.calls) == 4:
                break
            await asyncio.sleep(0.001)
        assert len(gateway.calls) == 4
        request = ItemDetailsRequest(region_id="FR", source_item_id="10001")
        tasks.extend(asyncio.create_task(service.details(request)) for _ in range(2))
        for _ in range(100):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.001)
        assert len(calls) == 2
        with pytest.raises(ParserError, match="capacity"):
            await service.parse(ParseRequest(region_id="FR"))
        with pytest.raises(ParserError, match="capacity"):
            await service.details(request)
    finally:
        gateway.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()
