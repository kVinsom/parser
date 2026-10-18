"""Enforce admission before creating a browser, and expose local budget status."""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from proxy_budget import BudgetPause


def install_budget_http(app, budget):
    from starlette.responses import JSONResponse
    pending = set()

    def tracked(coroutine):
        task = asyncio.create_task(coroutine)
        pending.add(task)
        task.add_done_callback(pending.discard)
        return task

    async def settle_cancelled(admission):
        try:
            ticket = await admission
        except BudgetPause:
            return
        await asyncio.to_thread(budget.finish, ticket)

    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def drain(application):
        async with lifespan(application) as state:
            try:
                yield state
            finally:
                while pending:
                    await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))

    app.router.lifespan_context = drain

    @app.get('/proxy-budget/report')
    async def report():
        value = await asyncio.to_thread(budget.report)
        return JSONResponse(value, status_code=503 if value['code'] == 'ProxyBudgetUnavailable' else 200)

    @app.get("/proxy-budget")
    async def status(kind: str = "catalog", demand: bool = False):
        if kind not in {"catalog", "details"}:
            return JSONResponse({"code": "ProxyBudgetUnavailable", "retry_after": 30}, status_code=400)
        return await asyncio.to_thread(budget.status, kind, demand=demand)

    @app.middleware("http")
    async def gate(request, call_next):
        if request.method != "POST" or request.url.path != "/v1":
            return await call_next(request)
        try:
            payload = await request.json()
        except ValueError:
            return await call_next(request)  # Preserve normal schema validation.
        if not isinstance(payload, dict) or payload.get("cmd") != "request.get":
            return await call_next(request)
        try:
            path = urlsplit(payload.get("url", "")).path
        except (TypeError, ValueError):
            return await call_next(request)
        kind = "details" if path.startswith("/items/") else "catalog"
        admission = tracked(asyncio.to_thread(budget.admit, kind))
        try:
            ticket = await asyncio.shield(admission)
        except asyncio.CancelledError:
            tracked(settle_cancelled(admission))
            raise
        except BudgetPause as exc:
            return JSONResponse({"detail": exc.code}, status_code=503, headers={
                "X-Vinted-Budget-Code": exc.code,
                "X-Vinted-Retry-After": str(exc.retry_after), "Retry-After": str(exc.retry_after)})
        try:
            response = await call_next(request)
            if response.status_code >= 400:
                state = await asyncio.to_thread(budget.status, kind)
                if state["code"] in {"ProxyBudgetExceeded", "ProxyBudgetUnavailable", "ProxyBudgetNotStarted"}:
                    return JSONResponse({"detail": state["code"]}, status_code=503, headers={
                        "X-Vinted-Budget-Code": state["code"],
                        "X-Vinted-Retry-After": str(state["retry_after"]),
                        "Retry-After": str(state["retry_after"])})
            return response
        finally:
            # Shield settlement from disconnect cancellation. Its estimate cannot
            # refund any actual byte allowance reserved by the CONNECT gate.
            task = tracked(asyncio.to_thread(budget.finish, ticket))
            await asyncio.shield(task)
