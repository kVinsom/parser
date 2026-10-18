"""Rotate service between saved searches without changing delivery eligibility."""

from datetime import UTC, datetime

from sqlalchemy import JSON, String, and_, cast, column, func, select, text
from sqlalchemy.dialects.postgresql import insert
from vpars_worker.db import Delivery, DeliveryTurn

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


async def lock_lane(session, lane):
    # Only the short claim transaction is serialized, never network work. Both
    # workers must see the previous committed turn before choosing another lot.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(198404, :lane)"),
        {"lane": {"prepare": 1, "ready": 2}.get(lane, 0)},
    )


def service_order(lane):
    candidate = func.json_array_elements(Delivery.candidates).table_valued(
        column("value", JSON)
    )
    token = candidate.c.value["token"]
    # A lot may serve several matching searches. Credit all of them when it is
    # claimed, so the number or order of overlapping candidates gives no bonus.
    last_served = (
        select(func.min(func.coalesce(DeliveryTurn.last_claimed_at, EPOCH)))
        .select_from(candidate)
        .outerjoin(
            DeliveryTurn,
            and_(
                DeliveryTurn.lane == (lane or "any"),
                DeliveryTurn.user_id == Delivery.user_id,
                DeliveryTurn.search_id == token["search_id"].as_string(),
            ),
        )
        .where(token["user_id"].as_string() == cast(Delivery.user_id, String))
        .correlate(Delivery)
        .scalar_subquery()
    )
    return func.coalesce(last_served, EPOCH)


async def record_turn(session, row, lane, now):
    searches = {
        candidate["token"]["search_id"]
        for candidate in row.candidates
        if candidate.get("token", {}).get("user_id") == row.user_id
        and candidate["token"].get("search_id")
    }
    if not searches:
        return
    statement = insert(DeliveryTurn).values(
        [
            {
                "lane": lane or "any",
                "user_id": row.user_id,
                "search_id": search,
                "last_claimed_at": now,
            }
            for search in sorted(searches)
        ]
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                DeliveryTurn.lane,
                DeliveryTurn.user_id,
                DeliveryTurn.search_id,
            ],
            set_={"last_claimed_at": statement.excluded.last_claimed_at},
        )
    )
