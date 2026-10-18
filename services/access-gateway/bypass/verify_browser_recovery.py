"""Verify both runtime dependencies and one-tab recovery in the pinned image.

Run through docker exec stdin. Navigates only to a local data URL; it does not
contact Vinted, write database rows or send Telegram messages.
"""
# Runtime-only modules live in the pinned container's /app and /opt/vinted.
# ruff: noqa: E402

import asyncio
import json
import sys
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import patch

sys.path[:0] = ["/opt/vinted", "/app"]

import httpx
from byparr_runtime import BrowserSession, main, navigate_new_tab
from src import endpoints, utils
from src.models import HealthcheckResponse
from starlette.requests import Request


async def verify(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            modes = []
            for headers, expected in (({}, "reused"), ({"X-Proxy-Username": ""}, "per-request")):
                response = await client.get("/health", headers=headers)
                assert response.status_code == 200, response.status_code
                assert HealthcheckResponse.model_validate(response.json()).user_agent
                assert response.headers["X-Vinted-Browser-Lifecycle"] == expected
                modes.append(expected)

        scope = {"type": "http", "method": "GET", "headers": []}
        dependency = app.dependency_overrides[utils.get_browser](Request(scope))
        async with aclosing(dependency):
            async for dep in dependency:
                assert isinstance(dep, BrowserSession)
                old_page, original_context = dep.page, dep.context

                async def broken_goto(*args, **kwargs):
                    raise RuntimeError("loadURI: browsingContext is undefined")

                old_page.goto = broken_goto
                request = SimpleNamespace(
                    url="data:text/html,<title>recovered</title><p>local recovery</p>",
                    block_media=True,
                )
                wrapped = SimpleNamespace(
                    remaining_ms=lambda timer: 10000, setup_routes=endpoints.setup_routes
                )
                async with asyncio.timeout(10):
                    await navigate_new_tab(dep, request, wrapped, None)
                assert dep.page is not old_page
                assert dep.context is original_context
                assert old_page.is_closed()
                assert await dep.page.title() == "recovered"
                assert "local recovery" in await dep.page.content()
        print(json.dumps({"health_modes": modes, "new_tab": True,
                          "same_context": True, "old_tab_closed": True,
                          "replacement_content_verified": True,
                          "external_navigation": False}))


if __name__ == "__main__":
    with patch("uvicorn.run") as server:
        main()
    asyncio.run(verify(server.call_args.args[0]))
