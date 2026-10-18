"""Temporary loopback-only Uvicorn process for integration measurements."""

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def parser_server(log_path: Path, environment: dict[str, str] | None = None):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "vpars_parser.api:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
            env={**os.environ, **(environment or {})},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 15
            with httpx.Client(base_url=url, timeout=1, trust_env=False) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Temporary parser server exited during startup")
                    try:
                        if client.get("/health/live").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Temporary parser server did not start")
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
