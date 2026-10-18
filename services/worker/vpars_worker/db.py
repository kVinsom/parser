from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateSchema
from vpars_bot.db import utcnow

from .message_cache import cache_values

SCHEMA = "vpars_monitor"


class Base(DeclarativeBase):
    pass


class Revision(Base):
    __tablename__ = "schema_revision"
    __table_args__ = {"schema": SCHEMA}
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Receipt(Base):
    __tablename__ = "command_receipts"
    __table_args__ = {"schema": SCHEMA}
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "catalog_jobs"
    __table_args__ = {"schema": SCHEMA}
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token: Mapped[dict] = mapped_column(JSON)
    query: Mapped[dict] = mapped_column(JSON)
    query_key: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(16), default="waiting", index=True)
    page: Mapped[int] = mapped_column(Integer, default=1)
    next_due: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    checks: Mapped[int] = mapped_column(Integer, default=0)
    items_checked: Mapped[int] = mapped_column(Integer, default=0)
    matches: Mapped[int] = mapped_column(Integer, default=0)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    coverage: Mapped[str | None] = mapped_column(String(32))
    baseline_ready: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    baseline_pages: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    checkpoint: Mapped[dict | None] = mapped_column(JSON)
    scan_state: Mapped[dict | None] = mapped_column(JSON)
    scheduling: Mapped[dict | None] = mapped_column(JSON)


class RegionalCatalog(Base):
    """One durable traversal and lease per upstream region, shared by subscribers."""
    __tablename__ = "regional_catalogs"
    __table_args__ = {"schema": SCHEMA}
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    next_due: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RegionalIdentity(Base):
    __tablename__ = "regional_identities"
    __table_args__ = {"schema": SCHEMA}
    catalog_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)


class SeenItem(Base):
    __tablename__ = "seen_items"
    __table_args__ = {"schema": SCHEMA}
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    item: Mapped[dict | None] = mapped_column(JSON)
    decision: Mapped[dict | None] = mapped_column(JSON)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CatalogPage(Base):
    __tablename__ = "catalog_pages"
    __table_args__ = {"schema": SCHEMA}
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    page: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Preserve normalized source fields and warnings for item-level diagnosis.
    result: Mapped[dict] = mapped_column(JSON)
    new_ids: Mapped[list] = mapped_column(JSON)
    decisions: Mapped[dict] = mapped_column(JSON)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = {"schema": SCHEMA}
    # sha256(user, source, namespace, item): one durable identity across searches/restarts.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    item: Mapped[dict] = mapped_column(JSON)
    candidates: Mapped[list[dict]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    next_due: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    uncertain: Mapped[int] = mapped_column(Integer, default=0)
    text_only: Mapped[bool] = mapped_column(Boolean, default=False)
    media_progress: Mapped[dict | None] = mapped_column(JSON)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeliveryTurn(Base):
    """Durable service order, independent of user settings and delivery history."""

    __tablename__ = "delivery_turns"
    __table_args__ = {"schema": SCHEMA}
    lane: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    search_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    last_claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TelegramPhoto(Base):
    __tablename__ = "telegram_photos"
    __table_args__ = {"schema": SCHEMA}
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(1024))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SentLot(Base):
    __tablename__ = "sent_lots"
    __table_args__ = {"schema": SCHEMA}
    # No TTL or FK to deliveries: queue cleanup must never erase notification history.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    source_item_id: Mapped[str] = mapped_column(String(128))
    delivery_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


WORK_INDEX = Index(
    "ix_deliveries_work",
    Delivery.state,
    Delivery.next_due,
    Delivery.created_at,
    postgresql_where=text("state IN ('pending', 'sending')"),
)
ITEM_INDEX = Index(
    "ix_deliveries_item_state",
    Delivery.user_id,
    Delivery.item["source_id"].as_string(),
    Delivery.item["source_item_id"].as_string(),
    Delivery.state,
)


