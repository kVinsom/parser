"""Durable, conservative offset-pagination traversal.

No ID ordering or single known card is a frontier. A checkpoint contains the
identity inventory of two head pages (one only for a proven terminal catalog).
We must encounter the entire inventory, on at least two pages, then repeat the
walk without discoveries and recheck its head before replacing the checkpoint.
Disappearing anchors / an unconfirmed end leave explicit unfinished work.
"""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta

# Offset catalogs have no snapshot and may lose checkpoint cards. Keep the
# continuation, but bound its traffic instead of walking forever at head speed.
DEEP_PAGE_BUDGET = 20
SWEEP_REQUEST_BUDGET = 100


def defer_deep(job, state, now):
    exhausted = (
        state["max_page"] >= DEEP_PAGE_BUDGET
        or state["requests"] >= SWEEP_REQUEST_BUDGET
        or state.get("repeated_inventory", False)
    )
    if not exhausted:
        return False
    rounds = max(
        state["max_page"] // DEEP_PAGE_BUDGET, state["requests"] // SWEEP_REQUEST_BUDGET, 1
    )
    seconds = min(300, 30 * 2 ** min(rounds - 1, 4))
    schedule = dict(job.scheduling or {})
    until = now + timedelta(seconds=seconds)
    previous = schedule.get("deep_due_at")
    if previous:
        until = max(until, datetime.fromisoformat(previous))
    job.scheduling = {**schedule, "deep_due_at": until.isoformat()}
    state["coverage_note"] = (
        "RepeatedPageInventory" if state.get("repeated_inventory") else "CatchupBudgetExceeded"
    )
    return True


def scope_key(query):
    """Identity of the actual upstream catalog, excluding local matching rules."""
    scope = {
        "source": query["source_id"],
        "region": "UK" if query["region_id"] == "GB" else query["region_id"],
        "text": query.get("search_text", ""),
        "newest_first": query.get("newest_first", False),
    }
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def begin(job, now, legacy_ids=()):
    if job.scan_state:
        return deepcopy(job.scan_state)
    checkpoint = job.checkpoint or {}
    if checkpoint and checkpoint.get("scope") != scope_key(job.query):
        raise ValueError("Checkpoint belongs to another upstream query")
    return {
        "started_at": now.isoformat(),
        "scope": scope_key(job.query),
        "phase": "scan" if job.baseline_ready else "baseline",
        "target": checkpoint.get("ids", sorted(legacy_ids)),
        "matched": [],
        "head": {},
        "new_in_sweep": 0,
        "sweep_pages": 0,
        "requests": 0,
        "batch_pages": 0,
        "step": "forward",
        "last_forward": 0,
        "max_page": 0,
    }


def refresh_head_if_due(job, now, seconds=24, *, force=False):
    """Refresh discovery without replacing the durable deep cursor or its overlap."""
    state = job.scan_state
    if (
        not job.baseline_ready
        or job.page <= 1
        or not state
        or state["phase"] == "baseline"
        or state["step"] == "refresh_head"
        or (not force and state["step"] in {"confirm_head", "head_probe"})
        or (not force and state.get("resume_after_refresh"))
    ):
        return
    checked = datetime.fromisoformat(state.get("head_checked_at", state["started_at"]))
    if not force and (now - checked).total_seconds() < seconds:
        return
    job.scan_state = {
        **state,
        "step": "refresh_head",
        "refresh_page": job.page,
        "refresh_step": state["step"],
    }
    job.page = 1


