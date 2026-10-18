import asyncio

import grpc
import pytest
from conftest import html

from vpars_parser.errors import ParserError
from vpars_parser.gateway import GrpcGatewayClient, pb, rpc
from vpars_parser.ports import GatewayQuery
from vpars_parser.settings import Settings


class Server(rpc.VintedAccessServicer):
    def __init__(self):
        self.calls = []
        self.status = None
        self.html = None
        self.retry_after = None
        self.started = self.cancelled = None

    async def wait_for_cancel(self):
        if self.started is not None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    async def ListRegions(self, request, context):
        return pb.ListRegionsResponse(
            regions=[pb.Region(code="UK", name="UK", base_url="https://www.vinted.co.uk")]
        )

    async def FetchCatalog(self, request, context):
        self.calls.append((request, dict(context.invocation_metadata()), context.time_remaining()))
        await self.wait_for_cancel()
        if self.status:
            await context.abort(self.status, "internal secret detail")
        if self.retry_after:
            context.set_trailing_metadata((("retry-after", str(self.retry_after)),))
        return pb.FetchCatalogResponse(
            region=request.region,
            url="https://www.vinted.co.uk/catalog",
            final_url="https://www.vinted.co.uk/catalog",
            outcome=pb.PAGE_LOADED,
            html_utf8=self.html if self.html is not None else html().encode(),
            item_samples=["https://www.vinted.co.uk/items/only-a-sample"],
            upstream_status=200,
        )

    async def FetchItem(self, request, context):
        self.calls.append((request, dict(context.invocation_metadata()), context.time_remaining()))
        await self.wait_for_cancel()
        return pb.FetchCatalogResponse(region=request.region, outcome=pb.PAGE_LOADED)


@pytest.fixture
async def rpc_pair():
    server = grpc.aio.server()
    implementation = Server()
    rpc.add_VintedAccessServicer_to_server(implementation, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    client = GrpcGatewayClient(Settings(gateway_target=f"127.0.0.1:{port}"))
    try:
        yield client, implementation
    finally:
        await client.close()
        await server.stop(0)


async def test_actual_async_grpc_protocol_channel_reuse_and_budget(rpc_pair):
    client, server = rpc_pair
    channel = client.channel
    assert (await client.list_regions("test"))[0].code == "UK"
    for _ in range(2):
        response = await client.fetch_catalog(GatewayQuery("UK", "nike", 2, 60, "rpc-42"))
        assert response.html == html().encode() and response.outcome == "PAGE_LOADED"
    assert client.channel is channel and len(server.calls) == 2
    request, metadata, remaining = server.calls[0]
    assert request.include_html and not request.load_media
    assert request.search_text == "nike" and request.page == 2
    assert metadata["x-request-id"] == "rpc-42"
    assert 70 <= remaining <= 76


async def test_force_html_crosses_real_grpc_transport(rpc_pair):
    from vpars_parser.models import ItemDetailsRequest

    client, server = rpc_pair
    await client.fetch_item(
        ItemDetailsRequest(region_id="FR", source_item_id="123", force_html=True)
    )
    assert server.calls[0][0].force_html


@pytest.mark.parametrize("details", [False, True])
async def test_cancellation_reaches_real_grpc_server(rpc_pair, details):
    from vpars_parser.models import ItemDetailsRequest

    client, server = rpc_pair
    server.started, server.cancelled = asyncio.Event(), asyncio.Event()
    operation = (client.fetch_item(ItemDetailsRequest(region_id="FR", source_item_id="123"))
                 if details else client.fetch_catalog(GatewayQuery("UK", "Nike", 1, 60, "cancel")))
    caller = asyncio.create_task(operation)
    await asyncio.wait_for(server.started.wait(), 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait_for(server.cancelled.wait(), 2)
    assert len(server.calls) == 1


async def test_retry_after_reaches_parser_without_exposing_response_payload(rpc_pair):
    client, server = rpc_pair
    server.retry_after = 900
    page = await client.fetch_catalog(GatewayQuery("UK", "", 1, 15, "retry"))
    assert page.retry_after == 900


@pytest.mark.parametrize(
    "status,code,http_status",
    [
        (grpc.StatusCode.INVALID_ARGUMENT, "GatewayInvalidArgument", 502),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, "GatewayResourceExhausted", 503),
        (grpc.StatusCode.DEADLINE_EXCEEDED, "GatewayTimeout", 504),
        (grpc.StatusCode.UNAVAILABLE, "GatewayUnavailable", 503),
        (grpc.StatusCode.CANCELLED, "GatewayCancelled", 503),
        (grpc.StatusCode.INTERNAL, "GatewayTransportError", 502),
        (grpc.StatusCode.UNAUTHENTICATED, "GatewayAuthenticationFailed", 502),
        (grpc.StatusCode.PERMISSION_DENIED, "GatewayAuthenticationFailed", 502),
    ],
)
async def test_real_grpc_errors_redacted_and_not_retried(rpc_pair, status, code, http_status):
    client, server = rpc_pair
    server.status = status
    with pytest.raises(ParserError) as raised:
        await client.fetch_catalog(GatewayQuery("UK", "", 1, 60, "rpc-error"))
    assert raised.value.detail.code == code and raised.value.status == http_status
    assert raised.value.detail.source_status == status.name
    assert "secret" not in str(raised.value) and len(server.calls) == 1


async def test_real_grpc_html_exceeds_default_four_mib_limit(rpc_pair):
    client, server = rpc_pair
    # Synthetic padding checks transport capacity, not Vinted HTML compatibility.
    server.html = html().encode() + b" " * (5 * 1024 * 1024)
    response = await client.fetch_catalog(GatewayQuery("UK", "", 1, 60, "large-html"))
    assert response.html == server.html and len(response.html) > 4 * 1024 * 1024
    assert len(server.calls) == 1
