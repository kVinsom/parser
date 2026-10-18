"""Split Vinted countries into independently selectable marketplaces."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

REGIONS = ("GB", "FR", "IT", "DE")
MARKETS = {f"vinted_{region.lower()}": region for region in REGIONS}


def convert(sources, *, split):
    result = {}
    for key, regions in sources.items():
        if split and key == "vinted":
            for region in regions:
                region = "GB" if region.upper() == "UK" else region.upper()
                if region not in REGIONS:
                    raise ValueError("Unexpected legacy Vinted region; data left unchanged.")
                target = f"vinted_{region.lower()}"
                result[target] = [region]
        else:
            target = "vinted" if not split and key in MARKETS else key
            result[target] = list(dict.fromkeys([*result.get(target, []), *regions]))
    return result


def migrate(*, split):
    connection = op.get_bind()
    metadata = sa.MetaData()
    tables = {
        name: sa.Table(name, metadata, autoload_with=connection)
        for name in (
            "bot_searches",
            "bot_search_versions",
            "bot_users",
            "bot_search_sources",
            "bot_control_commands",
        )
    }
    # Preserve IDs, versions, archived flags and every non-source filter, including history.
    for name in ("bot_searches", "bot_search_versions"):
        table = tables[name]
        for row in list(connection.execute(sa.select(table)).mappings()):
            filters = {**row["filters"], "sources": convert(row["filters"]["sources"], split=split)}
            if filters != row["filters"]:
                where = sa.and_(
                    *(column == row[column.name] for column in table.primary_key.columns)
                )
                connection.execute(table.update().where(where).values(filters=filters))

    users = tables["bot_users"]
    for row in list(connection.execute(sa.select(users.c.id, users.c.enabled_sources)).mappings()):
        enabled = row["enabled_sources"]
        if enabled is None:
            continue
        changed = []
        for source_id in enabled:
            if split and source_id == "vinted":
                changed.extend(MARKETS)
            else:
                changed.append("vinted" if not split and source_id in MARKETS else source_id)
        connection.execute(
            users.update()
            .where(users.c.id == row["id"])
            .values(enabled_sources=list(dict.fromkeys(changed)))
        )

    searches, runs, commands = (
        tables[name] for name in ("bot_searches", "bot_search_sources", "bot_control_commands")
    )
    replaced = ["vinted"] if split else list(MARKETS)
    old_runs = list(
        connection.execute(sa.select(runs).where(runs.c.source_id.in_(replaced))).mappings()
    )
    grouped = {}
    for run in old_runs:
        grouped.setdefault(run["search_id"], []).append(run)
    for search_id, previous in grouped.items():
        item = (
            connection.execute(sa.select(searches).where(searches.c.id == search_id))
            .mappings()
            .one()
        )
        generation = max(run["generation"] for run in previous) + 1
        # Old work retains the old IDs and therefore cannot acknowledge or deliver after migration.
        connection.execute(
            commands.insert().values(
                id=str(uuid4()),
                user_id=item["user_id"],
                kind="invalidate",
                payload={
                    "search_id": search_id,
                    "filter_version": item["version"],
                    "delivery_epoch": connection.scalar(
                        sa.select(users.c.delivery_epoch).where(users.c.id == item["user_id"])
                    ),
                    "generations": {run["source_id"]: run["generation"] + 1 for run in previous},
                },
                created_at=datetime.now(UTC),
            )
        )
        connection.execute(
            runs.delete().where(runs.c.search_id == search_id, runs.c.source_id.in_(replaced))
        )
        targets = [
            key for key in item["filters"]["sources"] if key in (MARKETS if split else {"vinted"})
        ]
        for target in targets:
            exists = connection.scalar(
                sa.select(runs.c.source_id).where(
                    runs.c.search_id == search_id, runs.c.source_id == target
                )
            )
            if exists is None:
                connection.execute(
                    runs.insert().values(
                        search_id=search_id,
                        source_id=target,
                        generation=generation,
                        state="stopped",
                        active_until=None,
                    )
                )


def upgrade():
    migrate(split=True)


def downgrade():
    # The old UI has one Vinted switch: any selected country maps back to that switch.
    migrate(split=False)
