"""Synthetic transfer fixture for isolated Docker backup/restore validation."""

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path


async def main():
    from sqlalchemy import select
    from vpars_bot.config import Settings
    from vpars_bot.db import Search, SearchRun, Subscription, User, database, utcnow
    from vpars_worker.db import SentLot

    config = Settings()
    assert (
        config.telegram_bot_token.get_secret_value() == "999999:server-smoke-test-token"
    )
    engine, sessions = database(config.database_url.get_secret_value())
    try:
        if sys.argv[1] == "seed":
            async with sessions.begin() as session:
                assert not list(await session.scalars(select(User)))
                for user, brand in ((101, "Nike"), (202, "Adidas")):
                    session.add(
                        User(
                            id=user,
                            chat_id=user,
                            full_name="Synthetic test",
                            delivery_epoch=user,
                            is_admin=user == 101,
                            enabled_sources=["vinted_fr"],
                        )
                    )
                    await session.flush()
                    session.add(
                        Subscription(
                            user_id=user,
                            level="ULTRA",
                            granted_by=user,
                            expires_at=utcnow() + timedelta(days=30),
                        )
                    )
                    session.add(
                        Search(
                            id=f"transfer-{user}",
                            user_id=user,
                            version=3,
                            enabled=user == 101,
                            filters={
                                "brands": [brand],
                                "keywords": [],
                                "excluded_keywords": ["Fake"],
                                "sources": {"vinted_fr": ["FR"]},
                            },
                        )
                    )
                    await session.flush()
                    session.add(
                        SearchRun(
                            search_id=f"transfer-{user}",
                            source_id="vinted_fr",
                            generation=7,
                            state="stopped",
                        )
                    )
                    session.add(
                        SentLot(
                            id=f"history-{user}",
                            user_id=user,
                            source_id="vinted",
                            source_item_id=f"item-{user}",
                            delivery_id=f"delivery-{user}",
                            message_id=user,
                            sent_at=utcnow(),
                        )
                    )
            Path("/app/services/worker/.artifacts/source-cooldowns.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "regions": {},
                        "pacing": {
                            "FR": {
                                "interval": 8,
                                "recover_after": (
                                    utcnow() + timedelta(minutes=10)
                                ).isoformat(),
                            }
                        },
                    }
                )
            )
        else:
            async with sessions() as session:
                users = list(await session.scalars(select(User).order_by(User.id)))
                searches = list(
                    await session.scalars(select(Search).order_by(Search.user_id))
                )
                runs = list(await session.scalars(select(SearchRun)))
                history = list(await session.scalars(select(SentLot)))
                assert [
                    (u.id, u.delivery_epoch, u.is_admin, u.enabled_sources)
                    for u in users
                ] == [(101, 101, True, ["vinted_fr"]), (202, 202, False, ["vinted_fr"])]
                assert [
                    (
                        s.filters["brands"],
                        s.filters["excluded_keywords"],
                        s.enabled,
                        s.version,
                    )
                    for s in searches
                ] == [(["Nike"], ["Fake"], True, 3), (["Adidas"], ["Fake"], False, 3)]
                assert len(runs) == 2 and all(
                    r.state == "stopped" and r.generation == 7 for r in runs
                )
                assert len(history) == 2 and {r.message_id for r in history} == {
                    101,
                    202,
                }
            state = json.loads(
                Path(
                    "/app/services/worker/.artifacts/source-cooldowns.json"
                ).read_text()
            )
            assert state["pacing"]["FR"]["interval"] == 8
            print(
                "Restored users, access, filters, Stop, generations, history and regional pacing: OK"
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
