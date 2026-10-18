"""Export a new source-only directory suitable for a private or public Git repository."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

try:
    from tools.build_release import ROOT, collect
except ModuleNotFoundError:
    from build_release import ROOT, collect

PATTERNS = (
    re.compile(rb"\b[0-9]{7,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}\b"),
)


def env_values(path):
    if not path.is_file():
        return []
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip().startswith("#"):
            continue
        value = value.strip().strip("\"'")
        if re.search(r"TOKEN|SECRET|PASSWORD|USERNAME|ENCRYPTION|ADMIN.*ID", key, re.I):
            values.append(value)
        if "URL" in key and "://" in value:
            try:
                url = urlsplit(value)
                values.extend([url.username or "", url.password or ""])
            except ValueError:
                pass
    return values


def known_credentials(root):
    parents = [root, root / "deploy"] + [root / "services" / service for service in
        ("telegram-bot", "worker", "parser", "access-gateway/bypass")]
    examples = {v for parent in parents for v in env_values(parent / ".env.example")}
    return {v.encode() for parent in parents for v in env_values(parent / ".env")
            if len(v) >= 8 and v not in examples and v not in {"change_me", "changeme"}}


def public_document(name, data):
    # Operational reports can contain personal history without any passwords.
    if name.startswith("docs/RELEASE_AUDIT_") or name == "docs/BOT_BUY_2026-10-12.md":
        return ("# Historical verification report\n\n"
                "Private operational history is omitted from this source export.\n"
                "See [README](../README.md) and [shared catalogs](SHARED_CATALOG.md) "
                "for current installation and test information.\n").encode()
    if name == "docs/AUTOBUY.md":
        return ("# Autobuy\n\n"
                "Autobuy is disabled in this release; its actions return Soon.\n"
                "The implementation is retained in services/telegram-bot/vpars_bot.\n"
                "Private checkout sessions, account history and operational reports "
                "are not included in this source export.\n").encode()
    if name == "services/telegram-bot/docs/ADMINS.md":
        return ("# Administrator permissions\n\n"
                "Administrator roles and ownership are stored in the database.\n"
                "This source export contains no personal role assignments or production "
                "Telegram IDs. Configure the initial administrator using the documented "
                "bootstrap command for your installation; see [README](../README.md).\n").encode()
    return data


def export(root, output):
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError("Export destination must be new; existing files were left unchanged")
    files = [(name, public_document(name, data)) for name, data in collect(root)]
    credentials = known_credentials(root)
    blocked = [name for name, data in files
               if any(value in data for value in credentials) or any(p.search(data) for p in PATTERNS)]
    if blocked:
        raise ValueError("Potential credentials in source files (values hidden): " + ", ".join(blocked))
    # Validate the entire payload before creating any output.
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, data in files:
        target = output / name
        assert target.resolve().is_relative_to(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    (output / "MANIFEST.json").write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(f"Exported {export(ROOT, args.output)} checked source files to {args.output.resolve()}")
