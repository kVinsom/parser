"""Bounded Telegram calls and shared Redis pacing; never hides uncertain sends with retries."""

import asyncio
from time import perf_counter

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from redis.exceptions import RedisError

from .latency import LatencyMetrics

MESSAGE_METHODS = {
    "sendMessage",
    "sendRichMessage",
    "sendPhoto",
    "sendDocument",
    "sendMediaGroup",
    "editMessageText",
    "editMessageCaption",
    "editMessageReplyMarkup",
}


class TelegramDeferred(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(0.01, float(retry_after))
        super().__init__("Telegram request deferred")


class TelegramBudgetUnavailable(TelegramDeferred):
    """No Telegram request was attempted because shared pacing was unavailable."""


class RedisTelegramBudget:
    # Check-and-reserve is atomic across bot/notifier processes and uses Redis's clock.
    RESERVE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
local ready = math.max(tonumber(redis.call('GET', KEYS[1]) or '0'),
                      tonumber(redis.call('GET', KEYS[2]) or '0'),
                      tonumber(redis.call('GET', KEYS[3]) or '0'))
if ready > now then return math.ceil(ready - now) end
redis.call('SET', KEYS[1], now + tonumber(ARGV[1]), 'PX', math.ceil(tonumber(ARGV[1])) + 1000)
redis.call('SET', KEYS[2], now + tonumber(ARGV[2]), 'PX', math.ceil(tonumber(ARGV[2])) + 1000)
return 0
"""
    COOLDOWN = """
local t = redis.call('TIME')
local until_ms = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000 + tonumber(ARGV[1])
local old = tonumber(redis.call('GET', KEYS[1]) or '0')
if until_ms > old then redis.call('SET', KEYS[1], until_ms, 'PX', math.ceil(tonumber(ARGV[1])) + 1000) end
return 1
"""

    def __init__(
        self, redis, bot_id, *, per_second=25, chat_interval=1.0, prefix="telegram-budget"
    ):
        self.redis = redis
        self.prefix = f"{prefix}:{{{bot_id}}}"
        self.global_ms = 1000 / per_second
        self.chat_ms = 1000 * chat_interval

    async def acquire(self, chat_id, *, cost=1):
        wait = await self.redis.eval(
            self.RESERVE,
            3,
            self.prefix + ":global",
            f"{self.prefix}:chat:{chat_id}",
            self.prefix + ":cooldown",
            self.global_ms * cost,
            self.chat_ms,
        )
        if wait:
            raise TelegramDeferred(wait / 1000)

    async def cooldown(self, seconds):
        await self.redis.eval(self.COOLDOWN, 1, self.prefix + ":cooldown", (seconds + 0.05) * 1000)


class TelegramRequestPolicy(BaseRequestMiddleware):
    def __init__(self, budget, metrics: LatencyMetrics, *, concurrency=12, budget_timeout=1.0):
        self.budget, self.metrics = budget, metrics
        self.messages = asyncio.Semaphore(concurrency)
        self.cooldown_until = 0
        self.budget_timeout = budget_timeout

    async def __call__(self, make_request, bot, method):
        name = method.__api_method__
        if name not in MESSAGE_METHODS:
            return await self.measured(make_request, bot, method)
        # Reserve only when a connection slot is available; no unbounded waiting queue here.
        async with self.messages:
            try:
                remaining = self.cooldown_until - perf_counter()
                if remaining > 0:
                    raise TelegramDeferred(remaining)
                async with asyncio.timeout(self.budget_timeout):
                    if name == "sendMediaGroup":
                        await self.budget.acquire(method.chat_id, cost=len(method.media))
                    else:
                        await self.budget.acquire(method.chat_id)
            except TelegramDeferred:
                self.metrics.count("outbound_deferred")
                raise
            except (RedisError, TimeoutError) as exc:
                # Nothing has reached make_request. Fail closed on shared pacing,
                # but let both inbox and notifier retry a definite unsent message.
                self.metrics.count("telegram_budget_unavailable")
                raise TelegramBudgetUnavailable(2) from exc
            return await self.measured(make_request, bot, method)

    async def measured(self, make_request, bot, method):
        name = method.__api_method__
        started = perf_counter()
        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as exc:
            self.metrics.count("telegram_429")
            if name in MESSAGE_METHODS:
                self.cooldown_until = max(
                    self.cooldown_until, perf_counter() + exc.retry_after + 0.05
                )
                try:
                    async with asyncio.timeout(self.budget_timeout):
                        await self.budget.cooldown(exc.retry_after)
                except (RedisError, TimeoutError):
                    # A failed Redis write cannot turn a definite rejection into
                    # an uncertain send. Keep the original retry and a local pause.
                    self.metrics.count("telegram_cooldown_unavailable")
            raise
        except TelegramAPIError:
            self.metrics.count("telegram_errors")
            raise
        finally:
            # Method names come from aiogram classes, never incoming user data.
            if name != "getUpdates":  # Long-poll wait is not an API latency sample.
                self.metrics.observe(f"telegram.{name}", perf_counter() - started)


class CallbackAcknowledger:
    def __init__(self, bot, metrics, *, timeout=1.0, capacity=64):
        self.bot, self.metrics, self.timeout, self.capacity = bot, metrics, timeout, capacity
        self.tasks = set()
        self.slots = asyncio.Semaphore(4)

    def offer(self, update):
        if not update.callback_query:
            return
        if len(self.tasks) >= self.capacity:
            self.metrics.count("callback_ack_overload")
            return
        task = asyncio.create_task(self.answer(update.callback_query.id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def answer(self, callback_id):
        try:
            async with asyncio.timeout(self.timeout):
                async with self.slots:
                    await self.bot.answer_callback_query(callback_id, request_timeout=self.timeout)
        except (TelegramAPIError, TimeoutError):
            self.metrics.count("callback_ack_failed")

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
