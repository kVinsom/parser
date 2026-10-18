"""Pace catalog and detail starts together without holding up unrelated regions."""

import asyncio
import logging
from time import monotonic

RECOVERY_SECONDS = 600

logger = logging.getLogger("vpars.source_rate")


class SourcePacer:
    def __init__(self, *, interval=1.0, clock=monotonic, sleep=asyncio.sleep):
        self.interval = interval
        self.clock, self.sleep = clock, sleep
        self.lock = asyncio.Lock()
        self.next_start = 0.0
        self.regions = {}
        self.next_region = {}
        self.recover_after = {}
        self.last_start = {}

    async def acquire(self, region, check_cooldown):
        recovered = False
        while True:
            check_cooldown(region)
            async with self.lock:
                recovered = self.recover(region) or recovered
                now = self.clock()
                delay = max(self.next_start, self.next_region.get(region, 0)) - now
                if delay <= 0:
                    self.next_start = now + self.interval
                    self.last_start[region] = now
                    self.next_region[region] = now + self.regions.get(region, self.interval)
                    return recovered
            # No lock while waiting: a slow region cannot stall the other regions.
            # Recheck cooldown after each wait; a concurrent refusal may arrive.
            await self.sleep(min(delay, 1.0))

    def limited(self, region):
        self.regions[region] = min(16.0, max(4.0, self.regions.get(region, 1.0) * 2))
        self.recover_after[region] = self.clock() + RECOVERY_SECONDS
        if region in self.last_start:
            self.next_region[region] = self.last_start[region] + self.regions[region]

    def recover(self, region):
        """Decay one step per quiet window, including time spent stopped.

        A learned interval is separate from an explicit source cooldown. The
        caller must check that cooldown before allowing recovery/admission.
        Keeping the original deadline lets old checkpoints and short runs
        accumulate progress without counting responses or restarting timers.
        """
        deadline = self.recover_after.get(region)
        now = self.clock()
        if deadline is None or now < deadline:
            return False
        old = interval = self.regions.get(region, self.interval)
        while interval > self.interval and now >= deadline:
            interval = max(self.interval, interval / 2)
            deadline += RECOVERY_SECONDS
        if interval == old:
            return False
        if interval > self.interval:
            self.regions[region] = interval
            self.recover_after[region] = deadline
        else:
            self.regions.pop(region, None)
            self.recover_after.pop(region, None)
        if region in self.last_start:
            self.next_region[region] = self.last_start[region] + interval
        logger.info("Source pacing recovered region=%s previous_interval=%s interval=%s", region, old, interval)
        return True

    def succeeded(self, region):
        return self.recover(region)
