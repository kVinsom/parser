"""Commit-time wakeups; durable database queues remain the source of truth."""

import asyncio
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary


@dataclass
class Signals:
    wallet: asyncio.Event = field(default_factory=asyncio.Event)
    notifications: asyncio.Event = field(default_factory=asyncio.Event)


_signals = WeakKeyDictionary()


def signals(sessions):
    if sessions not in _signals:
        _signals[sessions] = Signals()
    return _signals[sessions]


async def wait_for_work(event, timeout=0.5):
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except TimeoutError:
        pass  # Cross-process writes/restarts still get picked up by the durable queue.
