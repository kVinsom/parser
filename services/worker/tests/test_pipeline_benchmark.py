"""Controlled local benchmark: real SQL and HTTP, no Vinted or Telegram traffic."""

import asyncio
import json
from math import ceil
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_monitoring import Parser, lot, rows, start
from vpars_bot.db import utcnow
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery, SentLot
from vpars_worker.monitor import Monitor
from vpars_worker.parser import ParserClient


@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "separate-lanes"])
async def test_local_pipeline_benchmark(environment, parallel):
    store, _ = environment
    await start(environment)
    items = {str(30000 + i): lot(str(30000 + i), photos_complete=False) for i in range(12)}
    latencies = []
    detail_calls = 0
    beginning = perf_counter()

    async def details(request):
        nonlocal detail_calls
        detail_calls += 1
        item = items[(await request.json())["source_item_id"]]
        await asyncio.sleep(0.2)
        return web.json_response(
            {
                "source_id": "vinted",
                "region_id": "FR",
                "source_item_id": item.source_item_id,
                "photo_urls": item.photo_urls,
                "photos_complete": True,
                "description": "Good",
                "published_at": item.published_at.isoformat(),
                "publication_evidence": "exact",
                "fetched_at": utcnow().isoformat(),
            }
        )

    async def telegram(request):
        data = await request.post()
        assert (
            data["rich_message"]
            and len(json.loads(data["reply_markup"])["inline_keyboard"][0]) == 2
        )
        await asyncio.sleep(0.06)
        latencies.append((perf_counter() - beginning) * 1000)
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "message_id": 1000 + len(latencies),
                    "date": int(utcnow().timestamp()),
                    "chat": {"id": int(data["chat_id"]), "type": "private"},
                    "text": "Test lot",
                },
            }
        )

    app = web.Application()
    app.router.add_post("/v1/items/details", details)
    app.router.add_post("/bot42:TEST/sendRichMessage", telegram)
    async with TestServer(app) as server:
        parser = ParserClient(str(server.make_url("")))
        bot = Bot(
            "42:TEST",
            session=AiohttpSession(
                api=TelegramAPIServer.from_base(str(server.make_url(""))),
                timeout=3,
            ),
        )
        notifier = Notifier(store, bot, parser=parser)
        tasks = []
        try:
            beginning = perf_counter()
            await Monitor(store, Parser(items=list(items.values()))).once()
            async with asyncio.timeout(20):
                if parallel:
                    tasks = [
                        asyncio.create_task(notifier.run(lane=lane))
                        for lane in ("prepare", "prepare", "ready", "ready")
                    ]
                    while len(await rows(store, SentLot)) < len(items):
                        await asyncio.sleep(0.02)
                else:
                    while await notifier.once():
                        pass
            elapsed = (perf_counter() - beginning) * 1000
            assert len(latencies) == len(items)
            assert 0 < detail_calls <= len(items)
            delivered = await rows(store, Delivery)
            assert all(row.state == "sent" for row in delivered)
            fallback = [row for row in delivered if not row.item["photos_complete"]]
            assert all(
                row.media_progress["gallery_unavailable"]
                and row.media_progress["detail_error"] == "DetailsDeadlineExceeded"
                and row.item["published_at"]
                == items[row.item["source_item_id"]].model_dump(mode="json")["published_at"]
                for row in fallback
            )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await parser.close()
            await bot.session.close()
    ordered = sorted(latencies)
    report = {
        "mode": "separate-lanes" if parallel else "serial",
        "scope": "Local PostgreSQL + HTTP; Vinted 200ms and Telegram 60ms are simulated; no Telegram pacing",
        "items": len(items),
        "details_requests": detail_calls,
        "bounded_optional_fallbacks": len(fallback),
        "batch_ms": round(elapsed, 1),
        "first_delivery_ms": round(ordered[0], 1),
        "p50_delivery_ms": round(median(ordered), 1),
        "p95_delivery_ms": round(ordered[ceil(len(ordered) * 0.95) - 1], 1),
    }
    path = (
        Path(__file__).resolve().parents[1]
        / ".artifacts"
        / f"pipeline-benchmark-{report['mode']}.json"
    )
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
