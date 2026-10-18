import asyncio
import tempfile
from datetime import timedelta
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx
from budget_http import install_budget_http
from fastapi import FastAPI
from proxy_budget import BURST, DEMAND_TTL, QUANTUM, ProxyBudget


class HttpBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.budget = ProxyBudget(Path(self.temp.name) / "budget.sqlite3", clock=lambda: self.now, tz=timezone.utc)
        self.calls = 0
        self.app = FastAPI()

        @self.app.post("/v1")
        async def source():
            self.calls += 1
            self.budget.reserve(QUANTUM)
            return {"status": "ok"}

        @self.app.get("/health")
        async def health():
            return {"status": "ok"}

        install_budget_http(self.app, self.budget)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://local")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def test_pre_month_blocks_all_regions_and_filters_before_source_handler(self):
        for market in ("fr", "de", "it", "co.uk"):
            for brand in ("Nike", "Gucci"):
                response = await self.client.post("/v1", json={"cmd": "request.get",
                    "url": f"https://www.vinted.{market}/catalog?search_text={brand}"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers["X-Vinted-Budget-Code"], "ProxyBudgetNotStarted")
        self.assertEqual(self.calls, 0)
        self.assertEqual((await self.client.get("/health")).status_code, 200)
        self.assertEqual((await self.client.get("/proxy-budget")).json()["code"], "ProxyBudgetNotStarted")

    async def test_daily_report_is_local_and_day_cap_blocks_all_filters_before_browser(self):
        report=await self.client.get('/proxy-budget/report')
        self.assertEqual(report.status_code,200)
        self.assertEqual(report.json()['months'],[])
        self.now=datetime(2026,11,1,tzinfo=timezone.utc)
        self.budget.reserve(1_200_000_000)
        for market in ('fr','it','de','co.uk'):
            for path in ('/catalog?search_text=Nike','/catalog?search_text=Gucci','/items/123'):
                response=await self.client.post('/v1',json={'cmd':'request.get','url':'https://www.vinted.'+market+path})
                self.assertEqual(response.status_code,503)
                self.assertEqual(response.headers['X-Vinted-Budget-Code'],'ProxyBudgetWait')
        self.assertEqual(self.calls,0)
        report=(await self.client.get('/proxy-budget/report')).json()
        self.assertEqual(report['daily_remaining_bytes'],0)
        self.assertEqual(report['monthly_charged_bytes'],1_200_000_000)

    async def test_new_month_allows_source_and_settles_admission_without_refunding_bytes(self):
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        response = await self.client.post("/v1", json={"cmd": "request.get", "url": "https://www.vinted.fr/items/123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.budget.status()["charged_bytes"], QUANTUM)
        self.assertEqual(self.budget.held, {})

    async def test_preflight_fairness_does_not_spend_or_call_source_for_waiting_work(self):
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.budget.reserve(BURST - 200000)
        await self.client.get("/proxy-budget?kind=details")
        self.assertFalse(self.budget.waiting)
        response = await self.client.get("/proxy-budget?kind=details&demand=true")
        self.assertEqual(response.json()["code"], "ProxyBudgetWait")
        before = response.json()["charged_bytes"]
        for market in ("fr", "de", "it", "co.uk"):
            for brand in ("Nike", "Gucci"):
                response = await self.client.post("/v1", json={"cmd": "request.get",
                    "url": f"https://www.vinted.{market}/catalog?search_text={brand}"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers["X-Vinted-Budget-Code"], "ProxyBudgetWait")
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.budget.status()["charged_bytes"], before)
        self.now += timedelta(seconds=DEMAND_TTL)
        response = await self.client.post("/v1", json={"cmd": "request.get",
            "url": "https://www.vinted.fr/catalog?search_text=Gucci"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, 1)

    async def test_cancelled_source_keeps_debits_and_releases_admission(self):
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        started = asyncio.Event()
        self.app.router.routes = [r for r in self.app.router.routes if getattr(r, "path", "") != "/v1"]

        @self.app.post("/v1")
        async def source():
            self.budget.reserve(QUANTUM)
            started.set()
            await asyncio.Event().wait()

        async with self.app.router.lifespan_context(self.app):
            task = asyncio.create_task(self.client.post("/v1", json={"cmd": "request.get", "url": "https://www.vinted.de/items/123"}))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.budget.held, {})
        self.assertEqual(self.budget.status()["charged_bytes"], QUANTUM)
