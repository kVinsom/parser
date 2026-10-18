"""Atomic, bounded persistence for verified public assets; never page/session data."""
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path


class DiskAssetStore:
    def __init__(self, directory, *, max_bytes=96 * 1024 * 1024, max_entries=512):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.max_entries = max_bytes, max_entries

    @staticmethod
    def name(key):
        return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest() + ".json"

    def files(self):
        return [p for p in self.directory.glob("*.json")
                if len(p.stem) == 64 and all(c in "0123456789abcdef" for c in p.stem)
                and not p.is_symlink()]

    def load(self, now, max_asset_bytes):
        result = []
        self.prune()
        for path in self.files():
            try:
                if path.stat().st_size > max_asset_bytes * 2 + 65536:
                    raise ValueError("Oversized cache entry")
                data = json.loads(path.read_bytes())
                key = tuple(data["key"])
                if (data["version"] != 1 or self.name(key) != path.name
                        or not 0 < data["expires"] - now <= 86400
                        or data["written_at"] > now):
                    raise ValueError("Stale or invalid cache entry")
                body = base64.b64decode(data["body"], validate=True)
                if (not body or len(body) > max_asset_bytes
                        or hashlib.sha256(body).hexdigest() != data["sha256"]):
                    raise ValueError("Corrupt cache entry")
                result.append((key, body, data["headers"], data["expires"] - now))
            except (OSError, ValueError, TypeError, KeyError):
                path.unlink(missing_ok=True)
        return result

    def save(self, key, body, headers, ttl, now):
        data = json.dumps({"version": 1, "key": key, "headers": headers,
                           "written_at": now, "expires": now + ttl,
                           "sha256": hashlib.sha256(body).hexdigest(),
                           "body": base64.b64encode(body).decode("ascii")},
                          separators=(",", ":")).encode()
        if len(data) > self.max_bytes:
            return
        descriptor, temporary = tempfile.mkstemp(prefix="asset-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            os.replace(temporary, self.directory / self.name(key))
            self.prune()
        finally:
            Path(temporary).unlink(missing_ok=True)

    def prune(self):
        files = sorted((p.stat().st_mtime, p.stat().st_size, p) for p in self.files())
        total = sum(size for _, size, _ in files)
        while files and (total > self.max_bytes or len(files) > self.max_entries):
            _, size, path = files.pop(0)
            path.unlink(missing_ok=True)
            total -= size
