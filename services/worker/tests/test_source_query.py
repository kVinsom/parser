import pytest
from test_monitoring import Parser, due, lot, rows
from vpars_bot.domain import Filters

from vpars_worker.db import Delivery, Job
from vpars_worker.monitor import Monitor, source_text


@pytest.mark.parametrize(
    "keywords,expected",
    [
        (["Stone island"], "Stone island"),
        (["Gucci", "Jeans", "W32", "Very Good"], "Gucci"),
        (["W32", "Very Good", "Gucci", "Jeans"], "Gucci"),
        (["w 32", "Very Good"], ""),
        (["L", "Very Good", "Stone Island", "hoodie"], "Stone Island"),
        (["EU 42", "Nike", "Air Max 90"], "Nike"),
        (["2XL", "Très bon état", "Adidas"], "Adidas"),
        (["One size", "New with tags"], ""),
        (["L", "Very Good"], ""),
        (["  Stone   Island  ", "L"], "Stone Island"),
    ],
)
def test_and_keywords_narrow_catalog_without_requiring_size_or_condition_in_title(
    keywords, expected
):
    assert source_text({"keywords": keywords, "keywords_all": True}) == expected


def test_or_alternatives_are_not_silently_narrowed_to_one_keyword():
    assert source_text({"keywords": ["Nike", "Adidas"]}) == ""
    assert source_text({"keywords": ["Nike"]}) == "Nike"
    assert source_text({"brands": ["Nike"], "keywords": ["Air Max", "Air Force"]}) == "Nike"


def test_source_query_obeys_gateway_utf8_byte_limit():
    query = source_text({"keywords": ["я" * 300], "keywords_all": True})
    assert query == "я" * 250


async def test_keyword_query_upgrade_rebaselines_then_delivers_only_new_matches(environment):
    store, catalog = environment
    await catalog.save(
        101,
        Filters(
            keywords=["Stone Island", "L"],
            keywords_all=True,
            sources={"vinted_fr": ["FR"]},
            follow_enabled_sources=True,
        ),
        "save-keywords",
    )
    await store.control.start(101, "start-keywords")
    await store.ingest()
    original = next(job for job in await rows(store, Job) if job.query["region_id"] == "FR")
    async with store.sessions.begin() as session:
        job = await session.get(Job, original.id)
        job.query = {**job.query, "search_text": ""}
        job.baseline_ready = True
    await store.refresh_queries()
    upgraded = next(job for job in await rows(store, Job) if job.id == original.id)
    assert (
        upgraded.token == original.token and upgraded.query["filters"] == original.query["filters"]
    )
    assert upgraded.query["search_text"] == "Stone Island"
    assert not upgraded.baseline_ready

    # First targeted catalog may contain previously unseen old inventory: stay silent.
    parser = Parser(items=[lot("10001", published_at=None)])
    await Monitor(store, parser).once(region="FR")
    assert not await rows(store, Delivery)
    assert parser.calls[0].search_text == "Stone Island"
    await store.refresh_queries()
    assert next(job for job in await rows(store, Job) if job.id == original.id).baseline_ready
    await due(store, Job)
    parser.items.append(lot("10002", published_at=None))
    await Monitor(store, parser).once(region="FR")
    assert [d.item["source_item_id"] for d in await rows(store, Delivery)] == ["10002"]
    await due(store, Job)
    await Monitor(store, parser).once(region="FR")
    assert len(await rows(store, Delivery)) == 1
