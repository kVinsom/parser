"""Create a private server configuration once; never overwrite existing secrets."""

import base64
import os
import secrets
from pathlib import Path


def configure(directory):
    target = directory / ".env"
    template = (directory / ".env.example").read_text(encoding="utf-8")
    content = template.replace(
        "POSTGRES_PASSWORD=\n", "POSTGRES_PASSWORD=" + secrets.token_hex(32) + "\n"
    )
    content = content.replace(
        "WALLET_SESSION_ENCRYPTION_KEY=\n",
        "WALLET_SESSION_ENCRYPTION_KEY="
        + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        + "\n",
    )
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    return target


if __name__ == "__main__":
    try:
        output = configure(Path(__file__).resolve().parent)
    except FileExistsError:
        raise SystemExit("deploy/.env already exists; left unchanged.") from None
    print(f"Created {output}. Set TELEGRAM_BOT_TOKEN privately before starting.")
