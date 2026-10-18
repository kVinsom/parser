"""Full local command benchmark: real webhook/SQL/Redis/aiogram/budget/HTTP, fake Telegram.

This CLI never loads a real token or contacts Telegram. The shared environment can
use a real token only when explicitly passed by benchmark_live.py.
Times begin immediately before the local webhook POST, not on the user's device.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Update
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpars_bot.application import AdminService, Catalog
from vpars_bot.config import Settings
from vpars_bot.db import Inbox
from vpars_bot.domain import Filters, SourceRegistry
from vpars_bot.latency import LatencyMetrics
from vpars_bot.runtime import UpdateInbox, webhook_app
from vpars_bot.search_control import PersistentSearchControl
from vpars_bot.telegram import BotUI, TelegramProfileVerifier, make_dispatcher
from vpars_bot.transport import MESSAGE_METHODS, RedisTelegramBudget, TelegramRequestPolicy
from vpars_bot.update_context import current_update


def distribution(values):
    """Nearest-rank percentiles; each event has equal weight, including slow outliers."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot summarize an empty sample")
    return {
        "samples": len(ordered),
        "min": round(ordered[0], 2),
        "mean": round(statistics.mean(ordered), 2),
        "p50": round(statistics.median(ordered), 2),
        "p95": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 2),
        "p99": round(ordered[math.ceil(0.99 * len(ordered)) - 1], 2),
        "max": round(ordered[-1], 2),
    }


@dataclass
class Trace:
    started: float
    admitted: float = 0
    first_handler: float = 0
    reply: float = 0
    completed: float = 0
    api_seconds: float = 0
    handler_seconds: float = 0
    attempts: int = 0
    api_calls: int = 0
    successful_handler: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def result(self):
        if not (self.admitted and self.reply and self.completed):
            raise RuntimeError("Incomplete command measurement")
        return {
            "response_ms": (self.reply - self.started) * 1000,
            "completed_ms": (self.completed - self.started) * 1000,
            "admission_ms": (self.admitted - self.started) * 1000,
            "initial_queue_ms": (self.first_handler - self.admitted) * 1000,
            "api_ms": self.api_seconds * 1000,
            "handler_without_api_ms": (self.handler_seconds - self.api_seconds) * 1000,
            "wait_and_inbox_commit_ms": (self.completed - self.admitted - self.handler_seconds)
            * 1000,
            "attempts": self.attempts,
            "api_calls": self.api_calls,
        }


class ObservedInbox(UpdateInbox):
    def __init__(self, *args, traces, **kwargs):
        super().__init__(*args, **kwargs)
        self.traces = traces

    async def receive(self, update):
        await super().receive(update)
        self.traces[update.update_id].admitted = perf_counter()

    async def process(self, claimed, dispatcher, bot):
        trace = self.traces[claimed.update_id]
        await super().process(claimed, dispatcher, bot)
        if trace.successful_handler:
            trace.completed = perf_counter()
            trace.done.set()


class ObservedDispatcher:
    def __init__(self, dispatcher, traces):
        self.dispatcher, self.traces = dispatcher, traces

    async def feed_update(self, bot, update):
        trace = self.traces[update.update_id]
        started = perf_counter()
        trace.first_handler = trace.first_handler or started
        trace.attempts += 1
        trace.successful_handler = False
        try:
            await self.dispatcher.feed_update(bot, update)
            trace.successful_handler = True
        finally:
            trace.handler_seconds += perf_counter() - started


class ObserveHTTP:
    """Installed INSIDE the real budget: only actual HTTP attempts are timed here."""

    def __init__(self, traces):
        self.traces = traces

    async def __call__(self, make_request, bot, method):
        trace = self.traces[current_update.get().update_id]
        started = perf_counter()
        trace.api_calls += 1
        try:
            result = await make_request(bot, method)
            if method.__api_method__ in MESSAGE_METHODS:
                trace.reply = perf_counter()
            return result
        finally:
            trace.api_seconds += perf_counter() - started


class LiveRequestCap:
    """Bound externally visible effects, including requests with an uncertain result."""

    def __init__(self, limit):
        self.remaining = limit
        self.uncertain = False

    async def __call__(self, make_request, bot, method):
        if method.__api_method__ not in MESSAGE_METHODS:
            raise RuntimeError("Unexpected live benchmark API method")
        if self.remaining <= 0 or self.uncertain:
            raise RuntimeError("Live benchmark request allowance exhausted or outcome uncertain")
        self.remaining -= 1
        try:
            return await make_request(bot, method)
        except (TelegramNetworkError, TimeoutError):
            self.uncertain = True  # Do not retry a message Telegram may already have accepted.
            raise


