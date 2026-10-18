import asyncio
import re
from dataclasses import replace
from decimal import Decimal
from html import unescape

import pytest

from vpars_bot.formatting import (
    Conversion,
    Lot,
    UnavailableCurrencyConverter,
    format_lot,
    optional_conversion,
    safe_url,
)


@pytest.fixture
def lot():
    return Lot(
        title="Nike Air Max 90",
        brand="Nike",
        price=Decimal("45"),
        currency="EUR",
        source_label="Vinted",
        region="FR",
        url="https://www.vinted.fr/items/123-example",
        search_name="Air Max 90",
        size="EU 42",
        condition="Very good",
        color="Black",
    )


async def test_original_price_and_missing_attributes(lot):
    conversion = await optional_conversion(UnavailableCurrencyConverter(), lot.price, lot.currency)
    assert conversion is None
    msg = format_lot(replace(lot, size=None, color=None, condition=None), conversion)
    assert "Price: 45.00 €" in msg.text and "≈" not in msg.text
    assert "N/A" in msg.text and "None" not in msg.text and msg.photo_url is None
    assert len(msg.keyboard.inline_keyboard) == 1
    open_button, buy_button = msg.keyboard.inline_keyboard[0]
    assert (open_button.text, open_button.url) == ("Open", lot.url)
    assert (buy_button.text, buy_button.callback_data) == ("Soon", "lot:buy")


@pytest.mark.parametrize("mode", ["failure", "timeout", "none", "wrong_currency", "nan"])
async def test_converter_failure_cannot_hide_original(lot, mode):
    class Converter:
        async def to_uah(self, amount, currency):
            if mode == "failure":
                raise RuntimeError("offline")
            if mode == "timeout":
                await asyncio.sleep(1)
            if mode == "none":
                return None
            if mode == "wrong_currency":
                return Conversion(Decimal("1000"), "EUR")
            return Conversion(Decimal("NaN"))

    converted = await optional_conversion(Converter(), lot.price, lot.currency, timeout=0.01)
    assert converted is None
    assert "45.00" in format_lot(lot, converted).text


def test_converter_demo_photo_and_html_caption_budget(lot):
    changed = replace(
        lot,
        title="😀<&>" * 300,
        brand="<&>😀" * 200,
        search_name="<&>😀" * 200,
        size="<&>😀" * 200,
        condition="<&>😀" * 200,
        color="<&>😀" * 200,
        photo_url="https://images.example.com/demo.jpg",
    )
    msg = format_lot(changed, Conversion(Decimal("2000.25")), demo=True)
    assert "Demo — example data" in msg.text
    assert "≈ 2000.25 UAH" in msg.text
    assert "&lt;&amp;&gt;" in msg.text and msg.photo_url == changed.photo_url
    visible = unescape(re.sub(r"</?b>", "", msg.text))
    assert len(visible.encode("utf-16-le")) // 2 < 1024


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "https://", "http://a b", "https://user:pass@host", "https://[", None],
)
def test_bad_urls_are_not_buttons_or_photos(url, lot):
    assert safe_url(url) is None
    msg = format_lot(replace(lot, url=url or "", photo_url=url))
    assert not msg.keyboard.inline_keyboard and msg.photo_url is None


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1"])
def test_invalid_original_price(amount, lot):
    with pytest.raises(ValueError):
        format_lot(replace(lot, price=Decimal(amount)))


@pytest.mark.parametrize("photo,broken", [(False, False), (True, False), (True, True)])
async def test_photo_caption_and_text_fallback(photo, broken, lot):
    from unittest.mock import AsyncMock

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendRichMessage

    from vpars_bot.lot_messages import prepare_lot, send_lot_message

    card = await prepare_lot(
        replace(lot, photo_url="https://images.example.com/photo.jpg" if photo else None)
    )
    bot = AsyncMock()
    if broken:
        bot.send_rich_message.side_effect = [
            TelegramBadRequest(
                method=SendRichMessage(chat_id=101, rich_message=card.rich_message),
                message="image unavailable",
            ),
            True,
        ]
    await send_lot_message(bot, 101, card)
    calls = bot.send_rich_message.call_args_list
    assert len(calls) == (2 if broken else 1)
    assert (calls[0].kwargs["rich_message"].blocks[0].type == "photo") == photo
    for call in calls:
        assert call.kwargs["reply_markup"] == card.keyboard
        assert any(b.type == "paragraph" for b in call.kwargs["rich_message"].blocks)
    if broken:
        assert calls[-1].kwargs["rich_message"].blocks[0].type == "paragraph"
    bot.send_photo.assert_not_called()
    bot.send_message.assert_not_called()


def test_caption_budget_with_all_astral_characters(lot):
    huge = "😀" * 2000
    card = format_lot(
        replace(
            lot,
            title=huge,
            brand=huge,
            search_name=huge,
            size=huge,
            condition=huge,
            color=huge,
            source_label=huge,
            region=huge,
            seller_name=huge,
            seller_rating=4.9,
            seller_feedback_count=2147483647,
        ),
        demo=True,
    )
    visible = unescape(re.sub(r"</?b>", "", card.text))
    assert len(visible.encode("utf-16-le")) // 2 < 1024


