"""Run a bounded read-only traffic probe after draining normal source work.

Uses the same durable scheduling journal/guard as safe restart; restores every
saved due time even on failure. No user Start/Stop or search setting changes.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), *(str(ROOT.parent / s) for s in ("telegram-bot", "parser", "notifier"))]
from sqlalchemy import text  # noqa: E402
from vpars_bot.db import database, utcnow  # noqa: E402

from vpars_worker.maintenance import hold, restore  # noqa: E402
from vpars_worker.runtime import settings  # noqa: E402


async def main(args):
    engine, sessions = database(settings().database_url.get_secret_value())
    path = ROOT / ".artifacts/restart-schedule.json"
    script = ROOT.parent / "access-gateway/bypass" / args.script
    try:
        async with engine.connect() as guard:
            if not await guard.scalar(text("SELECT pg_try_advisory_lock(198403, 2)")):
                raise RuntimeError("Another maintenance operation is active")
            if path.exists():
                await restore(sessions, json.loads(path.read_text(encoding="utf-8")))
                path.unlink()
            journal = {"until": (utcnow() + timedelta(minutes=10)).isoformat(),
                       "jobs": {}, "deliveries": {}}
            process = None
            try:
                started = monotonic()
                while await hold(sessions, journal, path):
                    if monotonic() - started > 90:
                        raise TimeoutError("Normal requests did not drain")
                    await asyncio.sleep(0.5)
                pauses = ROOT / ".artifacts/source-cooldowns.json"
                if pauses.exists():
                    until = max((datetime.fromisoformat(v["until"]) for v in
                                 json.loads(pauses.read_text())["regions"].values()),
                                default=utcnow())
                    delay = max(0, (until - utcnow()).total_seconds())
                    if delay > 180:
                        raise RuntimeError("Source pause is too long; diagnosis deferred")
                    print(json.dumps({"drained": True, "cooldown_wait_seconds": round(delay)}), flush=True)
                    await asyncio.sleep(delay)
                if args.script == "verify_cached_content.py":
                    parser_python = ROOT.parent / "parser/.venv" / (
                        "Scripts/python.exe" if sys.platform == "win32" else "bin/python")
                    command = [str(parser_python), str(script)]
                    source = b""
                    environment = {**os.environ, "PYTHONPATH": str(script.parent / ".deps")}
                else:
                    command = ["docker", "exec", "-i", "-u", "0"]
                    for value in args.env:
                        command += ["-e", value]
                    command += ["vinted-access-byparr-1", "/app/.venv/bin/python", "-"]
                    if args.script == "measure_wire.py":
                        command.append("--benchmark")
                    source = script.read_bytes()
                    environment = None
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    env=environment)
                pause_until = monotonic()
                async with asyncio.timeout(240):
                    process.stdin.write(source)
                    await process.stdin.drain()
                    process.stdin.close()
                    while line := await process.stdout.readline():
                        decoded = line.decode("utf-8", errors="replace").rstrip()
                        print(decoded, flush=True)
                        try:
                            row = json.loads(decoded)
                            refused = (row.get("status") in (401, 403, 429)
                                       or row.get("outcome") in ("blocked", "rate_limited")
                                       or row.get("details_outcome") in ("blocked", "rate_limited"))
                            if refused:
                                delay = int(row.get("retry_after") or 120)
                                pause_until = max(pause_until, monotonic() + max(120, delay))
                        except (ValueError, TypeError):
                            pass
                    await process.wait()
                if pause_until > monotonic():
                    delay = pause_until - monotonic()
                    print(json.dumps({"post_probe_source_pause_seconds": round(delay)}), flush=True)
                    # Keep normal work held while the observed source pause elapses.
                    await asyncio.sleep(delay)
                if process.returncode:
                    raise RuntimeError("Diagnosis failed")
            finally:
                if process and process.returncode is None:
                    process.terminate()
                    await process.wait()
                await restore(sessions, journal)
                path.unlink(missing_ok=True)
                print(json.dumps({"schedules_restored": True}), flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("script", choices=["probe_catalog_transport.py", "probe_rsc_cold.py",
                                           "measure_wire.py", "verify_cached_content.py",
                                           "probe_catalog_api.py"])
    parser.add_argument("--env", action="append", default=[])
    asyncio.run(main(parser.parse_args()))
