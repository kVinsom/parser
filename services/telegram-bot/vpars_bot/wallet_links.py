"""Direct Mini App buttons; a delivery reference never grants account access."""

import re
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from . import autobuy_availability as autobuy


def delivery_checkout_url(base_url, delivery_id):
    if not autobuy.AVAILABLE:
        return None
    if not base_url:
        return None
    target = urlsplit(base_url)
    if (target.scheme != "https" or not target.hostname or target.username
            or target.password or target.port not in {None, 443}
            or target.path not in {"", "/"} or target.query or target.fragment
            or not re.fullmatch(r"[0-9a-f]{64}", delivery_id)):
        raise ValueError("Invalid checkout delivery link")
    return f"{base_url.rstrip('/')}/buy/{delivery_id}"


def direct_buy_keyboard(keyboard, checkout_url):
    if not autobuy.AVAILABLE:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=autobuy.SOON, callback_data="lot:buy")
            if button.callback_data == "lot:buy" else button
            for button in row
        ] for row in keyboard.inline_keyboard])
    if not checkout_url:
        return keyboard
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button.text, web_app=WebAppInfo(url=checkout_url))
        if button.callback_data == "lot:buy" else button
        for button in row
    ] for row in keyboard.inline_keyboard])
