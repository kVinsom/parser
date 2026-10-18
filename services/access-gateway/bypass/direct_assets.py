"""Fetch only public immutable interface assets, directly, without proxy credentials."""

import asyncio
import time
import urllib.request
import zlib
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.error import HTTPError

from static_cache import MIME_TYPES, asset_key, lifetime

MAX_WIRE_BYTES = 512 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class DirectResponse:
    url: str
    headers: dict
    data: bytes
    status: int = 200

    async def body(self):
        return self.data

    async def dispose(self):
        pass


def fetch_asset(request, max_decoded_bytes, *, opener=None, clock=time.monotonic):
    if asset_key(request) is None:
        raise ValueError("Not an eligible public asset")
    # Explicitly ignore HTTP(S)_PROXY and never attach cookies or authorization.
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    headers = {"Accept-Encoding": "gzip", "Accept": "*/*"}
    for name in ("origin", "user-agent"):
        if request.headers.get(name):
            headers[name] = request.headers[name]
    started = clock()
    try:
        reply = opener.open(urllib.request.Request(request.url, headers=headers), timeout=3)
    except HTTPError as exc:
        exc.close()
        raise
    with reply:
        response_headers = {k.lower(): v for k, v in reply.headers.items()}
        mime = response_headers.get("content-type", "").partition(";")[0].strip().lower()
        if (reply.status != 200 or reply.geturl() != request.url
                or lifetime(response_headers, time.time()) <= 0
                or mime not in MIME_TYPES[request.resource_type]):
            raise ValueError("Not a public immutable asset response")
        length = response_headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_WIRE_BYTES):
            raise ValueError("Direct asset exceeds transfer limit")
        encoding = response_headers.get("content-encoding", "identity").lower()
        if encoding not in {"identity", "gzip"}:
            raise ValueError("Unsupported direct asset encoding")
        chunks, received = [], 0
        while True:
            if clock() - started >= 8:
                raise TimeoutError("Direct asset deadline exceeded")
            chunk = reply.read1(min(65536, MAX_WIRE_BYTES - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_WIRE_BYTES:
                raise ValueError("Direct asset exceeds transfer limit")
            chunks.append(chunk)
    body = b"".join(chunks)
    if length is not None and received != int(length):
        raise ValueError("Incomplete direct asset")
    if encoding == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = decoder.decompress(body, max_decoded_bytes + 1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Incomplete or oversized compressed asset")
    if not body or len(body) > max_decoded_bytes:
        raise ValueError("Direct asset exceeds decoded limit")
    clean = {k: v for k, v in response_headers.items()
             if k not in {"content-encoding", "content-length", "transfer-encoding"}}
    return DirectResponse(request.url, clean, body)


class DirectAssetFetcher:
    async def __call__(self, request, max_decoded_bytes):
        snapshot = SimpleNamespace(url=request.url, method=request.method,
                                   resource_type=request.resource_type, headers=dict(request.headers))
        return await asyncio.to_thread(fetch_asset, snapshot, max_decoded_bytes)
