import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from vpars_shared.http_cancellation import CancelOnDisconnect

from .errors import ParserError
from .models import (
    Capabilities,
    ErrorDetail,
    ErrorResponse,
    ItemDetails,
    ItemDetailsRequest,
    ParseRequest,
    ParseResult,
)
from .ports import GatewayClient
from .service import AdapterRegistry, ParserService
from .settings import Settings

ID_PATTERN = re.compile(r"[a-zA-Z0-9._:-]{1,128}")


class RequestEnvelope:
    """Bound input before JSON decoding and keep one safe correlation ID on every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        headers = dict(scope.get("headers", []))
        header_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = header_id if ID_PATTERN.fullmatch(header_id) else str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 65536:
                response = JSONResponse(
                    status_code=413,
                    content=ErrorResponse(
                        request_id=request_id,
                        error=ErrorDetail(
                            code="RequestTooLarge", message="HTTP body exceeds 64 KiB"
                        ),
                    ).model_dump(),
                    headers={"X-Request-ID": request_id},
                )
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            payload = json.loads(body)
            candidate = payload.get("request_id") if isinstance(payload, dict) else None
            if isinstance(candidate, str) and ID_PATTERN.fullmatch(candidate):
                request_id = candidate
        except (ValueError, UnicodeDecodeError):
            pass
        except RecursionError:
            response = JSONResponse(
                status_code=422,
                content=ErrorResponse(
                    request_id=request_id,
                    error=ErrorDetail(code="InvalidRequest", message="JSON nesting is too deep"),
                ).model_dump(),
                headers={"X-Request-ID": request_id},
            )
            return await response(scope, receive, send)
        scope.setdefault("state", {})["request_id"] = request_id
        delivered = False
        response_started = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def tagged(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode("ascii"))
                ]
            await send(message)

        try:
            await self.app(scope, replay, tagged)
        except Exception as exc:
            # Catch before ServerErrorMiddleware re-raises a raw exception into Uvicorn logs.
            logging.getLogger("vpars.parser").error(
                "Unhandled parser error request_id=%s type=%s", request_id, type(exc).__name__
            )
            if response_started:
                raise RuntimeError("Parser response failed after headers") from None
            response = JSONResponse(
                status_code=500,
                content=ErrorResponse(
                    request_id=request_id,
                    error=ErrorDetail(code="InternalError", message="Parser failed unexpectedly"),
                ).model_dump(),
            )
            await response(scope, replay, tagged)


def create_app(
    settings: Settings | None = None,
    gateway: GatewayClient | None = None,
    registry: AdapterRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        if gateway is None:
            from .gateway import GrpcGatewayClient

            client = GrpcGatewayClient(settings)
        else:
            client = gateway
        app.state.service = ParserService(client, settings, registry)
        try:
            yield
        finally:
            await app.state.service.close()

    app = FastAPI(title="vpars catalog parser", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestEnvelope)
    app.add_middleware(CancelOnDisconnect)

    @app.exception_handler(ParserError)
    async def parser_error(request: Request, exc: ParserError):
        return JSONResponse(
            status_code=exc.status,
            content=ErrorResponse(
                request_id=request.state.request_id, error=exc.detail
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                request_id=request.state.request_id,
                error=ErrorDetail(
                    code="InvalidRequest",
                    message="Request does not match the catalog contract",
                    fields=[".".join(map(str, e["loc"])) for e in exc.errors()],
                ),
            ).model_dump(),
        )

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(request: Request):
        service = request.app.state.service
        if service.closed:
            raise ParserError("NotReady", "Parser is shutting down", 503)
        try:
            async with asyncio.timeout(settings.control_timeout):
                regions = await service.gateway.list_regions(request.state.request_id)
            if not any(a.available_regions(regions) for a in service.registry.adapters.values()):
                raise ParserError("NotReady", "Gateway advertises no supported regions", 503)
        except (TimeoutError, ParserError):
            raise ParserError(
                "NotReady", "Gateway control RPC is unavailable or has no supported regions", 503
            ) from None
        return {"status": "ready", "vinted_access_verified": False}

    @app.get("/v1/sources/{source_id}/capabilities", response_model=Capabilities)
    async def capabilities(source_id: str, request: Request):
        service = request.app.state.service
        adapter = service.registry.get(source_id)
        try:
            async with asyncio.timeout(settings.control_timeout):
                regions = await service.gateway.list_regions(request.state.request_id)
        except TimeoutError:
            raise ParserError("GatewayTimeout", "Gateway control RPC timed out", 504) from None
        return adapter.capabilities(regions)

    @app.post(
        "/v1/catalog/parse",
        response_model=ParseResult,
        responses={code: {"model": ErrorResponse} for code in (413, 422, 500, 502, 503, 504)},
    )
    async def parse(body: ParseRequest, request: Request):
        body = body.model_copy(update={"request_id": request.state.request_id})
        return await request.app.state.service.parse(body)

    @app.post("/v1/items/details", response_model=ItemDetails)
    async def details(body: ItemDetailsRequest, request: Request):
        body = body.model_copy(update={"request_id": request.state.request_id})
        return await request.app.state.service.details(body)

    return app
