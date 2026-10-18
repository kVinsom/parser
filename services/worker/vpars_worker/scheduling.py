"""Prioritize fresh inventories while retaining a fair, durable catch-up lane."""

from datetime import UTC, datetime, timedelta

from .catchup import refresh_head_if_due

EPOCH = datetime.min.replace(tzinfo=UTC)


def head_timeout(jobs, configured=45):
    """Retain a timed-out query's learned budget briefly, including after success."""
    failures = max((j.scheduling or {}).get("head_timeouts", 0) for j in jobs)
    # Cold regional HTML regularly takes 15-18 seconds. A 12-second cancellation
    # discarded that paid bootstrap and forced another request before any head.
    # A first baseline also pays for the lazy browser launch and its first tab.
    # Cancelling that paid bootstrap forced another full HTML request and left
    # less byte credit for every user's later, much smaller catalog refreshes.
    cold = any(not getattr(job, 'baseline_ready', True) and not getattr(job, 'checks', 1)
               for job in jobs)
    budget = 40 if cold else 20 if failures == 0 else 24
    now = datetime.now(UTC)
    for job in jobs:
        schedule = job.scheduling or {}
        if timestamp(schedule.get("head_budget_until")) > now:
            budget = max(budget, min(24, schedule.get("head_budget_seconds", 20)))
    return min(configured, budget)


def timestamp(value):
    return datetime.fromisoformat(value) if value else EPOCH


def last_head(job):
    schedule = job.scheduling or {}
    scan = job.scan_state or {}
    return timestamp(schedule.get("head_attempt_at") or scan.get("head_checked_at"))


def choose(jobs, now, head_streak, head_seconds=8):
    """One head response serves every local filter, irrespective of deep cursor.

    At most four consecutive priority head requests precede a due continuation;
    an unfinished initial baseline may continue after one head request.
    Failed deep offsets have their own cooldown; source 429 cooldowns are still
    enforced by Job.next_due and the parser's regional cooldown before this call.
    """
    groups = {}
    for job in jobs:
        groups.setdefault(job.query_key, []).append(job)
    heads = []
    deep = []
    for peers in groups.values():
        checked = max(last_head(job) for job in peers)
        candidates = [
            job for job in peers if job.page == 1 or (job.baseline_ready and job.scan_state)
        ]
        if candidates and (now - checked).total_seconds() >= head_seconds:
            heads.append((checked, candidates))
        deep.extend(
            job
            for job in peers
            if job.page > 1 and timestamp((job.scheduling or {}).get("deep_due_at")) <= now
        )
    # Complete a new query's quiet two-page baseline promptly. It cannot refresh
    # its head until that proof is complete; waiting for four polls of another
    # query caused minute-long gaps. Retain one real head between baseline
    # continuations and respect the same durable deep-page retry deadline.
    baseline = [job for job in deep if not job.baseline_ready]
    if baseline and (not heads or head_streak >= 1):
        first = min(
            baseline,
            key=lambda j: (
                timestamp((j.scheduling or {}).get("deep_attempt_at")), j.next_due, j.id
            ),
        )
        return [j for j in deep if j.query_key == first.query_key and j.page == first.page]
    if deep and (not heads or head_streak >= 4):
        first = min(
            deep,
            key=lambda j: (
                timestamp((j.scheduling or {}).get("deep_attempt_at")),
                j.next_due,
                j.id,
            ),
        )
        return [j for j in deep if j.query_key == first.query_key and j.page == first.page]
    if heads:
        peers = min(heads, key=lambda pair: pair[0])[1]
        for job in peers:
            refresh_head_if_due(job, now, force=True)
        return [job for job in peers if job.page == 1]
    # Baselines and the catch-up proof retain their natural page-one steps.
    natural = [job for job in jobs if job.page == 1]
    if natural:
        first = min(natural, key=lambda j: (j.next_due, j.id))
        return [j for j in natural if j.query_key == first.query_key]
    return []


def deep_failed(job, now):
    schedule = dict(job.scheduling or {})
    failures = schedule.get("deep_failures", 0) + 1
    schedule.update(
        deep_failures=failures,
        deep_due_at=(now + timedelta(seconds=min(300, 30 * 2 ** min(failures - 1, 4)))).isoformat(),
    )
    job.scheduling = schedule
