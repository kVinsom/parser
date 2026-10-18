"""Reusable Telegram presentation helpers, with no queue, scheduler or recipient selection."""

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputMediaPhoto,
    InputRichBlockCollage,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichMessage,
)

from .formatting import (
    CurrencyConverter,
    Lot,
    LotMessage,
    UnavailableCurrencyConverter,
    format_lot,
    optional_conversion,
    safe_url,
)

MAX_PHOTOS_PER_LOT = 10


def rich_lot_message(card: LotMessage, photos: list[str], *, notices=(), photo_ids=None) -> InputRichMessage:
    """One gallery, six-line card and notices in one Telegram message."""
    photos = list(dict.fromkeys(url for url in photos if safe_url(url)))[:MAX_PHOTOS_PER_LOT]
    media = [InputRichBlockPhoto(photo=InputMediaPhoto(media=(photo_ids or {}).get(url, url)))
             for url in photos]
    blocks = []
    if media:
        blocks.append(InputRichBlockCollage(blocks=media) if len(media) > 1 else media[0])
    blocks.extend(card.rich_message.blocks or [])
    blocks.extend(InputRichBlockParagraph(text=notice) for notice in notices)
    return InputRichMessage(blocks=blocks, skip_entity_detection=True)


async def prepare_lot(lot: Lot, converter: CurrencyConverter | None = None, *, demo=False):
    conversion = await optional_conversion(
        converter or UnavailableCurrencyConverter(), lot.price, lot.currency
    )
    return format_lot(lot, conversion, demo=demo)


async def send_lot_message(bot: Bot, chat_id: int, card: LotMessage):
    """Caller must check delivery eligibility immediately before calling this helper."""
    if card.photo_url:
        try:
            return await bot.send_rich_message(
                chat_id,
                rich_message=rich_lot_message(card, [card.photo_url]),
                reply_markup=card.keyboard,
            )
        except TelegramBadRequest:
            # A missing/unusable image must not suppress the original-price text card.
            pass
    return await bot.send_rich_message(
        chat_id,
        rich_message=rich_lot_message(card, []),
        reply_markup=card.keyboard,
    )