class FakeTelegram:
    def __init__(self, delay, *, retry_after=0, slow_first=0):
        self.delay, self.retry_after, self.slow_first = delay, retry_after, slow_first
        self.calls = 0
        self.entered = asyncio.Event()

    async def request(self, request):
        # Only our offline token is accepted. This server listens on loopback only.
        if request.match_info["token"] != "42:benchmark":
            raise web.HTTPForbidden()
        method = request.match_info["method"]
        if method not in {"sendMessage", "editMessageText"}:
            raise RuntimeError(f"Unexpected API method: {method}")
        fields = await request.post()
        self.calls += 1
        first = self.calls == 1
        self.entered.set()
        await asyncio.sleep(self.slow_first if first and self.slow_first else self.delay)
        if first and self.retry_after:
            return web.json_response(
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: benchmark cooldown",
                    "parameters": {"retry_after": self.retry_after},
                },
                status=429,
            )
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "message_id": int(fields.get("message_id", 1000 + self.calls)),
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": int(fields["chat_id"]), "type": "private"},
                    "text": fields["text"],
                    "from": {"id": 42, "is_bot": True, "first_name": "Benchmark"},
                },
            }
        )


def incoming(number, user, *, stop=False):
    author = {"id": user, "is_bot": False, "first_name": "Benchmark"}
    message = {
        "message_id": number,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": user, "type": "private"},
        "from": author,
        "text": "/start",
        "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
    }
    event = (
        {
            "callback_query": {
                "id": str(number),
                "chat_instance": "benchmark",
                "data": "oldscreen:stop",
                "from": author,
                "message": message,
            }
        }
        if stop
        else {"message": message}
    )
    return Update.model_validate({"update_id": number, **event})


async def cancel(task):
    task.cancel()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    if isinstance(result, Exception):
        raise result


@asynccontextmanager
async def environment(postgres_url, redis_url, fake, *, live_token=None, live_limit=5):
    schema = "bench_total_" + uuid4().hex
    async with AsyncExitStack() as stack:
        root = create_async_engine(postgres_url, hide_parameters=True)
        stack.push_async_callback(root.dispose)
        async with root.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

        async def drop_schema():
            async with root.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

        stack.push_async_callback(drop_schema)
        engine = create_async_engine(
            postgres_url,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"server_settings": {"search_path": schema}},
        )
        stack.push_async_callback(engine.dispose)

        def migrate(connection):
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        async with engine.begin() as conn:
            await conn.run_sync(migrate)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        redis = Redis.from_url(redis_url)
        stack.push_async_callback(redis.aclose)

        async def clear_keys():
            keys = [key async for key in redis.scan_iter(match=schema + ":*")]
            if keys:
                await redis.delete(*keys)

        stack.push_async_callback(clear_keys)
        await redis.ping()
        registry = SourceRegistry.read(Path("config/sources.json"))
        catalog = Catalog(sessions, registry)
        control = PersistentSearchControl(sessions, registry)
        if live_token is None:
            api_app = web.Application()
            api_app.router.add_post("/bot{token}/{method}", fake.request)
            api_server = await stack.enter_async_context(TestServer(api_app, host="127.0.0.1"))
            bot_session = AiohttpSession(
                api=TelegramAPIServer.from_base(str(api_server.make_url("/"))), timeout=7, limit=40
            )
        else:
            # Explicit opt-in used only by benchmark_live.py after recipient confirmation.
            bot_session = AiohttpSession(timeout=7, limit=40)
        stack.push_async_callback(bot_session.close)
        bot = Bot(
            live_token or "42:benchmark",
            session=bot_session,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        traces, metrics = {}, LatencyMetrics()
        bot_session.middleware(
            TelegramRequestPolicy(
                RedisTelegramBudget(redis, bot.id, prefix=schema + ":budget"),
                metrics,
                concurrency=8,
            )
        )
        if live_token is not None:
            bot_session.middleware(LiveRequestCap(live_limit))
        bot_session.middleware(ObserveHTTP(traces))
        storage = RedisStorage(
            redis,
            key_builder=DefaultKeyBuilder(prefix=schema, with_bot_id=True),
            state_ttl=86400,
            data_ttl=86400,
        )
        isolation = storage.create_isolation(lock_kwargs={"timeout": 30})

        class LiveTestUI(BotUI):
            async def screen(self, message, state, body, rows):
                # Test messages cannot trigger actions against disposable searches after cleanup.
                label = f"🧪 Тест затримки {message.message_id - 9}. Пошук не запускається.\n\n"
                await super().screen(message, state, label + body, [])

        ui_class = LiveTestUI if live_token is not None else BotUI
        ui = ui_class(
            catalog, control, AdminService(sessions, TelegramProfileVerifier(bot)), registry
        )
        dp = make_dispatcher(storage, isolation, ui, acknowledge_callbacks=False)
        inbox = ObservedInbox(sessions, bot.id, traces=traces, metrics=metrics, control=control)
        settings = Settings(
            _env_file=None,
            update_mode="webhook",
            webhook_base_url="https://benchmark.invalid",
            webhook_secret="benchmark_" + uuid4().hex,
        )
        client = await stack.enter_async_context(
            TestClient(TestServer(webhook_app(settings, inbox), host="127.0.0.1"))
        )
        pump = asyncio.create_task(inbox.pump(ObservedDispatcher(dp, traces), bot))
        stack.push_async_callback(cancel, pump)

        async def submit(number, user, *, stop=False):
            payload = incoming(number, user, stop=stop).model_dump(mode="json", exclude_none=True)
            trace = traces[number] = Trace(perf_counter())
            async with client.post(
                "/telegram",
                json=payload,
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret.get_secret_value()
                },
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"Webhook benchmark returned {response.status}")
                await response.read()
            return trace

        yield catalog, control, sessions, submit, traces, metrics


