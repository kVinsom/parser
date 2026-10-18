"""Passive Linux/container traffic counter. Stores byte counts, never payloads.

Pipe into `docker exec -i -u 0 ... python - --seconds 180`. Does not start any
upstream request. Internal POST /v1 starts are only an approximate denominator:
in-flight work spans window boundaries and successes/errors are mixed together.
"""

import argparse
import ipaddress
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def measure(seconds, stop=None, ready=None, benchmark_counts=None, progress=None):
    counters = {"external_in": 0, "external_out": 0,
                "tcp_payload_in": 0, "tcp_payload_out": 0}
    starts = set()
    started = datetime.now(timezone.utc).isoformat()
    tick = time.monotonic()
    deadline = tick + seconds
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as capture:
        capture.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        capture.bind(("eth0", 0))
        capture.settimeout(0.5)
        if ready:
            ready.set()
        while time.monotonic() < deadline and not (stop and stop.is_set()):
            try:
                packet = capture.recv(65535)
            except TimeoutError:
                continue
            if len(packet) < 34 or packet[12:14] != b"\x08\x00":
                continue  # Current deployment uses IPv4; do not count IPv6 as zero.
            ip = packet[14:]
            header_size = (ip[0] & 15) * 4
            size = int.from_bytes(ip[2:4], "big")
            src, dst = ipaddress.ip_address(ip[12:16]), ipaddress.ip_address(ip[16:20])
            direction = "in" if src.is_global else "out" if dst.is_global else None
            if direction:
                counters["external_" + direction] += size
                if progress is not None:
                    progress["external_total"] = counters["external_in"] + counters["external_out"]
            if ip[9] != 6 or len(ip) < header_size + 20:
                continue
            tcp = ip[header_size:]
            tcp_header = (tcp[12] >> 4) * 4
            payload = tcp[tcp_header:]
            if direction:
                counters["tcp_payload_" + direction] += max(0, size - header_size - tcp_header)
            elif int.from_bytes(tcp[2:4], "big") == 8191 and payload.startswith(b"POST /v1 HTTP/"):
                starts.add((ip[12:16], tcp[:8]))
        packets, dropped = struct.unpack("II", capture.getsockopt(263, 6, 8))
    total = counters["external_in"] + counters["external_out"]
    print(json.dumps({"started_utc": started, "seconds": seconds, **counters,
                      "elapsed_seconds": time.monotonic() - tick,
                      "external_total": total, "v1_starts": len(starts),
                      "bytes_per_start": total / len(starts) if starts else None,
                      "capture_packets": packets, "capture_dropped": dropped,
                      "benchmark": benchmark_counts,
                      "scope": "IPv4 external IP bytes; mixed browser traffic; not a proxy bill"}), flush=True)


def benchmark():
    """Eight read-only catalog checks; no Telegram sends or saved-search changes."""
    import re
    import sys
    sys.path.insert(0, "/opt/vinted")
    from vinted import CatalogResult, classify
    from vinted_regions import get_region

    stop, ready = threading.Event(), threading.Event()
    counts = {"attempts": 0, "page_loaded": 0}
    progress = {"external_total": 0}
    collector = threading.Thread(target=measure, args=(300, stop, ready, counts, progress), daemon=True)
    collector.start()
    if not ready.wait(5):
        raise RuntimeError("Packet counter did not start")
    paused = set()
    try:
        rounds = max(1, min(4, int(os.environ.get("BENCH_ROUNDS", "2"))))
        page_number = max(1, min(3, int(os.environ.get("BENCH_PAGE", "1"))))
        for code, brand in [("DE", "Nike"), ("FR", "Adidas"),
                            ("IT", "Stone Island"), ("UK", "Amiri")] * rounds:
            if code in paused:
                continue
            region = get_region(code)
            url = region.catalog_url(brand, page_number, True)
            payload = json.dumps({"cmd": "request.get", "url": url,
                                  "maxTimeout": 20000, "blockMedia": True,
                                  "returnOnlyCookies": False}).encode()
            started = time.monotonic()
            wire_start = progress["external_total"]
            counts["attempts"] += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:8191/v1", data=payload,
                    headers={"Content-Type": "application/json"}), timeout=30) as response:
                    headers = response.headers
                    solution = json.load(response)["solution"]
                result = classify(CatalogResult(code, url), region, solution["url"],
                                  int(headers.get("X-Vinted-Upstream-Status", solution["status"])),
                                  solution["response"])
                if result.outcome in ("blocked", "rate_limited"):
                    paused.add(code)
                if result.outcome == "page_loaded":
                    counts["page_loaded"] += 1
                print(json.dumps({"region": code, "brand": brand, "outcome": result.outcome,
                                  "page": page_number,
                                  "wire_bytes_during_call": progress["external_total"] - wire_start,
                                  "elapsed_seconds": time.monotonic() - started,
                                  "catalog_cards": len(set(re.findall(
                                      r'data-testid="product-item-id-([0-9]+)"', result.body))),
                                  "cache": headers.get("X-Vinted-Static-Cache", "unreported"),
                                  "transport": headers.get("X-Vinted-Catalog-Transport", "unreported"),
                                  "retry_after": headers.get("X-Vinted-Retry-After"),
                                  "skip_analytics": headers.get("X-Vinted-Skip-Analytics", "unreported"),
                                  "cache_hits": headers.get("X-Vinted-Static-Cache-Hits")}), flush=True)
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
                paused.add(code)
                print(json.dumps({"region": code, "error": type(exc).__name__}), flush=True)
            time.sleep(4)
    finally:
        stop.set()
        collector.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args()
    if not 10 <= args.seconds <= 600:
        parser.error("seconds must be between 10 and 600")
    if args.benchmark:
        benchmark()
    else:
        measure(args.seconds)
