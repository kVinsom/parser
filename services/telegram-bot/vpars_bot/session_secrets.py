"""Encrypt session material before it reaches the durable Telegram inbox."""

import ctypes
import json
import os
import re
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

TOKEN_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}")
SESSION_INPUT = "[Сесійний ключ приховано]"
incoming_session_key = ContextVar("incoming_session_key", default=None)


def dpapi(value, *, decrypt=False):
    # Windows protects the encryption key for the service's OS user, not each Telegram user.
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source, output = Blob(len(value), buffer), Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError("SessionEncryptionUnavailable")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class SessionCipher:
    def __init__(self, key):
        self.fernet = Fernet(key)

    def seal(self, value, *, owner, purpose):
        return self.fernet.encrypt(
            json.dumps(
                {
                    "owner": owner,
                    "purpose": purpose,
                    "value": value,
                },
                separators=(",", ":"),
            ).encode()
        ).decode()

    def open(self, value, *, owner, purpose):
        try:
            content = json.loads(self.fernet.decrypt(value.encode()))
            if content["owner"] != owner or content["purpose"] != purpose:
                raise ValueError
            return content["value"]
        except (InvalidToken, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("SessionSecretUnavailable") from exc


@lru_cache(maxsize=1)
def session_cipher():
    from .config import Settings

    configured = Settings().wallet_session_encryption_key.get_secret_value()
    if configured:
        return SessionCipher(configured.encode())
    if os.name != "nt":
        raise RuntimeError("ConfigureWalletSessionEncryptionKey")
    path = Path(__file__).resolve().parents[1] / ".wallet-secrets" / "master.dpapi"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Exclusive creation: a second process must use the winner's key.
        protected = dpapi(Fernet.generate_key())
        try:
            with path.open("xb") as stream:
                stream.write(protected)
        except FileExistsError:
            pass
    return SessionCipher(dpapi(path.read_bytes(), decrypt=True))


def scrub(value):
    if isinstance(value, str):
        return TOKEN_PATTERN.sub(SESSION_INPUT, value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value


def private_session_key(message):
    if message is None or message.chat.type != "private":
        return None
    value = message.text or ""
    found = TOKEN_PATTERN.search(value)
    return found.group() if found else None


def protected_payload(update, bot_id):
    from . import autobuy_availability as autobuy

    payload = scrub(update.model_dump(mode="json", exclude_none=True))
    secret = private_session_key(update.message)
    if secret:
        owner = update.message.from_user.id
        if owner == update.message.chat.id:
            if autobuy.AVAILABLE:
                payload["_wallet_secret"] = session_cipher().seal(
                    secret, owner=owner, purpose=f"telegram:{bot_id}:{update.update_id}"
                )
            # Do not preserve any accompanying free text or entity offsets from a secret.
            payload["message"]["text"] = SESSION_INPUT
            payload["message"].pop("entities", None)
    return payload