def test_seller_rating_count_without_account_name(lot):
    card = format_lot(
        replace(
            lot,
            seller_name="seller <&>",
            seller_rating=4.9,
            seller_feedback_count=190,
        )
    )
    assert "seller" not in card.text and "Продавець" not in card.text
    assert "⭐️⭐️⭐️⭐️☆ (190)" in card.text and "4.9/5" in card.text
    assert "Vinted.Fr" in card.text and "Very Good" in card.text
    assert lot.title not in card.text and lot.search_name not in card.text


def test_absent_seller_data_is_not_zero_reviews_or_zero_stars(lot):
    card = format_lot(lot)
    assert "N/A (N/A)" in card.text
    assert "Продавець:" not in card.text
    card = format_lot(replace(lot, seller_rating=0, seller_feedback_count=0))
    assert "N/A (0)" in card.text and "⭐" not in card.text


@pytest.mark.parametrize(
    "region,condition,expected",
    [
        ("GB", "New without tags", "New Without Tags"),
        ("FR", "Très bon état", "Very Good"),
        ("DE", "Neu", "New"),
        ("IT", "Ottime", "Very Good"),
        ("DE", "unverified condition", "N/A"),
    ],
)
def test_english_card_and_market_regions(lot, region, condition, expected):
    card = format_lot(replace(lot, region=region, condition=condition))
    assert expected in card.text
    assert f"Vinted.{('UK' if region == 'GB' else region).title()}" in card.text
    assert "Vinted.Gb" not in card.text


def test_requested_card_layout(lot):
    card = format_lot(
        replace(
            lot,
            brand="Stone Island",
            region="DE",
            price=Decimal("144.5"),
            size="S",
            condition="Good",
            seller_rating=5,
            seller_feedback_count=255,
        )
    )
    paragraph = card.rich_message.blocks[0]
    assert paragraph.type == "paragraph"
    parts = paragraph.text
    assert parts[1::2] == ["\n"] * 5
    assert all(part.type == "bold" and part.text[1].type == "italic" for part in parts[::2])
    assert [part.text[0] + part.text[1].text for part in parts[::2]] == [
        "👀Market: Vinted.De",
        "🥇Brand: Stone Island",
        "📏Size: S",
        "😇Condition: Good",
        "💰Price: 144.50 €",
        "✨Reviews: ⭐️⭐️⭐️⭐️⭐️ (255)",
    ]


@pytest.mark.parametrize(
    ("currency", "symbol"),
    [("EUR", "€"), ("GBP", "£"), ("USD", "$"), ("UAH", "₴"), ("PLN", "zł"), ("CHF", "CHF")],
)
def test_price_keeps_original_amount(lot, currency, symbol):
    card = format_lot(replace(lot, currency=currency))
    assert f"Price: 45.00 {symbol}</i>" in card.text
    assert card.rich_message.blocks[0].text[8].text[1].text == f"Price: 45.00 {symbol}"


async def test_development_example_is_explicit(chat):
    await chat.send("/example")
    assert "Demo — example data" in chat.transport.calls[-1].rich_message.blocks[0].text
    before = await (await chat.state()).get_data()
    count = len(chat.transport.calls)
    await chat.click(data="lot:buy")
    after = await (await chat.state()).get_data()
    # Buy may explain missing setup, but a demo never becomes a purchase or changes FSM.
    before.pop("render_update_id", None)
    after.pop("render_update_id", None)
    assert after == before
    assert [call.__api_method__ for call in chat.transport.calls[count:]] == [
        "answerCallbackQuery", "sendMessage"
    ]
    assert chat.transport.calls[-1].text == "Soon"


async def test_gallery_card_and_buttons_serialize_as_one_api_request(lot):
    import json

    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from vpars_bot.lot_messages import rich_lot_message

    calls = []

    async def receive(request):
        calls.append((request.match_info["method"], dict(await request.post())))
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "message_id": 900,
                    "date": 1,
                    "chat": {"id": 101, "type": "private"},
                },
            }
        )

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", receive)
    async with TestServer(app) as server:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(str(server.make_url("")).rstrip("/"))
        )
        bot = Bot("42:offline", session=session)
        try:
            card = format_lot(lot)
            photos = [f"https://images.example.com/{i}.jpg" for i in range(12)]
            result = await bot.send_rich_message(
                101,
                rich_message=rich_lot_message(card, [photos[0], *photos]),
                reply_markup=card.keyboard,
            )
            assert result.message_id == 900
        finally:
            await session.close()
    assert len(calls) == 1 and calls[0][0] == "sendRichMessage"
    payload = calls[0][1]
    message = json.loads(payload["rich_message"])
    assert [b["type"] for b in message["blocks"]] == ["collage", "paragraph"]
    assert [b["photo"]["media"] for b in message["blocks"][0]["blocks"]] == photos[:10]
    assert message["blocks"][1]["text"].count("\n") == 5
    buttons = json.loads(payload["reply_markup"])["inline_keyboard"]
    assert [[b["text"] for b in row] for row in buttons] == [["Open", "Soon"]]
    assert buttons[0][0]["url"] == lot.url and buttons[0][1]["callback_data"] == "lot:buy"
