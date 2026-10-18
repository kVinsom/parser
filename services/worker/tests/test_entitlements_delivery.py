from datetime import timedelta

from test_monitoring import Bot, Parser, due, lot, rows, start
from vpars_bot.db import SearchRun, Subscription, utcnow
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor


async def test_expired_subscription_stops_fetch_and_pending_delivery(environment):
    store, _ = environment
    await start(environment)
    parser = Parser(items=[lot()])
    await Monitor(store, parser).once()
    assert len(await rows(store, Delivery)) == 1
    async with store.sessions.begin() as session:
        (await session.get(Subscription, 101)).expires_at = utcnow() - timedelta(seconds=1)
    bot = Bot()
    await Notifier(store, bot).once()
    saved = (await rows(store, Delivery))[0]
    assert not bot.calls and saved.last_error == "SubscriptionRequired"
    await due(store, Job)
    await Monitor(store, parser).once()
    assert len(parser.calls) == 1
    assert (await rows(store, SearchRun))[0].state == "paused_limit"
