"""Real HTTP disconnects cancel source work; CPU capacity remains honest."""

import asyncio
import json
import socket
import threading

import pytest
import uvicorn
from test_item_details import page

from vpars_parser.api import create_app
from vpars_parser.errors import ParserError
from vpars_parser.models import ItemDetailsRequest
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings


@pytest.mark.parametrize("region", ["FR", "DE", "IT", "UK"])
async def test_real_http_disconnect_cancels_gateway_details(gateway, region):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def fetch_item(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gateway.fetch_item = fetch_item
    app = create_app(gateway=gateway)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.setblocking(False)
    ready = asyncio.Event()

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    server = ReadyServer(uvicorn.Config(app, log_level="error", lifespan="on"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        released = asyncio.Event()
        original_finished = app.state.service._finished

        def finished(task):
            original_finished(task)
            released.set()

        app.state.service._finished = finished
        _, writer = await asyncio.open_connection(*listener.getsockname())
        body = json.dumps({"region_id": region, "source_item_id": "10001"}).encode()
        writer.write(b"POST /v1/items/details HTTP/1.1\r\nHost: localhost\r\n"
                     b"Content-Type: application/json\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        await asyncio.wait_for(started.wait(), 2)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.wait_for(released.wait(), 2)
        assert not app.state.service.active
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
    assert gateway.closed


async def test_cancel_during_cpu_work_keeps_slot_until_thread_exits(gateway, monkeypatch):
    started, finish = threading.Event(), threading.Event()

    async def fetch_item(request):
        return page()

    def normalize(*args):
        started.set()
        assert finish.wait(3)

    gateway.fetch_item = fetch_item
    service = ParserService(gateway, Settings(max_inflight=1))
    monkeypatch.setattr(service.registry.get("vinted"), "parse_details", normalize)
    request = ItemDetailsRequest(region_id="FR", source_item_id="10001")
    caller = asyncio.create_task(service.details(request))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()  # A second cancellation must not release a running thread's slot.
        await asyncio.sleep(0)
        assert service.active and not caller.done()
        with pytest.raises(ParserError) as raised:
            await service.details(request)
        assert raised.value.detail.code == "ParserBusy"
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await service.close()
    assert not service.active
