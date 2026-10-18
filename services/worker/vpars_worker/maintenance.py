"""Drain in-flight work before a local restart; restore exact due dates afterwards."""

import json
from datetime import datetime

from sqlalchemy import func, or_, select, update
from vpars_bot.db import utcnow

from .db import Delivery, Job

TABLES = {"jobs": (Job, "waiting"), "deliveries": (Delivery, "pending")}


async def hold(sessions, journal, path):
    until = datetime.fromisoformat(journal["until"])
    async with sessions.begin() as session:
        for key, (model, state) in TABLES.items():
            rows = (
                await session.execute(
                    select(model.id, model.next_due)
                    .where(
                        model.state == state,
                        model.next_due < until,
                        or_(model.lease_until.is_(None), model.lease_until <= utcnow()),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for identity, due in rows:
                journal[key][identity] = due.isoformat()
            # Write recovery data before committing any changed schedule.
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(journal), encoding="utf-8")
            temporary.replace(path)
            if rows:
                await session.execute(
                    update(model)
                    .where(model.id.in_([identity for identity, _ in rows]))
                    .values(next_due=until)
                )
        active = sum(
            [
                await session.scalar(
                    select(func.count()).select_from(model).where(model.state == state)
                )
                for model, state in ((Job, "running"), (Delivery, "sending"))
            ]
        )
    return active


async def restore(sessions, journal):
    until = datetime.fromisoformat(journal["until"])
    async with sessions.begin() as session:
        for key, (model, _) in TABLES.items():
            for identity, due in journal[key].items():
                # Never overwrite a retry/backoff or change delivery/search state.
                await session.execute(
                    update(model)
                    .where(model.id == identity, model.next_due == until)
                    .values(next_due=datetime.fromisoformat(due))
                )
