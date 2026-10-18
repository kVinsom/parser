"""Server operations. Does not change saved filters, roles or Start/Stop intent."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent
ROOT = DEPLOY.parent
STATE = {"worker": "worker", "profiles": "bot", "contracts": "bot"}


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Server:
    def __init__(self, env, project=None, handoff=False):
        self.env = env.resolve()
        self.prefix = [
            "docker",
            "compose",
            "--env-file",
            str(self.env),
            "-f",
            str(DEPLOY / "compose.yaml"),
        ]
        if handoff:
            self.prefix += ["-f", str(DEPLOY / "compose.handoff.yaml")]
        if project:
            self.prefix += ["-p", project]

    def call(self, *args, **options):
        return subprocess.run([*self.prefix, *args], cwd=ROOT, check=True, **options)

    def running(self):
        return set(
            self.call(
                "ps",
                "--status",
                "running",
                "--services",
                capture_output=True,
                text=True,
            ).stdout.split()
        )

    def database_empty(self):
        if self.running() & {"bot", "worker"}:
            raise RuntimeError("Stop bot and worker before importing data")
        self.call("up", "-d", "--wait", "postgres", "redis")
        value = self.call(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "vpars",
            "-d",
            "vpars_bot",
            "-Atc",
            "SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('public','vpars_monitor')",
            capture_output=True,
            text=True,
        ).stdout.strip()
        if value != "0":
            raise RuntimeError(
                "Restore is allowed only into an empty database; existing data left unchanged"
            )

    def import_database(self, dump):
        self.database_empty()
        with dump.open("rb") as stream:
            self.call(
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "vpars",
                "-d",
                "vpars_bot",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                stdin=stream,
            )

    def backup(self, directory):
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        active = sorted(self.running() & {"bot", "worker"})
        try:
            if active:
                self.call("stop", *active)
            with (directory / "database.dump").open("xb") as stream:
                self.call(
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
            for kind, service in STATE.items():
                with (directory / f"{kind}.tar").open("xb") as stream:
                    self.call(
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "--entrypoint",
                        "python",
                        service,
                        "/app/deploy/state_archive.py",
                        "export",
                        kind,
                        stdout=stream,
                    )
            shutil.copyfile(self.env, directory / "private.env")
            manifest = {p.name: file_digest(p) for p in directory.iterdir()}
            (directory / "manifest.json").write_text(
                json.dumps({"version": 1, "sha256": manifest}, indent=2),
                encoding="utf-8",
            )
            print(
                f"Backup completed: {directory}. Copy it to separate protected storage."
            )
        finally:
            if active:
                self.call("start", *active)

    def restore(self, directory):
        try:
            from .state_archive import validate
        except ImportError:
            from state_archive import validate
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        required = {"database.dump", "private.env", *(f"{kind}.tar" for kind in STATE)}
        if manifest.get("version") != 1 or set(manifest.get("sha256", {})) != required:
            raise ValueError("Invalid backup manifest")
        for name, digest in manifest["sha256"].items():
            if file_digest(directory / name) != digest:
                raise ValueError("Backup checksum mismatch")
        if self.env.read_bytes() != (directory / "private.env").read_bytes():
            raise ValueError(
                "Use the backup private.env as deploy/.env before restoring; preserve its encryption key"
            )
        for kind in STATE:
            with tarfile.open(directory / f"{kind}.tar") as archive:
                validate(archive)
        self.database_empty()
        for kind, service in STATE.items():
            self.call(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python",
                service,
                "/app/deploy/state_archive.py",
                "check-empty",
                kind,
            )
        self.import_database(directory / "database.dump")
        for kind, service in STATE.items():
            with (directory / f"{kind}.tar").open("rb") as stream:
                self.call(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--entrypoint",
                    "python",
                    service,
                    "/app/deploy/state_archive.py",
                    "restore",
                    kind,
                    stdin=stream,
                )
        print("Restore completed. Run start when the previous installation is stopped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEPLOY / ".env")
    parser.add_argument("--project")
    parser.add_argument(
        "--handoff", action="store_true", help="Enable configured HTTPS manual checkout"
    )
    parser.add_argument(
        "command",
        choices=[
            "check",
            "build",
            "start",
            "stop",
            "status",
            "logs",
            "backup",
            "restore",
            "import-db",
        ],
    )
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    server = Server(args.env_file, args.project, args.handoff)
    try:
        if args.command == "check":
            server.call("config", "--quiet")
        elif args.command == "build":
            server.call("build", "bot", "gateway", "byparr")
        elif args.command == "start":
            server.call("up", "-d", "--build", "--wait", "--wait-timeout", "300")
        elif args.command == "stop":
            server.call("stop", "bot", "worker")
        elif args.command == "status":
            server.call("ps", "--all")
        elif args.command == "logs":
            server.call("logs", "--tail", "100", "bot", "worker", "parser", "gateway")
        elif args.command == "backup":
            server.backup(
                args.path
                or ROOT / ".backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            )
        elif args.command in {"restore", "import-db"}:
            if not args.path:
                parser.error("A backup directory or dump file is required")
            if args.command == "restore":
                server.restore(args.path)
            else:
                server.import_database(args.path)
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"Docker operation failed (exit {error.returncode})."
        ) from None
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
