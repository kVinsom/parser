from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from vpars_bot.db import utcnow
from vpars_parser.models import Pagination

from vpars_worker.catchup import advance, begin, scope_key
from vpars_worker.scheduling import choose


def job_for(brand, region):
    query = {"source_id": "vinted", "region_id": region, "search_text": brand}
    return SimpleNamespace(
        query=query,
        query_key=scope_key(query),
        scan_state=None,
        checkpoint={"scope": scope_key(query), "ids": ["1", "2"]},
        baseline_ready=True,
        page=1,
        scheduling={},
        next_due=utcnow(),
        id=brand,
    )


def page_result(page, ids):
    return SimpleNamespace(
        observed_item_ids=ids,
        completeness="complete",
        identities_complete=True,
        pagination=Pagination(requested_page=page, has_more="unknown"),
    )


@pytest.mark.parametrize("brand,region", [("Amiri", "UK"), ("Stone Island", "FR"), ("Nike", "DE")])
def test_missing_anchor_bounds_deep_traffic_keeps_cursor_and_refreshes_head(brand, region):
    job, now = job_for(brand, region), utcnow()
    checkpoint = deepcopy(job.checkpoint)
    state = begin(job, now)
    for page in range(1, 21):
        job.page = page
        advance(job, state, page_result(page, [str(100 + page)]), {str(100 + page)}, now, 100)
        state = job.scan_state
    assert job.page == 21 and job.coverage == "catchup_unconfirmed"
    assert job.checkpoint == checkpoint
    assert not choose([job], now + timedelta(seconds=4), 0)
    assert choose([job], now + timedelta(seconds=9), 0) == [job]
    assert job.page == 1
    advance(job, job.scan_state, page_result(1, ["999"]), {"999"}, now + timedelta(seconds=9), 100)
    assert job.page == 21 and job.checkpoint == checkpoint
    # Reloaded continuation still has its pause and offset, with no lost IDs.
    restored = deepcopy(job)
    assert choose([restored], now + timedelta(seconds=32), 4) == [restored]
    assert restored.page == 21


def test_repeated_offset_is_retried_instead_of_counted_as_new_coverage():
    job, now = job_for("Adidas", "IT"), utcnow()
    state = begin(job, now)
    advance(job, state, page_result(1, ["100", "101"]), {"100", "101"}, now, 3)
    advance(job, job.scan_state, page_result(2, ["101", "100"]), set(), now, 3)
    assert job.page == 2 and job.scan_state["coverage_note"] == "RepeatedPageInventory"
    assert job.scan_state["sweep_pages"] == 1
    assert job.checkpoint["ids"] == ["1", "2"]
    advance(job, job.scan_state, page_result(2, ["102"]), {"102"}, now + timedelta(seconds=31), 3)
    assert job.page == 3 and job.scan_state["sweep_pages"] == 2