async def migrate(engine):
    """Additive migrations; preserve existing jobs, dedup identities and deliveries."""
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(198403, 1)"))
        await connection.execute(CreateSchema(SCHEMA, if_not_exists=True))
        await connection.run_sync(Base.metadata.create_all)
        versions = list(
            (await connection.execute(text(f"SELECT id FROM {SCHEMA}.schema_revision"))).scalars()
        )
        if versions and versions not in ([1], [2], [3], [4], [5], [6]):
            raise RuntimeError("Unsupported monitoring schema version")
        if versions == [1]:
            await connection.execute(
                text(
                    f"ALTER TABLE {SCHEMA}.deliveries ADD COLUMN IF NOT EXISTS media_progress JSON"
                )
            )
            await connection.execute(
                text(
                    f"ALTER TABLE {SCHEMA}.catalog_jobs ADD COLUMN IF NOT EXISTS "
                    "baseline_ready BOOLEAN NOT NULL DEFAULT false"
                )
            )
            await connection.execute(
                text(f"UPDATE {SCHEMA}.schema_revision SET id = 2 WHERE id = 1")
            )
        if versions not in ([3], [4], [5], [6]):
            # Seed from all confirmed historical deliveries, including those cancelled
            # after sending. Never mark merely observed, pending or failed lots as sent.
            history = (
                await connection.execute(
                    select(Delivery)
                    .where(or_(Delivery.state == "sent", Delivery.message_id.is_not(None)))
                    .order_by(Delivery.sent_at.asc().nulls_last(), Delivery.created_at, Delivery.id)
                )
            ).mappings()
            for row in history:
                await connection.execute(
                    insert(SentLot)
                    .values(
                        **cache_values(
                            row["user_id"],
                            row["item"],
                            row["id"],
                            row["message_id"],
                            row["sent_at"] or row["created_at"],
                        )
                    )
                    .on_conflict_do_nothing(index_elements=[SentLot.id])
                )
            if versions:
                await connection.execute(text(f"UPDATE {SCHEMA}.schema_revision SET id = 3"))
            else:
                await connection.execute(
                    text(f"INSERT INTO {SCHEMA}.schema_revision (id) VALUES (3)")
                )
        if versions not in ([4], [5], [6]):
            await connection.execute(
                text(
                    f"ALTER TABLE {SCHEMA}.seen_items ADD COLUMN IF NOT EXISTS "
                    "eligible BOOLEAN NOT NULL DEFAULT false"
                )
            )
            await connection.execute(
                text(
                    f"ALTER TABLE {SCHEMA}.catalog_jobs ADD COLUMN IF NOT EXISTS "
                    "baseline_pages JSON NOT NULL DEFAULT '[]'"
                )
            )
            await connection.execute(
                text(
                    f"UPDATE {SCHEMA}.catalog_jobs SET baseline_pages = '[1]' "
                    "WHERE baseline_ready AND baseline_pages::text = '[]'"
                )
            )
            await connection.execute(text(f"UPDATE {SCHEMA}.schema_revision SET id = 4"))
        if versions not in ([5], [6]):
            for table, column, datatype in (
                ("catalog_jobs", "checkpoint", "JSON"),
                ("catalog_jobs", "scan_state", "JSON"),
                ("seen_items", "item", "JSON"),
                ("seen_items", "decision", "JSON"),
                ("seen_items", "last_seen_at", "TIMESTAMP WITH TIME ZONE"),
            ):
                await connection.execute(
                    text(
                        f"ALTER TABLE {SCHEMA}.{table} ADD COLUMN IF NOT EXISTS {column} {datatype}"
                    )
                )
            # Existing baseline flags/seen IDs are retained. Legacy jobs recover
            # their old frontier from known IDs; no new silent baseline is inserted.
            await connection.execute(text(f"UPDATE {SCHEMA}.schema_revision SET id = 5"))
        if versions != [6]:
            await connection.execute(
                text(f"ALTER TABLE {SCHEMA}.catalog_jobs ADD COLUMN IF NOT EXISTS scheduling JSON")
            )
            await connection.execute(text(f"UPDATE {SCHEMA}.schema_revision SET id = 6"))
        for index in (WORK_INDEX, ITEM_INDEX):
            await connection.run_sync(lambda conn, index=index: index.create(conn, checkfirst=True))
