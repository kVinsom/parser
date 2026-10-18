"""Replace Buy in already delivered private cards, preserving content and history.

Dry run is the default. --apply updates only Telegram reply markup. Progress is
local and resumable per HTTPS origin; no delivery/user/search rows are changed.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import text

from . import autobuy_availability as autobuy
from .config import Settings
from .db import database
from .transport import RedisTelegramBudget, TelegramDeferred
from .wallet import WalletError, listing_identity
from .wallet_links import delivery_checkout_url, direct_buy_keyboard


def upgraded_keyboard(item_url, base_url, delivery_id):
    listing_identity(item_url)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Open", url=item_url),
        InlineKeyboardButton(text="Buy", callback_data="lot:buy"),
    ]])
    return direct_buy_keyboard(keyboard, delivery_checkout_url(base_url, delivery_id))


async def upgrade(settings, *, apply=False):
    origin = settings.checkout_window_url
    if autobuy.AVAILABLE and not origin and settings.wallet_checkout_mode != "bot":
        raise ValueError("Configure the checkout HTTPS origin before upgrading buttons")
    if origin:
        delivery_checkout_url(origin, "a" * 64)
    engine, sessions = database(settings.database_url.get_secret_value())
    checkpoint = Path(".artifacts") / ("buy-buttons-" + hashlib.sha256(
        ((origin or "bot-callback-v1") if autobuy.AVAILABLE else "soon-v1").encode()).hexdigest()[:16] + ".json")
    done = json.loads(checkpoint.read_text("utf-8")) if checkpoint.exists() else {}
    counts = {"eligible": 0, "updated": 0, "already_done": 0, "unavailable": 0, "failed": 0}
    bot = Bot(settings.telegram_bot_token.get_secret_value()) if apply else None
    redis = Redis.from_url(settings.redis_url.get_secret_value()) if apply else None
    budget = RedisTelegramBudget(redis, bot.id) if apply else None
    try:
        async with sessions() as session:
            rows = (await session.execute(text(
                "SELECT d.id, d.user_id, d.message_id, d.item->>'item_url' AS url "
                "FROM vpars_monitor.deliveries d JOIN bot_users u ON u.id=d.user_id "
                "WHERE d.state='sent' AND d.message_id IS NOT NULL AND u.chat_id=d.user_id "
                "ORDER BY d.sent_at DESC, d.id"
            ))).mappings().all()
        for row in rows:
            try:
                keyboard = upgraded_keyboard(row["url"], origin, row["id"])
            except (WalletError, ValueError, TypeError):
                continue
            counts["eligible"] += 1
            key = f"{row['id']}:{row['message_id']}"
            if key in done:
                counts["already_done"] += 1
                continue
            if not apply:
                continue
            outcome = None
            while outcome is None:
                try:
                    await budget.acquire(row["user_id"])
                    await bot.edit_message_reply_markup(
                        row["user_id"], row["message_id"], reply_markup=keyboard,
                        request_timeout=10,
                    )
                    outcome = "updated"
                except TelegramDeferred as exc:
                    await asyncio.sleep(min(exc.retry_after, 30))
                except TelegramRetryAfter as exc:
                    await budget.cooldown(exc.retry_after)
                except TelegramBadRequest as exc:
                    if "message is not modified" in exc.message.lower():
                        outcome = "updated"
                    elif any(s in exc.message.lower() for s in (
                        "message to edit not found", "message can't be edited", "chat not found",
                    )):
                        outcome = "unavailable"
                    else:
                        raise  # Invalid endpoint/markup is a deployment error, never skip silently.
                except TelegramForbiddenError:
                    outcome = "unavailable"
            counts[outcome] += 1
            done[key] = outcome
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            temporary = checkpoint.with_suffix(".tmp")
            temporary.write_text(json.dumps(done), encoding="utf-8")
            temporary.replace(checkpoint)
            if (counts["updated"] + counts["unavailable"]) % 50 == 0:
                print(json.dumps(counts), flush=True)
        print(json.dumps(counts), flush=True)
    finally:
        if bot:
            await bot.session.close()
        if redis:
            await redis.aclose()
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--base-url", help="Override configured HTTPS origin")
    args = parser.parse_args()
    settings = Settings()
    if args.base_url:
        settings.wallet_handoff_url = args.base_url
    asyncio.run(upgrade(settings, apply=args.apply))


if __name__ == "__main__":
    main()
