"""Reuse successful direct VPS timezone resolution across browser rotations."""

import threading
import time


class HostGeoCache:
    def __init__(self, prepare, *, ttl=300, clock=time.monotonic):
        self.prepare, self.ttl, self.clock = prepare, ttl, clock
        self.lock = threading.Lock()
        self.value = None
        self.expires = 0

    def resolve(self, timezone):
        # Explicit timezones retain the launcher's existing validation/behavior.
        if timezone and timezone.strip().lower() != "auto":
            return self.prepare(timezone, None)
        with self.lock:
            if self.value is not None and self.clock() < self.expires:
                return self.value
            result = self.prepare(timezone, None)
            # Empty host fallback is not successful geographic evidence. Retry
            # normally next time; never retain a transient lookup failure.
            if isinstance(getattr(result, "timezone", None), str) and result.timezone:
                self.value = result
                self.expires = self.clock() + self.ttl
            return result
