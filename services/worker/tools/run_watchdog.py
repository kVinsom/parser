"""Run with pythonw.exe so scheduled recovery never opens a Windows console."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT.parent / "telegram-bot/.venv/Scripts/python.exe"


def main():
    artifacts = ROOT / ".artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    # pythonw has no standard streams; give the checker real log handles.
    with (
        (artifacts / "watchdog-stdout.log").open("w", encoding="utf-8") as stdout,
        (artifacts / "watchdog.log").open("w", encoding="utf-8") as stderr,
    ):
        try:
            result = subprocess.run(
                [str(PYTHON), str(ROOT / "main.py"), "watchdog"],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                check=False,
            )
        except OSError as error:
            # Preserve the cause type without exposing configuration or secrets.
            print(f"Worker recovery launcher unavailable ({type(error).__name__})", file=stderr)
            return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
