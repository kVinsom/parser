import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from vpars_bot.wallet_links import delivery_checkout_url, direct_buy_keyboard
from vpars_bot.wallet_upgrade_buttons import upgraded_keyboard


def test_buy_opens_mini_app_directly_and_keeps_other_buttons():
    old = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Open", url="https://www.vinted.de/items/123"),
        InlineKeyboardButton(text="Buy", callback_data="lot:buy"),
    ]])
    url = delivery_checkout_url("https://checkout.example/", "a" * 64)
    changed = direct_buy_keyboard(old, url)
    assert changed.inline_keyboard[0][0] == old.inline_keyboard[0][0]
    buy = changed.inline_keyboard[0][1]
    assert buy.text == "Buy" and buy.callback_data is None
    assert buy.web_app.url == "https://checkout.example/buy/" + "a" * 64
    assert old.inline_keyboard[0][1].callback_data == "lot:buy"
    assert direct_buy_keyboard(old, None) is old
    assert delivery_checkout_url("", "a" * 64) is None


@pytest.mark.parametrize("origin,reference", [
    ("http://checkout.example", "a" * 64), ("https://x@y", "a" * 64),
    ("https://checkout.example/path", "a" * 64), ("https://checkout.example", "../other"),
    ("https://checkout.example", "a" * 63), ("https://checkout.example", "A" * 64),
])
def test_invalid_checkout_target_rejected(origin, reference):
    with pytest.raises(ValueError):
        delivery_checkout_url(origin, reference)


@pytest.mark.parametrize("host", ["de", "fr", "it", "co.uk"])
@pytest.mark.parametrize("brand", ["stone-island", "amiri"])
def test_old_card_upgrade_preserves_listing_link(host, brand):
    item = f"https://www.vinted.{host}/items/123-{brand}"
    keyboard = upgraded_keyboard(item, "https://checkout.example", "b" * 64)
    assert keyboard.inline_keyboard[0][0].url == item
    assert keyboard.inline_keyboard[0][1].web_app.url.endswith("/buy/" + "b" * 64)


@pytest.mark.parametrize("host", ["de", "fr", "it", "co.uk"])
@pytest.mark.parametrize("brand", ["stone-island", "amiri"])
def test_bot_mode_old_card_keyboard_uses_plain_callback(host, brand):
    item = f"https://www.vinted.{host}/items/123-{brand}"
    keyboard = upgraded_keyboard(item, "", "b" * 64)
    assert keyboard.inline_keyboard[0][0].url == item
    buy = keyboard.inline_keyboard[0][1]
    assert buy.callback_data == "lot:buy" and buy.web_app is None and buy.url is None
