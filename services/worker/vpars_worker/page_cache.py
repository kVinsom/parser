"""Short-lived reuse across jobs; head/overlap/verification always go to the source.

The durable, complete page audit remains in PostgreSQL's catalog_pages. This
bounded memory cache only avoids re-fetching a recent deep page for another job.
"""

from collections import OrderedDict

from .catchup import scope_key


class PageCache:
    def __init__(self, ttl_seconds=30, max_pages=32):
        self.ttl_seconds, self.max_pages = ttl_seconds, max_pages
        self.pages = OrderedDict()

    def get(self, jobs, now):
        first = jobs[0]
        key = (scope_key(first.query), first.page)
        entry = self.pages.get(key)
        if entry is None:
            return None
        at, result, used = entry
        if not 0 <= (now - at).total_seconds() < self.ttl_seconds:
            self.pages.pop(key, None)
            return None
        for job in jobs:
            state = job.scan_state or {}
            if (
                job.page <= 1
                or not job.baseline_ready
                or job.failures
                or state.get("phase") != "scan"
                or state.get("step") != "forward"
                or job.id in used
                or (job.last_checked and job.last_checked >= at)
            ):
                return None
        used.update(job.id for job in jobs)
        self.pages.move_to_end(key)
        return result.model_copy(deep=True)

    def put(self, request, result, jobs, now):
        if request.page <= 1 or result.completeness != "complete" or not result.identities_complete:
            return
        key = (scope_key(request.model_dump()), request.page)
        self.pages[key] = (now, result.model_copy(deep=True), {job.id for job in jobs})
        self.pages.move_to_end(key)
        while len(self.pages) > self.max_pages:
            self.pages.popitem(last=False)