SCENARIOS = {
    "idle_menu_50ms": {"kind": "sequential", "count": 20, "delay": 0.05},
    "idle_menu_250ms": {"kind": "sequential", "count": 20, "delay": 0.25},
    "burst_32_chats": {"kind": "burst", "count": 32, "delay": 0.05},
    "burst_5_one_chat": {"kind": "one_chat", "count": 5, "delay": 0.05},
    "telegram_429_3s": {"kind": "burst", "count": 1, "delay": 0.05, "retry_after": 3},
    "stop_during_slow_reply": {"kind": "stop", "count": 2, "delay": 0.05, "slow_first": 2},
}


async def measure(postgres_url, redis_url, scenario):
    fake = FakeTelegram(
        scenario["delay"],
        retry_after=scenario.get("retry_after", 0),
        slow_first=scenario.get("slow_first", 0),
    )
    async with environment(postgres_url, redis_url, fake) as env:
        catalog, control, sessions, submit, traces, metrics = env
        kind, count = scenario["kind"], scenario["count"]
        users = [900001 + (0 if kind in {"one_chat", "stop"} else i) for i in range(count)]
        for user in sorted(set(users)):
            await catalog.register(user, user, None, "Benchmark", 1, start=True)
            await catalog.save(user, Filters(sources={"vinted": ["FR"]}), "benchmark-card")
        stop_result = None
        async with asyncio.timeout(60):
            if kind == "stop":
                await control.start(users[0], "benchmark-running")
                await submit(10, users[0])
                await fake.entered.wait()
                stop_trace = await submit(11, users[0], stop=True)
                assert set((await control.status(users[0])).values()) == {"stopped"}
                stop_result = {"committed_ms": (stop_trace.admitted - stop_trace.started) * 1000}
            elif kind == "sequential":
                for i, user in enumerate(users):
                    trace = await submit(10 + i, user)
                    await trace.done.wait()
            else:
                await asyncio.gather(*(submit(10 + i, user) for i, user in enumerate(users)))
            await asyncio.gather(*(trace.done.wait() for trace in traces.values()))
        async with sessions() as session:
            states = (await session.scalars(select(Inbox.state))).all()
            assert len(states) == count and set(states) == {"processed"}
        samples = [{"update_id": key, **trace.result()} for key, trace in traces.items()]
        if stop_result is not None:
            stop_result["confirmation_ms"] = stop_trace.result()["response_ms"]
        return {"samples": samples, "stop": stop_result, "metrics": metrics.snapshot()}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    postgres_url = os.environ.get(
        "TEST_POSTGRES_URL", "postgresql+asyncpg://vpars:vpars_local@127.0.0.1:15432/vpars_bot"
    )
    redis_url = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:16379/0")
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "kind": "local full command pipeline with simulated Telegram HTTP server",
        "real_telegram": False,
        "boundary": "before local webhook POST -> successful Bot API HTTP response received",
        "percentiles": "nearest rank, pooled individual updates across all runs",
        "warmup": "DB/Redis initialized and users/cards seeded; no bot HTTP warmup",
        "config": {"concurrency": 8, "messages_per_second": 25, "chat_interval_seconds": 1},
        "code_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path("vpars_bot").glob("*.py"))
        },
        "scenarios": {},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for name in args.scenario or SCENARIOS:
        runs = []
        for index in range(args.runs):
            runs.append(await measure(postgres_url, redis_url, SCENARIOS[name]))
            print(f"{name}: run {index + 1}/{args.runs} complete", flush=True)
        samples = [sample for run in runs for sample in run["samples"]]
        summary = {
            field: distribution([sample[field] for sample in samples])
            for field in samples[0]
            if field.endswith("_ms")
        }
        report["scenarios"][name] = {
            "parameters": SCENARIOS[name],
            "summary": summary,
            "runs": runs,
        }
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({name: summary["response_ms"]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
