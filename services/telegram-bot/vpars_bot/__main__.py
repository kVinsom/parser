import argparse
import asyncio
import logging
import sys

from aiogram import Bot

from .application import AdminService
from .config import Settings
from .db import database
from .domain import UserError
from .runtime import run
from .telegram import TelegramProfileVerifier


async def bootstrap(settings, recipient_id, confirm_id):
    if not settings.telegram_bot_token.get_secret_value():
        raise UserError("Налаштуй TELEGRAM_BOT_TOKEN у локальному .env.")
    engine, sessions = database(settings.database_url.get_secret_value())
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        async with sessions() as session:
            from sqlalchemy import select

            from .db import User

            user = await session.scalar(
                select(User).where(User.id == recipient_id, User.username_key == "kv1nsom")
            )
        if user is None:
            raise UserError("Потрібен справжній /start від @kV1nsom та ID із /id.")
        await TelegramProfileVerifier(bot).verify(user.id, "kv1nsom")
        print(f"Отримувач: @{user.username}; Telegram ID: {user.id}; ім’я: {user.full_name}")
        if confirm_id != recipient_id:
            print(
                "Перевір отримувача. Для одноразового призначення повтори команду з --confirm-id та цим самим ID."
            )
            return
        await AdminService(sessions, TelegramProfileVerifier(bot)).bootstrap(recipient_id)
        print("Першого адміністратора призначено. Повтори /start у боті.")
    finally:
        await bot.session.close()
        await engine.dispose()


def main():
    from .supervision import supervise

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Vpars Telegram bot")
    sub = parser.add_subparsers(dest="command")
    admin = sub.add_parser(
        "bootstrap-admin", help="Verify and appoint the first @kV1nsom administrator once"
    )
    admin.add_argument("--telegram-id", required=True, type=int)
    admin.add_argument("--confirm-id", type=int)
    sub.add_parser("run", help="Run configured polling or webhook receiver")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # Third-party tracebacks/HTTP logging can contain URLs or user message payloads.
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    logging.getLogger("vpars_bot").setLevel(logging.INFO)
    try:
        settings = Settings()
        asyncio.run(
            bootstrap(settings, args.telegram_id, args.confirm_id)
            if args.command == "bootstrap-admin"
            else supervise(lambda: run(settings))
        )
    except KeyboardInterrupt:
        pass
    except UserError as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception as exc:
        # Configuration validation may include input values; never print raw errors.
        parser.exit(
            1,
            f"Не вдалося запустити ({type(exc).__name__}). Перевір .env, міграції, PostgreSQL і Redis.\n",
        )


if __name__ == "__main__":
    main()
