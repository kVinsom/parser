"""Stream known persistent directories without following links or accepting unsafe paths."""

import argparse
import sys
import tarfile
from pathlib import Path, PurePosixPath

PATHS = {
    "worker": Path("/app/services/worker/.artifacts"),
    "profiles": Path("/app/services/telegram-bot/.wallet-profiles"),
    "contracts": Path("/app/services/telegram-bot/.wallet-contracts"),
}


def allowed(info):
    if not (info.isfile() or info.isdir()):
        return None
    if PurePosixPath(info.name).name.startswith("Singleton"):
        return None  # Chromium's host-specific process markers are not portable state.
    return info


def export(path, stream):
    with tarfile.open(fileobj=stream, mode="w|") as archive:
        archive.add(path, arcname=".", filter=allowed)


def validate(archive):
    for member in archive.getmembers():
        name = PurePosixPath(member.name)
        if (
            name.is_absolute()
            or ".." in name.parts
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError("Unsafe archive member")


def restore(path, stream):
    if any(path.iterdir()):
        raise ValueError("Restore target must be empty")
    # Validate the entire seekable archive before writing any entries.
    with tarfile.open(fileobj=stream, mode="r:*") as archive:
        validate(archive)
        archive.extractall(path, filter="data")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export", "restore", "check-empty"])
    parser.add_argument("state", choices=PATHS)
    args = parser.parse_args()
    try:
        if args.command == "check-empty":
            if any(PATHS[args.state].iterdir()):
                raise ValueError("Restore target must be empty")
        elif args.command == "export":
            export(PATHS[args.state], sys.stdout.buffer)
        else:
            import shutil
            import tempfile

            with tempfile.TemporaryFile() as stream:
                shutil.copyfileobj(sys.stdin.buffer, stream)
                stream.seek(0)
                restore(PATHS[args.state], stream)
    except Exception as error:  # noqa: BLE001 - report no private filenames or values
        print(f"State transfer failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
