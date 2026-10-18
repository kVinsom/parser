import pytest
from sqlalchemy import select
from test_monitoring import Bot, Parser, lot, rows, start
from vpars_bot.config import Settings
from vpars_bot.domain import UserError
from vpars_bot.wallet_handoff import HandoffService
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery
from vpars_worker.monitor import Monitor


@pytest.mark.parametrize("user", [101, 202])
@pytest.mark.parametrize("filter_key", ["saved-filter", "new-filter"])
async def test_delivery_is_soon_even_with_old_url_and_reference_is_owner_checked(environment, user, filter_key):
    store, catalog = environment
    if user == 202:
        await catalog.register(202, 202, "bob", "Test", 2, start=True)
    await start(environment, user=user, key=filter_key)
    await Monitor(store, Parser(items=[lot()])).once()
    bot = Bot()
    assert await Notifier(store, bot, handoff_url="https://checkout.example").once()
    sent = (await rows(store, Delivery))[0]
    assert sent.state == "sent" and sent.message_id == 501
    button = bot.calls[-1][2]["reply_markup"].inline_keyboard[0][1]
    assert button.text == "Soon" and button.callback_data == "lot:buy"
    assert button.web_app is None and button.url is None
    service = HandoffService(store.sessions, "https://checkout.example")
    assert await service.delivered_message(user, sent.id) == sent.message_id
    with pytest.raises(UserError):
        await service.delivered_message(202 if user == 101 else 101, sent.id)
    async with store.sessions.begin() as session:
        delivery = await session.scalar(select(Delivery).where(Delivery.id == sent.id))
        delivery.state = "pending"
    with pytest.raises(UserError):
        await service.delivered_message(user, sent.id)


@pytest.mark.parametrize("user", [101, 202])
@pytest.mark.parametrize("filter_key", ["saved-filter", "new-filter"])
async def test_bot_mode_delivers_plain_buy_even_with_previous_window_url(environment, user, filter_key):
    store, catalog = environment
    if user == 202:
        await catalog.register(202, 202, "bob", "Test", 2, start=True)
    await start(environment, user=user, key=filter_key)
    await Monitor(store, Parser(items=[lot()])).once()
    settings = Settings(_env_file=None, wallet_checkout_mode="bot", wallet_handoff_url="https://old.example")
    bot = Bot()
    assert await Notifier(store, bot, handoff_url=settings.checkout_window_url).once()
    button = bot.calls[-1][2]["reply_markup"].inline_keyboard[0][1]
    assert button.text == "Soon"
    assert button.callback_data == "lot:buy" and button.web_app is None and button.url is None
