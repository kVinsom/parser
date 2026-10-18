from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Guard(Base):
    __tablename__ = "bot_guards"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    bootstrapped: Mapped[bool] = mapped_column(Boolean, default=False)


class User(Base):
    __tablename__ = "bot_users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(32))
    username_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    full_name: Mapped[str] = mapped_column(String(256))
    profile_update_id: Mapped[int] = mapped_column(BigInteger, default=-1)
    profile_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    # NULL preserves the source selection of searches created before this setting existed.
    enabled_sources: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)


class Subscription(Base):
    __tablename__ = "bot_subscriptions"
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), primary_key=True)
    level: Mapped[str] = mapped_column(String(8))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    granted_by: Mapped[int] = mapped_column(ForeignKey("bot_users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Active extensions and tier changes preserve the 30-day Autobuy quota anchor.
    period_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )


class SubscriptionOrder(Base):
    __tablename__ = "bot_subscription_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    level: Mapped[str] = mapped_column(String(8))
    months: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="awaiting_receipt")
    receipt_kind: Mapped[str | None] = mapped_column(String(16))
    receipt_file_id: Mapped[str | None] = mapped_column(Text)
    receipt_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    """Durable subscription notifications, committed with the business operation."""

    __tablename__ = "bot_notifications"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"))
    admin_only: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(64))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WalletAccount(Base):
    """Local browser profile binding. Cookies/tokens never enter this database."""

    __tablename__ = "bot_wallet_accounts"
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), primary_key=True)
    region: Mapped[str] = mapped_column(String(2), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    max_total: Mapped[int] = mapped_column(Integer)  # Minor currency units, includes all fees.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    contract_hash: Mapped[str | None] = mapped_column(String(64))
    active_order: Mapped[str | None] = mapped_column(String(32))


class WalletSession(Base):
    """A connection attempt or verified encrypted cookie jar; never a raw key."""

    __tablename__ = "bot_wallet_sessions"
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(2), primary_key=True)
    auto_enable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    state: Mapped[str] = mapped_column(String(24))
    secret: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(64))
    account_key: Mapped[str | None] = mapped_column(String(80))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WalletOrder(Base):
    __tablename__ = "bot_wallet_orders"
    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_wallet_user_item"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    item_id: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    listed_price: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    state: Mapped[str] = mapped_column(String(24), default="requested", index=True)
    quote: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str | None] = mapped_column(String(64))
    external_order_id: Mapped[str | None] = mapped_column(String(80))
    quota_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Search(Base):
    __tablename__ = "bot_searches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    filters: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SearchVersion(Base):
    __tablename__ = "bot_search_versions"
    search_id: Mapped[str] = mapped_column(ForeignKey("bot_searches.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    filters: Mapped[dict] = mapped_column(JSON)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SearchRun(Base):
    __tablename__ = "bot_search_sources"
    search_id: Mapped[str] = mapped_column(ForeignKey("bot_searches.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(16), default="stopped")
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))


class Operation(Base):
    __tablename__ = "bot_operations"
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RoleEvent(Base):
    __tablename__ = "bot_role_events"
    operation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"))
    actor_username: Mapped[str | None] = mapped_column(String(32))
    recipient_username: Mapped[str | None] = mapped_column(String(32))
    old_role: Mapped[bool] = mapped_column(Boolean)
    new_role: Mapped[bool] = mapped_column(Boolean)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ControlCommand(Base):
    """Transactional outbox for the future worker; never consumed by this bot."""

    __tablename__ = "bot_control_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Inbox(Base):
    __tablename__ = "bot_inbox"
    __table_args__ = (
        Index(
            "ix_bot_inbox_pending_lane",
            "bot_id",
            "lane_key",
            "received_at",
            "update_id",
            postgresql_where=text("state IN ('received', 'processing')"),
            sqlite_where=text("state IN ('received', 'processing')"),
        ),
        Index(
            "ix_bot_inbox_pending_due",
            "bot_id",
            "next_attempt_at",
            postgresql_where=text("state IN ('received', 'processing')"),
            sqlite_where=text("state IN ('received', 'processing')"),
        ),
    )
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="received", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)
    lane_key: Mapped[str] = mapped_column(String(64), default="legacy")
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_epoch: Mapped[int | None] = mapped_column(BigInteger)


def database(url: str):
    engine = create_async_engine(url, pool_pre_ping=True, hide_parameters=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
