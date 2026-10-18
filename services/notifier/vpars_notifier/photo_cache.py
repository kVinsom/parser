"""Reuse only confirmed Telegram photos for the exact source URL and bot.

This is optional transport metadata, never delivery history or eligibility.
The original URLs stay in the delivery and are used if this cache fails.
"""
import asyncio
import hashlib
import logging
import re
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from vpars_bot.db import utcnow
from vpars_worker.db import TelegramPhoto

logger = logging.getLogger("vpars.photo_cache")
FILE_ID = re.compile(r"[A-Za-z0-9_-]{10,1024}\Z")


def url_hash(url):
    return hashlib.sha256(url.encode()).hexdigest()


def confirmed_photos(message, urls):
    rich = getattr(message, "rich_message", None)
    blocks = getattr(rich, "blocks", None)
    if not blocks or not urls or len(urls) > 10:
        return {}
    gallery = blocks[0]
    photos = gallery.blocks if gallery.type == "collage" else [gallery]
    if len(photos) != len(urls) or any(block.type != "photo" for block in photos):
        return {}
    result = {}
    for url, block in zip(urls, photos, strict=True):
        if not block.photo:
            return {}
        largest = max(block.photo, key=lambda p: p.width * p.height)
        if not isinstance(largest.file_id, str) or not FILE_ID.fullmatch(largest.file_id):
            return {}
        result[url] = largest.file_id
    return result


class PhotoCache:
    def __init__(self, sessions, bot_id, *, max_entries=20000, timeout=0.2):
        self.sessions = sessions
        self.bot_id = bot_id if type(bot_id) is int and bot_id > 0 else None
        self.max_entries, self.timeout = max_entries, timeout

    async def resolve(self, urls):
        if not self.bot_id or not urls:
            return {}
        try:
            async with asyncio.timeout(self.timeout), self.sessions() as session:
                hashes = {url_hash(url): url for url in urls}
                rows = await session.scalars(select(TelegramPhoto).where(
                    TelegramPhoto.bot_id == self.bot_id,
                    TelegramPhoto.url_hash.in_(hashes), TelegramPhoto.expires_at > utcnow()))
                return {hashes[row.url_hash]: row.file_id for row in rows
                        if FILE_ID.fullmatch(row.file_id)}
        except Exception as exc:  # noqa: BLE001 -- optional cache must never stop delivery
            logger.warning("Photo cache lookup skipped (%s)", type(exc).__name__)
            return {}

    async def remember(self, message, urls):
        if not self.bot_id:
            return
        try:
            mapping = confirmed_photos(message, urls)
            if not mapping:
                return
            async with asyncio.timeout(self.timeout), self.sessions.begin() as session:
                expires = utcnow() + timedelta(days=30)
                statement = insert(TelegramPhoto).values([
                    {"bot_id": self.bot_id, "url_hash": url_hash(url),
                     "file_id": file_id, "expires_at": expires} for url, file_id in mapping.items()])
                await session.execute(statement.on_conflict_do_update(
                    index_elements=[TelegramPhoto.bot_id, TelegramPhoto.url_hash],
                    set_={"file_id": statement.excluded.file_id, "expires_at": expires}))
                # Bound persistent metadata; never touch sent_lots or deliveries.
                overflow = select(TelegramPhoto.url_hash).where(
                    TelegramPhoto.bot_id == self.bot_id).order_by(
                    TelegramPhoto.expires_at.desc(), TelegramPhoto.url_hash).offset(self.max_entries)
                await session.execute(delete(TelegramPhoto).where(
                    TelegramPhoto.bot_id == self.bot_id, TelegramPhoto.url_hash.in_(overflow)))
            logger.info("Telegram photo references cached count=%s", len(mapping))
        except Exception as exc:  # noqa: BLE001 -- optional cache must never stop delivery
            logger.warning("Photo cache write skipped (%s)", type(exc).__name__)

    async def invalidate(self, mapping):
        if not self.bot_id or not mapping:
            return
        try:
            async with asyncio.timeout(self.timeout), self.sessions.begin() as session:
                for url, file_id in mapping.items():
                    await session.execute(delete(TelegramPhoto).where(
                        TelegramPhoto.bot_id == self.bot_id,
                        TelegramPhoto.url_hash == url_hash(url), TelegramPhoto.file_id == file_id))
        except Exception as exc:  # noqa: BLE001 -- optional cache must never stop delivery
            logger.warning("Photo cache invalidation skipped (%s)", type(exc).__name__)
