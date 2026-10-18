from contextvars import ContextVar
from dataclasses import dataclass

from aiogram.types import Message, Update

SEARCH_START = "✅ Почати пошук"
SEARCH_STOP = "❌ Завершити пошук"
# Previously displayed reply keyboards stay on clients until the next menu response.
SEARCH_START_LABELS = frozenset({SEARCH_START, "▶️ Почати пошук"})
SEARCH_STOP_LABELS = frozenset({SEARCH_STOP, "⏹ Завершити пошук"})


@dataclass(frozen=True)
class UpdateContext:
    update_id: int | None = None
    search_epoch: int | None = None
    stop_key: str | None = None


EMPTY_UPDATE = UpdateContext()  # Frozen dataclass containing only immutable scalar fields.
current_update = ContextVar("telegram_update", default=EMPTY_UPDATE)


def chat_key(update: Update) -> str:
    message = update.message or (update.callback_query and update.callback_query.message)
    return f"chat:{message.chat.id}" if message else f"update:{update.update_id}"


def private_actor(update: Update) -> int | None:
    event = update.callback_query or update.message
    message = update.message or (update.callback_query and update.callback_query.message)
    if (
        isinstance(message, Message)
        and message.chat.type == "private"
        and event.from_user
        and event.from_user.id == message.chat.id
    ):
        return event.from_user.id
    return None


def is_stop(update: Update, *, menu_mode: str = "full") -> bool:
    if menu_mode == "preview" or private_actor(update) is None:
        return False
    if update.message:
        text = update.message.text or ""
        if menu_mode == "menu" and text in SEARCH_STOP_LABELS:
            return True
        # Require a Telegram command entity, never an arbitrary mention/forwarded nickname.
        return bool(
            update.message.entities
            and update.message.entities[0].type == "bot_command"
            and update.message.entities[0].offset == 0
            and text.split()[0].split("@")[0] == "/stop"
        )
    return bool(
        menu_mode == "full"
        and update.callback_query
        and update.callback_query.data
        and len(update.callback_query.data.split(":")) == 2
        and update.callback_query.data.split(":")[1] == "stop"
    )


def is_search_start(update: Update, *, menu_mode: str = "full") -> bool:
    if menu_mode == "preview" or private_actor(update) is None:
        return False
    if menu_mode == "menu":
        return bool(update.message and update.message.text in SEARCH_START_LABELS)
    parts = (update.callback_query.data or "").split(":") if update.callback_query else []
    return len(parts) >= 2 and parts[1] in {"start", "run"}
