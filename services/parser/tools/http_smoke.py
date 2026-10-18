"""Start a temporary HTTP server and check one real GB search/page through gRPC."""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    folder = ROOT / ".artifacts"
    folder.mkdir(exist_ok=True)
    with (folder / "http-smoke-server.log").open("w", encoding="utf-8") as log:
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
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=95, trust_env=False
            ) as client:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        client.get("/health/live").raise_for_status()
                        break
                    except httpx.ConnectError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Temporary parser server did not start") from None
                        time.sleep(0.1)
                client.get("/health/ready").raise_for_status()
                disabled = client.post("/v1/catalog/parse", json={"region_id": "PL"})
                assert disabled.status_code == 422
                response = client.post(
                    "/v1/catalog/parse",
                    json={
                        "region_id": "GB",
                        "search_text": "nike",
                        "page": 2,
                        "request_id": "live-http-smoke",
                        "filters": {
                            "brands": ["Nike", "Adidas"],
                            "models": ["Air Max 90", "Air Force 1"],
                            "price": {"maximum": "100.00", "currency": "GBP"},
                        },
                    },
                )
                response.raise_for_status()
                result = response.json()
                assert result["region_id"] == "UK" and result["items"]
                assert result["request_id"] == response.headers["x-request-id"] == "live-http-smoke"
                report = {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "kind": "live_http_to_grpc",
                    "request": {"region": "GB", "search_text": "nike", "page": 2},
                    "region": result["region_id"],
                    "status": response.status_code,
                    "items": len(result["items"]),
                    "completeness": result["completeness"],
                    "cards_skipped": result["cards_skipped"],
                    "pagination": result["pagination"],
                    "applied_filters": result["applied_filters"],
                    "residual_filters": result["residual_filters"],
                    "disabled_PL_status": disabled.status_code,
                    "timings": result["timings"],
                }
                (folder / "http-smoke.json").write_text(
                    json.dumps(report, indent=2) + "\n", encoding="utf-8"
                )
                print(json.dumps(report, indent=2))
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    main()