def advance(job, state, result, new_ids, now, batch_size):
    """Called in the same transaction as observations and the delivery outbox."""
    state = deepcopy(state)
    ids = set(result.observed_item_ids or [item.source_item_id for item in result.items])
    page = job.page
    state["requests"] += 1
    # Partial data may be saved/delivered but cannot move any traversal boundary.
    if result.completeness != "complete" or (
        result.observed_item_ids and not result.identities_complete
    ):
        state["partial_attempts"] = state.get("partial_attempts", 0) + 1
        if state["partial_attempts"] >= 2 and page > 1 and state["step"] == "forward":
            state.update(step="head_probe", resume_page=page)
            job.page = 1
        job.scan_state = state
        job.coverage = "catchup_partial"
        return

    state["partial_attempts"] = 0
    state["new_in_sweep"] += len(new_ids)
    if page == 1:
        state["head_checked_at"] = now.isoformat()
    terminal = result.pagination.has_more == "false"
    step = state["step"]
    if step == "refresh_head":
        state["step"] = state.pop("refresh_step")
        job.page = state.pop("refresh_page")
        state["resume_after_refresh"] = True
        if state["step"] == "confirm_head" and ids != set(state["head"].get("1", [])):
            _restart(state, "verify")
            job.page = 1
    elif step == "head_probe":
        state["step"] = "overlap"
        job.page = max(1, state["last_forward"])
    elif step == "overlap":
        state["step"] = "forward"
        job.page = state.pop("resume_page")
    elif step == "confirm_head":
        expected = state["head"].get(str(page), [])
        if ids != set(expected) or new_ids:
            _restart(state, "verify")
            job.page = 1
        elif page < len(state["head"]):
            job.page = page + 1
        else:
            _commit(job, state, now)
            return
    else:
        state.pop("resume_after_refresh", None)
        fingerprint = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
        if (
            not terminal
            and page > 1
            and state.get("last_inventory_page") == page - 1
            and state.get("last_inventory_hash") == fingerprint
        ):
            # A source ignoring the requested offset is not advancing coverage.
            # Retry this offset slowly; continue independent head discovery.
            state["repeated_inventory"] = True
            defer_deep(job, state, now)
            job.scan_state = state
            job.coverage = "catchup_unconfirmed"
            return
        state.pop("repeated_inventory", None)
        state["last_inventory_hash"] = fingerprint
        state["last_inventory_page"] = page
        state["last_forward"] = page
        state["max_page"] = max(state["max_page"], page)
        state["sweep_pages"] += 1
        state["batch_pages"] += 1
        if page <= 2:
            state["head"][str(page)] = sorted(ids)
        target = set(state["target"])
        state["matched"] = sorted(set(state["matched"]) | (ids & target))
        boundary = len(target) >= 2 and target <= set(state["matched"]) and page >= 2
        if state["phase"] == "baseline":
            # Initial inventory is deliberately quiet, once per query, never per deep page.
            if page >= 2 or terminal:
                _commit(job, state, now)
                return
        elif boundary or terminal:
            if state["phase"] == "verify" and state["new_in_sweep"] == 0:
                state["step"] = "confirm_head"
                job.page = 1
            else:
                _restart(state, "verify")
                job.page = 1
            job.scan_state = state
            job.coverage = "catchup_verifying"
            return

        # Unknown pagination is not a terminal page. Probe the contiguous next
        # offset even when the HTML has no trustworthy next-page control.
        job.page = page + 1
        if page >= 4294967295:
            job.page = page
            job.coverage = "catchup_unconfirmed"
            job.scan_state = state
            return
        if state["batch_pages"] >= max(1, batch_size):
            state["batch_pages"] = 0
            state["resume_page"] = job.page
            state["step"] = "head_probe"
            job.page = 1
    job.scan_state = state
    job.coverage = "baseline_pending" if state["phase"] == "baseline" else "catchup_pending"
    if page > 1 and defer_deep(job, state, now):
        job.coverage = "catchup_unconfirmed"
    elif state.get("coverage_note"):
        job.coverage = "catchup_unconfirmed"


def _restart(state, phase):
    state.update(
        phase=phase,
        step="forward",
        matched=[],
        head={},
        new_in_sweep=0,
        sweep_pages=0,
        batch_pages=0,
        last_forward=0,
    )
    state.pop("resume_page", None)
    state.pop("resume_after_refresh", None)
    state.pop("last_inventory_hash", None)
    state.pop("last_inventory_page", None)


def _commit(job, state, now):
    job.checkpoint = {
        "scope": state["scope"],
        "ids": sorted({identity for ids in state["head"].values() for identity in ids}),
        "head": state["head"],
        "completed_at": now.isoformat(),
        "started_at": state["started_at"],
        "requests": state["requests"],
        "failed_requests": state.get("failed_requests", 0),
        "max_page": state["max_page"],
        "kind": "baseline" if state["phase"] == "baseline" else "verified_catchup",
    }
    job.baseline_ready = True
    job.baseline_pages = list(range(1, len(state["head"]) + 1))
    job.scan_state = None
    job.page = 1
    job.coverage = "baseline_ready" if state["phase"] == "baseline" else "catchup_complete"
