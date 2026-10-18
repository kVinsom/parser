"""Bounded, process-local latency telemetry; no user IDs, payloads or credentials."""

from collections import defaultdict, deque
from math import ceil
from statistics import median


class LatencyMetrics:
    def __init__(self, window=512):
        self.samples = defaultdict(lambda: deque(maxlen=window))
        self.counters = defaultdict(int)

    def observe(self, name, seconds):
        self.samples[name].append(max(0, seconds) * 1000)

    def count(self, name):
        self.counters[name] += 1

    def snapshot(self):
        return {
            "counters": dict(self.counters),
            "milliseconds": {
                name: {
                    "samples": len(values),
                    "p50": round(median(values), 2),
                    "p95": round(sorted(values)[ceil(len(values) * 0.95) - 1], 2),
                    "max": round(max(values), 2),
                }
                for name, values in self.samples.items()
                if values
            },
        }
