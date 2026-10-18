"""Explicit local bot reset after stopping its processes; secrets never enter output."""

import argparse
import asyncio
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = ROOT / "services/telegram-bot"
sys.path[:0] = [
    str(ROOT / "services" / s) for s in ("telegram-bot", "worker", "parser", "notifier")
]

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from vpars_bot.config import Settings
from vpars_bot.db import Base as BotBase
from vpars_bot.db import RoleEvent, User, database
from vpars_worker.db import Base as WorkerBase


async def reset(backup, expected_bot_id, owner_id, owner_username):
    backup = backup.resolve(strict=True)
    if not backup.is_relative_to((ROOT / ".backups").resolve()):
        raise RuntimeError("Backup must be inside the project backup directory")
    if not (backup / "processes-stopped.json").is_file():
        raise RuntimeError("Stop the verified processes first")
    pending = BOT_ROOT / ".env.reset-pending"
    original = Settings(_env_file=BOT_ROOT / ".env")
    replacement = Settings(_env_file=pending)
    old_id = int(original.telegram_bot_token.get_secret_value().split(":")[0])
    bot = Bot(replacement.telegram_bot_token.get_secret_value())
    engine, _ = database(original.database_url.get_secret_value())
    redis = Redis.from_url(original.redis_url.get_secret_value())
    try:
        me = await bot.get_me()
        if me.id != expected_bot_id or not me.is_bot:
            raise RuntimeError("Unexpected new bot identity")
        url = make_url(original.database_url.get_secret_value())
        if (url.host, url.port, url.database, url.username) != (
            "127.0.0.1",
            15432,
            "vpars_bot",
            "vpars",
        ):
            raise RuntimeError("Only the verified local bot database can be reset")
        if (
            original.database_url != replacement.database_url
            or original.redis_url != replacement.redis_url
        ):
            raise RuntimeError("Pending configuration must use the same local state")
        async with engine.connect() as guard:
            locks = {
                (198403, 2),
                *(
                    (ns, identity % 2147483647)
                    for identity in (old_id, me.id)
                    for ns in (198401, 198403)
                ),
            }
            for ns, key in sorted(locks):
                if not await guard.scalar(
                    text("SELECT pg_try_advisory_lock(:ns,:key)"),
                    {"ns": ns, "key": key},
                ):
                    raise RuntimeError("A bot or worker still owns the database")
            tables = [
                *BotBase.metadata.sorted_tables,
                *(
                    t
                    for t in WorkerBase.metadata.sorted_tables
                    if t.name != "schema_revision"
                ),
            ]
            expected = {(t.schema or "public", t.name) for t in tables} | {
                ("public", "alembic_version"),
                ("vpars_monitor", "schema_revision"),
            }
            actual = set(
                (
                    await guard.execute(
                        text(
                            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema IN ('public','vpars_monitor') AND table_type='BASE TABLE'"
                        )
                    )
                ).all()
            )
            if actual != expected:
                raise RuntimeError("Unrecognized tables; nothing was reset")
            owner = (
                (await guard.execute(select(User.__table__).where(User.id == owner_id)))
                .mappings()
                .one()
            )
            if owner["username_key"] != owner_username.lower() or not owner["is_admin"]:
                raise RuntimeError(
                    "Owner does not match the existing verified administrator"
                )
            counts = {
                t.fullname: await guard.scalar(select(func.count()).select_from(t))
                for t in tables
            }
            # Release table read locks before TRUNCATE on a separate transaction.
            # Session advisory locks continue to prevent bot/worker startup.
            await guard.commit()
            prefix = [
                "docker",
                "compose",
                "-f",
                str(BOT_ROOT / "compose.yaml"),
                "exec",
                "-T",
                "postgres",
            ]
            dump = backup / "database.dump"
            with dump.open("xb") as output:
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        *prefix,
                        "pg_dump",
                        "-U",
                        "vpars",
                        "-d",
                        "vpars_bot",
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            with dump.open("rb") as source:
                inspected = await asyncio.to_thread(
                    subprocess.run,
                    [*prefix, "pg_restore", "--list"],
                    stdin=source,
                    capture_output=True,
                    check=True,
                )
            if (
                b"bot_users" not in inspected.stdout
                or b"sent_lots" not in inspected.stdout
            ):
                raise RuntimeError("Backup verification failed")
            (backup / "private.env").write_bytes((BOT_ROOT / ".env").read_bytes())
            keys = set()
            for identity in (old_id, me.id):
                for pattern in (
                    f"fsm:{identity}:*",
                    f"telegram-budget:{{{identity}}}:*",
                ):
                    keys.update([key async for key in redis.scan_iter(match=pattern)])
            redis_dump = []
            for key in sorted(keys):
                value = await redis.dump(key)
                if value is not None:
                    redis_dump.append(
                        {
                            "key": base64.b64encode(key).decode(),
                            "dump": base64.b64encode(value).decode(),
                            "ttl_ms": await redis.pttl(key),
                        }
                    )
            (backup / "redis.json").write_text(json.dumps(redis_dump), encoding="utf-8")
            with dump.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            (backup / "manifest.json").write_text(
                json.dumps(
                    {
                        "old_bot_id": old_id,
                        "new_bot_id": me.id,
                        "database_sha256": checksum,
                        "before_counts": counts,
                        "redis_keys": len(keys),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                "Database backup verified; clearing authorized application state.",
                flush=True,
            )
            async with engine.begin() as connection:
                names = ", ".join(
                    f'"{t.schema or "public"}"."{t.name}"' for t in tables
                )
                await connection.execute(
                    text("TRUNCATE TABLE " + names + " RESTART IDENTITY")
                )
                await connection.execute(
                    text(
                        "INSERT INTO public.bot_guards (name,bootstrapped) VALUES ('admins',true),('profiles',false)"
                    )
                )
                await connection.execute(
                    User.__table__.insert().values(
                        id=owner_id,
                        chat_id=owner_id,
                        username=owner["username"],
                        username_key=owner_username.lower(),
                        full_name=owner["full_name"],
                        is_admin=True,
                        profile_update_id=-1,
                        delivery_epoch=0,
                        enabled_sources=None,
                    )
                )
                await connection.execute(
                    RoleEvent.__table__.insert().values(
                        operation_id="designated-admin:" + owner_username.lower(),
                        actor_id=None,
                        recipient_id=owner_id,
                        recipient_username=owner["username"],
                        old_role=False,
                        new_role=True,
                    )
                )
                for table in tables:
                    expected_count = {
                        "bot_guards": 2,
                        "bot_users": 1,
                        "bot_role_events": 1,
                    }.get(table.name, 0)
                    if (
                        await connection.scalar(select(func.count()).select_from(table))
                        != expected_count
                    ):
                        raise RuntimeError("Reset verification failed")
            if keys:
                await redis.delete(*keys)
            # Clear remote pending commands for the new bot before enabling polling.
            await bot.delete_webhook(drop_pending_updates=True)
            pending.replace(BOT_ROOT / ".env")
            report = {
                "bot_id": me.id,
                "username": me.username,
                "cleared_tables": len(tables),
                "remaining_users": 1,
                "owner_id": owner_id,
                "owner_username": owner_username,
                "remaining_subscriptions": 0,
                "remaining_searches": 0,
                "remaining_deliveries": 0,
                "remaining_history": 0,
                "only_owner_is_admin": True,
            }
            (backup / "reset-result.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            print(json.dumps(report), flush=True)
    finally:
        await bot.session.close()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--expected-bot-id", type=int, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--owner-username", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(
            reset(args.backup, args.expected_bot_id, args.owner_id, args.owner_username)
        )
    except Exception as error:  # noqa: BLE001 - configuration errors may contain secrets.
        print(
            "Reset stopped: "
            + type(error).__name__
            + "; inspect the completed backup stages. No secrets printed."
        )
        raise SystemExit(1) from None
