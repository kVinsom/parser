"""Export existing local secrets to a private server env file; never print values."""

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "services/telegram-bot"
sys.path.insert(0, str(BOT))


def quote(value):
    # Compose single-quoted env values are literal, including $ and #.
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("Unsupported multiline secret")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def export(output):
    from cryptography.fernet import Fernet
    from dotenv import dotenv_values
    from vpars_bot.session_secrets import dpapi

    config = dotenv_values(BOT / ".env", interpolate=False)
    token = config.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("LocalBotTokenMissing")
    key = config.get("WALLET_SESSION_ENCRYPTION_KEY")
    if not key:
        key_path = BOT / ".wallet-secrets/master.dpapi"
        if key_path.exists():
            if os.name != "nt":
                raise RuntimeError("ExportWithOriginalWindowsAccount")
            key = dpapi(key_path.read_bytes(), decrypt=True).decode("ascii")
        else:
            raise RuntimeError(
                "ExistingKeyMissing: do not generate a replacement for saved sessions"
            )
    Fernet(key.encode())
    proxy = dotenv_values(
        ROOT / "services/access-gateway/bypass/.env", interpolate=False
    )
    values = {
        "TELEGRAM_BOT_TOKEN": token,
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "WALLET_SESSION_ENCRYPTION_KEY": key,
        "WALLET_CHECKOUT_MODE": config.get("WALLET_CHECKOUT_MODE") or "automatic",
        "MENU_MODE": config.get("MENU_MODE") or "menu",
        "WORKER_INTERVAL": "4",
        "WORKER_PAGES": "3",
        "VPARS_IMAGE_TAG": "server",
        **{
            name: proxy.get(name) or ""
            for name in ("PROXY_SERVER", "PROXY_USERNAME", "PROXY_PASSWORD")
        },
    }
    if config.get("WALLET_HANDOFF_URL"):
        values["WALLET_HANDOFF_URL"] = config["WALLET_HANDOFF_URL"]
    content = "# PRIVATE: transfer separately from the source archive.\n" + "".join(
        f"{name}={quote(value)}\n" for name, value in values.items()
    )
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        export(args.output)
    except Exception as error:  # noqa: BLE001 - never expose exported credentials
        raise SystemExit(
            f"Export failed ({type(error).__name__}); no secrets printed. Check source key and output path."
        ) from None
    print(
        "Private server configuration exported. Keep it outside the source archive; restrict file access."
    )
