"""Build a source release from explicit runtime/test inputs, without local secrets."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {"README.md", ".gitignore", "start.ps1", "AGENTS.md"}
DEPLOY_FILES = {
    "Dockerfile",
    "Dockerfile.byparr",
    "compose.yaml",
    "compose.handoff.yaml",
    "Caddyfile",
    "configure.py",
    "service.py",
    "manage.py",
    "state_archive.py",
    "export_local_secrets.py",
    "export_local_data.py",
    "stop_local.ps1",
    "README.md",
    "seccomp-playwright.json",
    "LICENSE.playwright",
    "THIRD_PARTY.md",
}
SUFFIXES = {".py", ".ps1", ".toml", ".txt", ".lock", ".yaml", ".ini", ".mako", ".proto"}
SERVICES = {"telegram-bot", "worker", "notifier", "parser", "access-gateway"}
POLICY = "services/worker/publication_policy.json"


def included(path):
    parts = path.parts
    name = path.name
    if any(
        part.startswith(".")
        or part in {"__pycache__", "dist", "build"}
        or part.endswith(".egg-info")
        for part in parts
    ):
        return name in {".gitignore", ".dockerignore", ".env.example"} and not any(
            part.startswith(".") for part in parts[:-1]
        )
    if len(parts) == 1:
        return name in TOP_FILES
    if parts[0] in {"tools", "tests"}:
        return path.suffix in {".py", ".ps1"}
    if parts[0] == "shared":
        return path.suffix in {".py", ".toml"}
    if parts[0] == "docs":
        return name in {
            "RELEASE.md",
            "SHARED_CATALOG.md",
            "RELEASE_AUDIT_2026-09-29.md",
            "RELEASE_AUDIT_2026-10-13.md",
            "BOT_BUY_2026-10-12.md",
            "AUTOBUY.md",
            "DEPLOYMENT.md",
        }
    if parts[0] == "deploy":
        return len(parts) == 2 and name in DEPLOY_FILES
    if parts[0] != "services" or len(parts) < 3 or parts[1] not in SERVICES:
        return False
    if name == "README.md" or "docs" in parts:
        return path.suffix == ".md"
    if "fixtures" in parts:
        return path.suffix in {".html", ".json", ".md"}
    if parts[:4] == ("services", "telegram-bot", "vpars_bot", "handoff_assets"):
        return path.suffix in {".html", ".css", ".js"}
    if path.as_posix() in {POLICY, "services/telegram-bot/config/sources.json"}:
        return True
    return path.suffix in SUFFIXES or name.startswith("Dockerfile")


def collect(root):
    # Prune private directories before walking them; never follow directory links.
    import os

    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            d
            for d in directories
            if not (
                d.startswith(".")
                or d in {"__pycache__", "dist", "build"}
                or d.endswith(".egg-info")
                or (Path(directory) / d).is_symlink()
                or (Path(directory) / d).is_junction()
            )
        )
        for name in sorted(files):
            full = Path(directory) / name
            relative = full.relative_to(root)
            if full.is_symlink() or not included(relative):
                continue
            # The publication rule is global, with no personal Telegram IDs.
            data = (
                b'{"applies_to": "all_users", "max_age_seconds": 60}\n'
                if relative.as_posix() == POLICY
                else full.read_bytes()
            )
            yield relative.as_posix(), data


def build(root, output):
    files = list(collect(root))
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Do not overwrite a previously verified release.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr(name, data)
        archive.writestr("MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "dist/vpars-0.1.0-rc1.zip"
    )
    args = parser.parse_args()
    count = build(ROOT, args.output)
    print(f"Created {args.output.resolve()} ({count} files)")
