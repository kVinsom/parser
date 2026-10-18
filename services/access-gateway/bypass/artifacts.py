"""Compressed page snapshots; raw HTML remains available to callers in memory."""

import gzip
from pathlib import Path


def save_html(path: Path, body: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=3, newline="") as stream:
        stream.write(body)


def read_html(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        return stream.read()
