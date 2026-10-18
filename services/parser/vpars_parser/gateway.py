"""Async transport for the existing gateway protocol, with no browser dependencies."""

import sys
from pathlib import Path

import grpc

from .errors import ParserError
from .models import Region
from .ports import GatewayPage, GatewayQuery
from .settings import Settings

GENERATED = Path(__file__).resolve().parents[1] / ".generated"
if str(GENERATED) not in sys.path:
    sys.path.insert(0, str(GENERATED))
try:
    from vinted_rpc import access_pb2 as pb
    from vinted_rpc import access_pb2_grpc as rpc
except ImportError as exc:
    raise RuntimeError("Run python tools/generate_proto.py before starting the parser") from exc


def transport_error(code: grpc.StatusCode) -> ParserError:
    mapping = {
        grpc.StatusCode.INVALID_ARGUMENT: ("GatewayInvalidArgument", 502),
        grpc.StatusCode.RESOURCE_EXHAUSTED: ("GatewayResourceExhausted", 503),
        grpc.StatusCode.DEADLINE_EXCEEDED: ("GatewayTimeout", 504),
        grpc.StatusCode.UNAVAILABLE: ("GatewayUnavailable", 503),
        grpc.StatusCode.CANCELLED: ("GatewayCancelled", 503),
        grpc.StatusCode.UNAUTHENTICATED: ("GatewayAuthenticationFailed", 502),
        grpc.StatusCode.PERMISSION_DENIED: ("GatewayAuthenticationFailed", 502),
    }
    name, status = mapping.get(code, ("GatewayTransportError", 502))
    return ParserError(name, "Gateway RPC did not complete successfully", status, code.name)


def rpc_error(exc):
    result = transport_error(exc.code())
    metadata = dict(exc.trailing_metadata() or ())
    delay = metadata.get("retry-after")
    retry = max(1, min(86400, int(delay))) if isinstance(delay, str) and delay.isdigit() else None
    code = metadata.get("source-deferred-code")
    if exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED and code in {
        "ProxyBudgetWait", "ProxyBudgetExceeded", "ProxyBudgetNotStarted", "ProxyBudgetUnavailable"
    }:
        return ParserError(
            code, "Source request paused by proxy budget", 503, retry_after=retry or 30
        )
    return ParserError(result.detail.code, result.detail.message, result.status,
                       result.detail.source_status, result.detail.fields, retry_after=retry)


class GrpcGatewayClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.channel = grpc.aio.insecure_channel(
            settings.gateway_target,
            compression=grpc.Compression.Gzip,
            options=[
                ("grpc.max_receive_message_length", 32 * 1024 * 1024),
                ("grpc.max_send_message_length", 64 * 1024),
                ("grpc.enable_retries", 0),
            ],
        )
        self.stub = rpc.VintedAccessStub(self.channel)

    async def list_regions(self, request_id: str) -> list[Region]:
        try:
            response = await self.stub.ListRegions(
                pb.ListRegionsRequest(),
                timeout=self.settings.control_timeout,
                metadata=(("x-request-id", request_id),),
            )
        except grpc.aio.AioRpcError as exc:
            raise rpc_error(exc) from None
        return [Region(code=r.code, name=r.name, base_url=r.base_url) for r in response.regions]

    async def fetch_catalog(self, query: GatewayQuery) -> GatewayPage:
        try:
            call = self.stub.FetchCatalog(
                pb.FetchCatalogRequest(
                    region=query.region,
                    search_text=query.search_text,
                    page=query.page,
                    timeout_seconds=query.timeout_seconds,
                    include_html=True,
                    load_media=False,
                    measure_timing=False,
                    newest_first=query.newest_first,
                ),
                timeout=query.timeout_seconds + self.settings.rpc_grace,
                metadata=(("x-request-id", query.request_id),),
            )
            response = await call
            retry_after = await self.retry_delay(call)
        except grpc.aio.AioRpcError as exc:
            raise rpc_error(exc) from None
        try:
            outcome = pb.CatalogOutcome.Name(response.outcome)
        except ValueError:
            outcome = "UNKNOWN"
        return GatewayPage(
            region=response.region,
            url=response.url,
            final_url=response.final_url,
            outcome=outcome,
            html=response.html_utf8,
            retry_after=retry_after,
            upstream_status=response.upstream_status
            if response.HasField("upstream_status")
            else None,
        )

    async def fetch_item(self, request) -> GatewayPage:
        try:
            call = self.stub.FetchItem(
                pb.FetchItemRequest(
                    region=request.region_id,
                    item_id=request.source_item_id,
                    timeout_seconds=request.timeout_seconds,
                    include_html=True,
                    force_html=request.force_html,
                ),
                timeout=request.timeout_seconds + self.settings.rpc_grace,
                metadata=(("x-request-id", request.request_id),),
            )
            response = await call
            retry_after = await self.retry_delay(call)
        except grpc.aio.AioRpcError as exc:
            raise rpc_error(exc) from None
        return GatewayPage(
            region=response.region,
            url=response.url,
            final_url=response.final_url,
            outcome=pb.CatalogOutcome.Name(response.outcome),
            html=response.html_utf8,
            item_json=response.item_json_utf8,
            retry_after=retry_after,
            upstream_status=response.upstream_status
            if response.HasField("upstream_status")
            else None,
        )

    @staticmethod
    async def retry_delay(call):
        for key, value in (await call.trailing_metadata()) or ():
            if key == "retry-after" and isinstance(value, str) and value.isdigit():
                return max(1, min(int(value), 86400))
        return None

    async def close(self) -> None:
        await self.channel.close()
