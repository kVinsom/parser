from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage, SendRichMessage
from aiohttp import ClientConnectorError
from redis.exceptions import ConnectionError as RedisConnectionError
from test_monitoring import Bot, Parser, due, lot, rich_text, rows, start
from vpars_bot.domain import Filters
from vpars_bot.latency import LatencyMetrics
from vpars_bot.transport import TelegramRequestPolicy
from vpars_notifier.service import Notifier
from vpars_parser.models import Attribute

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor
from vpars_worker.parser import ParseFailure


async def branded_delivery(environment, brand, user, **changes):
    store, catalog = environment
    if user != 101:
        await catalog.register(user, user, "second", "Second", 1, start=True)
    await catalog.save(user, Filters(brands=[brand], sources={"vinted_fr": ["FR"]}), "save")
    await store.control.start(user, "start")
    parser = Parser(items=[lot("999", title=brand, brand=Attribute(raw=brand, normalized=brand))])
    monitor = Monitor(store, parser)
    await monitor.once()
    await due(store, Job)
    parser.items = [lot(title=brand, brand=Attribute(raw=brand, normalized=brand), **changes)]
    await monitor.once()


@pytest.mark.parametrize("brand,user", [("Stone Island", 101), ("Amiri", 202)])
@pytest.mark.parametrize("code", ["GatewayTimeout", "SourceRateLimited"])
@pytest.mark.parametrize("complete", [True, False])
async def test_optional_details_failure_does_not_expire_verified_young_lot(
    environment, code, complete, brand, user
):
    store, _ = environment
    await branded_delivery(environment, brand, user, photos_complete=complete)

    class Details:
        calls = 0

        async def details(self, item):
            self.calls += 1
            raise ParseFailure(code, retry_after=120 if code == "SourceRateLimited" else None)

    parser, bot = Details(), Bot()
    notifier = Notifier(store, bot, parser=parser)
    await notifier.once(lane="prepare")
    assert await notifier.once(lane="ready")
    assert parser.calls == 1
    assert len(bot.calls) == 1
    assert (await rows(store, Delivery))[0].state == "sent"
    assert "N/A (N/A)" in rich_text(bot.calls[0][2])
    assert bot.calls[0][1] == user


async def test_failed_connection_before_http_request_can_retry_without_becoming_unknown(
    environment,
):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    bot = Bot()
    error = TelegramNetworkError(
        method=SendMessage(chat_id=101, text="x"), message="connect failed"
    )
    error.__cause__ = ClientConnectorError(
        SimpleNamespace(host="api.telegram.org", port=443, ssl=True), OSError("refused")
    )
    bot.error = error
    await Notifier(store, bot).once()
    row = (await rows(store, Delivery))[0]
    assert row.state == "pending" and row.uncertain == 0
    assert not row.media_progress.get("transport_started")
    bot.error = None
    await due(store, Delivery)
    await Notifier(store, bot).once()
    assert (await rows(store, Delivery))[0].state == "sent"


@pytest.mark.parametrize("requirement", ["age", "description"])
async def test_failed_required_details_still_prevent_delivery(environment, requirement):
    store, catalog = environment
    await catalog.save(
        101,
        Filters(
            brands=["Stone Island"],
            sources={"vinted_fr": ["FR"]},
            excluded_keywords=["damage"] if requirement == "description" else [],
        ),
        "save",
    )
    await store.control.start(101, "start")
    monitor = Monitor(store, Parser(items=[lot("999")]))
    await monitor.once()
    await due(store, Job)
    item = lot(description=None)
    if requirement == "age":
        item.published_at = None
    monitor.parser.items = [item]
    await monitor.once()

    class Details:
        async def details(self, item):
            raise ParseFailure("SourceRateLimited", retry_after=120)

    bot = Bot()
    await Notifier(store, bot, parser=Details()).once(lane="prepare")
    row = (await rows(store, Delivery))[0]
    assert not bot.calls and not row.media_progress.get("dispatch_ready")


async def test_rich_mapper_failure_before_request_does_not_make_delivery_uncertain(
    environment, monkeypatch
):
    store, _ = environment
    await start(environment)
    await Monitor(store, Parser()).once()
    bot = Bot()

    def broken(*args, **kwargs):
        raise ValueError("invalid gallery")

    monkeypatch.setattr("vpars_notifier.service.rich_lot_message", broken)
    with pytest.raises(ValueError):
        await Notifier(store, bot).once()
    row = (await rows(store, Delivery))[0]
    assert row.state == "pending" and row.uncertain == 0
    assert not bot.calls and not row.media_progress.get("transport_started")


@pytest.mark.parametrize("brand,user", [("Stone Island", 101), ("Amiri", 202)])
async def test_shared_budget_outage_keeps_unsent_lot_retryable(environment, brand, user):
    store, _ = environment
    await branded_delivery(environment, brand, user)
    budget = AsyncMock()
    budget.acquire.side_effect = RedisConnectionError("unavailable")
    policy = TelegramRequestPolicy(budget, LatencyMetrics())

    class BudgetedBot(Bot):
        async def send_rich_message(self, chat_id, **kwargs):
            async def request(bot, method):
                return await super(BudgetedBot, self).send_rich_message(chat_id, **kwargs)

            payload = {key: value for key, value in kwargs.items() if key != "request_timeout"}
            return await policy(request, self, SendRichMessage(chat_id=chat_id, **payload))

    bot = BudgetedBot()
    notifier = Notifier(store, bot)
    await notifier.once()
    row = (await rows(store, Delivery))[0]
    assert row.state == "pending" and row.uncertain == 0
    assert not row.media_progress.get("transport_started") and not bot.calls
    budget.acquire.side_effect = None
    await due(store, Delivery)
    await notifier.once()
    assert (await rows(store, Delivery))[0].state == "sent"
    assert len(bot.calls) == 1 and bot.calls[0][1] == user
