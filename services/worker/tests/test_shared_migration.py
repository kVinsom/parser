from datetime import timedelta

from test_shared_catalog import Inventory, card, shared, subscribe, tick
from test_monitoring import rows
from vpars_bot.db import utcnow
from vpars_worker.db import Delivery, Job, SeenItem
from vpars_worker.monitor import Monitor
from vpars_worker.shared_catalog import SharedMonitor


async def test_old_saved_queries_keep_eligibility_then_share_one_fetch(environment):
    old, catalog = environment
    store, _ = shared(environment)
    await subscribe(old, catalog, 101, "Amiri")
    await subscribe(old, catalog, 202, "Stone Island")
    parser = Inventory([card("998"), card("999", "Stone Island")])
    legacy = Monitor(old, parser)
    await legacy.once("FR")
    await legacy.once("FR")
    assert all(j.baseline_ready for j in await rows(old, Job))
    old_seen = {(s.job_id,s.item_id):(s.first_seen_at,s.eligible) for s in await rows(old, SeenItem)}
    old_queries = {j.id:j.query for j in await rows(old, Job)}
    await store.refresh_queries()
    jobs = await rows(store, Job)
    assert len({j.query_key for j in jobs}) == 1
    assert all(j.baseline_ready and not j.query["search_text"] for j in jobs)
    assert all(j.scheduling["previous_catalog"]["query"] == old_queries[j.id] for j in jobs)
    assert old_seen == {(s.job_id,s.item_id):(s.first_seen_at,s.eligible) for s in await rows(store, SeenItem)}
    parser.calls.clear()
    parser.items = [card("10001"),card("10002","Stone Island")]
    await SharedMonitor(store,parser).once("FR")
    assert len(parser.calls)==1
    assert {(d.user_id,d.item["source_item_id"]) for d in await rows(store,Delivery)} == {(101,"10001"),(202,"10002")}


async def test_shared_matching_keeps_age_size_price_and_exclusion_rules(environment):
    from vpars_bot.db import ControlCommand
    from vpars_parser.models import Attribute, Money
    from sqlalchemy import select
    store,catalog=shared(environment)
    await subscribe(store,catalog,101,"Amiri",sizes=["L"],
                    native_prices={"EUR":{"minimum":"20","maximum":"40"}},excluded_keywords=["hole"])
    await subscribe(store,catalog,202,"Amiri")
    parser=Inventory([card("999")])
    monitor=SharedMonitor(store,parser)
    await monitor.once("FR")
    async with store.sessions.begin() as session:
        for command in await session.scalars(select(ControlCommand)):
            command.created_at=utcnow()-timedelta(hours=2)
    await tick(store)
    parser.items=[card("10001"),card("10002",size=Attribute(raw="S",normalized="s")),
                  card("10003",price=Money(amount="50",currency="EUR")),
                  card("10004",description="has a hole"),
                  card("10005",published_at=utcnow()-timedelta(minutes=10))]
    await monitor.once("FR")
    actual={(d.user_id,d.item["source_item_id"]) for d in await rows(store,Delivery)}
    assert actual == {(101,"10001"),(202,"10001"),(202,"10002"),(202,"10003"),(202,"10004")}


async def test_same_user_overlapping_filters_merge_candidates(environment):
    store,catalog=shared(environment)
    await subscribe(store,catalog,101,"Amiri",key="wide")
    await subscribe(store,catalog,101,"Amiri",key="narrow",sizes=["L"])
    parser=Inventory([card("999")])
    monitor=SharedMonitor(store,parser)
    await monitor.once("FR")
    await tick(store)
    parser.items=[card("10001")]
    await monitor.once("FR")
    deliveries=await rows(store,Delivery)
    assert len(deliveries)==1 and len(deliveries[0].candidates)==2
    assert len(parser.calls)==2
