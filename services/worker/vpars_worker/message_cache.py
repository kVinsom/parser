"""Persistent notification identity, independent of filter and Vinted country."""

import hashlib
import json


def message_key(user_id: int, item: dict) -> str:
    namespace = "vinted" if item["source_id"] == "vinted" else item["source_namespace"]
    identity = [user_id, item["source_id"], namespace, item["source_item_id"]]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def cache_values(user_id, item, delivery_id, message_id, sent_at):
    return {
        "id": message_key(user_id, item),
        "user_id": user_id,
        "source_id": item["source_id"],
        "source_item_id": item["source_item_id"],
        "delivery_id": delivery_id,
        "message_id": message_id,
        "sent_at": sent_at,
    }


def message_lock_key(key: str) -> int:
    # One-argument advisory locks have a separate namespace from receiver locks.
    return int.from_bytes(bytes.fromhex(key[:16]), byteorder="big", signed=True)
