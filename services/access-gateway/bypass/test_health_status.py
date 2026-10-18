import asyncio
import unittest
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock, patch

from byparr_runtime import (
    REQUEST_STATUS,
    browser_request_budget,
    install_catalog_readiness,
    install_local_health,
    install_navigation_timing,
    install_response_metadata,
)
from test_vinted import CATALOG, mock_requests
from vinted import fetch_catalog


class HealthStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_deadline_returns_http_408_instead_of_server_error(self):
        import httpx
        from fastapi import Depends, FastAPI

        async def stalled_browser():
            async with asyncio.timeout(0.01):
                await asyncio.Event().wait()
                yield None

        app = FastAPI()

        @app.get("/v1")
        async def endpoint(browser: Annotated[object, Depends(stalled_browser)]):
            return {"unreachable": True}

        install_response_metadata(app, reuse=True, enabled=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1")
        self.assertEqual(response.status_code, 408)
        self.assertEqual(response.json(), {"detail": "Browser operation exceeded its request time budget"})

    async def test_local_health_never_acquires_a_browser_even_when_cold(self):
        import httpx
        from fastapi import APIRouter, Depends, FastAPI
        from pydantic import BaseModel

        class Response(BaseModel):
            user_agent: str

        page = SimpleNamespace(evaluate=AsyncMock(return_value="browser"), goto=AsyncMock())
        released = []

        async def browser():
            try:
                yield SimpleNamespace(page=page)
            finally:
                released.append(True)

        app = FastAPI()
        router = APIRouter()

        @router.get("/health", response_model=Response)
        async def health(sb: Annotated[SimpleNamespace, Depends(browser)]):
            await page.goto("https://google.com")
            return Response(user_agent="original")

        ready = [True]
        install_local_health(router, Response, ready=lambda: ready[0])
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(3):
                response = await client.get("/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"user_agent": ""})
            ready[0] = False
            self.assertEqual((await client.get("/health")).status_code, 503)
        self.assertEqual(released, [])
        page.evaluate.assert_not_awaited()
        page.goto.assert_not_awaited()

    async def test_health_probe_never_requires_json_body(self):
        request = SimpleNamespace(
            method="GET", json=AsyncMock(side_effect=ValueError())
        )
        self.assertEqual(await browser_request_budget(request), 60000)
        request.json.assert_not_awaited()

    async def test_bad_json_does_not_turn_validation_into_server_error(self):
        request = SimpleNamespace(
            method="POST", json=AsyncMock(side_effect=ValueError())
        )
        self.assertEqual(await browser_request_budget(request), 60000)
        for value, expected in [
            (12000, 12000),
            (12599, 12000),
            (60, 60000),
            (9, 9000),
            (900000, 300000),
            (0, 1000),
            (True, 60000),
        ]:
            request.json = AsyncMock(return_value={"maxTimeout": value})
            self.assertEqual(await browser_request_budget(request), expected)
        request.json = AsyncMock(return_value={"max_timeout": 30})
        self.assertEqual(await browser_request_budget(request), 30000)

    async def test_refusals_do_not_wait_for_network_idle_or_lose_status(self):
        for code in (401, 403, 429, 503):
            response = SimpleNamespace(
                status=code, header_value=AsyncMock(return_value="900")
            )
            page = SimpleNamespace(
                goto=AsyncMock(return_value=response),
                content=AsyncMock(return_value="refused"),
            )
            endpoints = SimpleNamespace(
                _navigate_and_solve=AsyncMock(),
                remaining_ms=lambda timer: 10000,
                challenge_present=AsyncMock(),
                solve_challenge=AsyncMock(),
                _wait_for_networkidle=AsyncMock(),
            )
            install_catalog_readiness(endpoints)
            install_navigation_timing(endpoints)
            metadata = {}
            token = REQUEST_STATUS.set(metadata)
            try:
                result = await endpoints._navigate_and_solve(
                    SimpleNamespace(page=page),
                    SimpleNamespace(url="https://www.vinted.fr/catalog"),
                    None,
                )
            finally:
                REQUEST_STATUS.reset(token)
            self.assertEqual(result[2].status, code)
            self.assertEqual(metadata, {"code": code})
            endpoints.solve_challenge.assert_not_awaited()
            endpoints._wait_for_networkidle.assert_not_awaited()

    def test_real_source_status_wins_over_byparr_hardcoded_200(self):
        for code, expected in (
            (401, "blocked"),
            (403, "blocked"),
            (429, "rate_limited"),
            (503, "upstream_error"),
        ):
            for http_status in (200, 408):
                module, session = mock_requests()
                session.post.return_value.status_code = http_status
                session.post.return_value.headers = {
                    "X-Vinted-Upstream-Status": str(code),
                    "X-Vinted-Retry-After": "900",
                }
                session.post.return_value.json.return_value = {
                    "status": "ok",
                    "solution": {
                        "status": 200,
                        "url": "https://www.vinted.fr/catalog",
                        "response": CATALOG,
                    },
                }
                with patch("curl_cffi.requests", module):
                    result = fetch_catalog("FR")
                self.assertEqual(result.outcome, expected)
                self.assertEqual(result.solution_status, code)
                self.assertEqual(result.retry_after, 900)
