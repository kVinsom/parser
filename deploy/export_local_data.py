"""Create a portable local data backup only after the Windows services are stopped."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "services/telegram-bot"
WORKER = ROOT / "services/worker"
sys.path[:0] = [
    str(ROOT / "services" / name)
    for name in ("telegram-bot", "worker", "parser", "notifier")
]


def archive_state(output, source, *, filenames=None):
    try:
        from .state_archive import allowed
    except ImportError:
        from state_archive import allowed

    with tarfile.open(output, "x") as archive:
        if source.exists():
            for entry in sorted(source.iterdir()):
                if filenames is None or entry.name in filenames:
                    archive.add(entry, arcname=entry.name, filter=allowed)


async def export(output, env):
    from manage import file_digest
    from sqlalchemy import text
    from vpars_bot.db import database
    from vpars_worker.runtime import settings

    if (WORKER / ".artifacts/restart-schedule.json").exists():
        raise RuntimeError(
            "Finish or restore the pending maintenance schedule before export"
        )
    if not env.is_file():
        raise RuntimeError("Export the existing private configuration first")
    config = settings()
    engine, _ = database(config.database_url.get_secret_value())
    bot_id = (
        int(config.telegram_bot_token.get_secret_value().split(":", 1)[0]) % 2147483647
    )
    try:
        async with engine.connect() as guard:
            held = []
            try:
                for namespace, key in ((198403, 2), (198401, bot_id), (198403, bot_id)):
                    if not await guard.scalar(
                        text("SELECT pg_try_advisory_lock(:namespace,:key)"),
                        {"namespace": namespace, "key": key},
                    ):
                        raise RuntimeError(
                            "Stop bot, worker and recovery task before export"
                        )
                    held.append((namespace, key))
                # Dump through the local Compose service, not a guessed container name.
                # Refuse a custom DB URL rather than accidentally exporting another DB.
                from sqlalchemy.engine import make_url

                url = make_url(config.database_url.get_secret_value())
                if (
                    url.host not in {"localhost", "127.0.0.1"}
                    or url.port != 15432
                    or url.database != "vpars_bot"
                    or url.username != "vpars"
                ):
                    raise RuntimeError(
                        "Custom database: export it using its verified deployment configuration"
                    )
                output.mkdir(parents=True, exist_ok=False, mode=0o700)
                with (output / "database.dump").open("xb") as stream:
                    process = await asyncio.create_subprocess_exec(
                        "docker",
                        "compose",
                        "-f",
                        str(BOT / "compose.yaml"),
                        "exec",
                        "-T",
                        "postgres",
                        "pg_dump",
                        "-U",
                        "vpars",
                        "-d",
                        "vpars_bot",
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                        stdout=stream,
                    )
                    if await process.wait():
                        raise RuntimeError("Database export failed")
                archive_state(
                    output / "worker.tar",
                    WORKER / ".artifacts",
                    filenames={"source-cooldowns.json"},
                )
                archive_state(output / "profiles.tar", BOT / ".wallet-profiles")
                archive_state(output / "contracts.tar", BOT / ".wallet-contracts")
                shutil.copyfile(env, output / "private.env")
                files = {entry.name: file_digest(entry) for entry in output.iterdir()}
                (output / "manifest.json").write_text(
                    json.dumps({"version": 1, "sha256": files}, indent=2),
                    encoding="utf-8",
                )
            finally:
                for namespace, key in reversed(held):
                    await guard.execute(
                        text("SELECT pg_advisory_unlock(:namespace,:key)"),
                        {"namespace": namespace, "key": key},
                    )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        asyncio.run(export(args.output, args.env_file))
    except Exception as error:  # noqa: BLE001 - no private DB errors in console
        raise SystemExit(
            f"Local backup failed ({type(error).__name__}); check stopped services, source database and output directory."
        ) from None
    print(
        "Portable data backup completed. Keep the local bot stopped during the final transfer."
    )
