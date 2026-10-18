"""Isolate chat queues, fence leases and retain the epoch at search admission."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bot_inbox", sa.Column("lane_key", sa.String(64), nullable=False, server_default="legacy")
    )
    op.add_column("bot_inbox", sa.Column("lease_token", sa.String(36), nullable=True))
    op.add_column("bot_inbox", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bot_inbox", sa.Column("search_epoch", sa.BigInteger(), nullable=True))
    op.execute("""UPDATE bot_inbox SET lane_key = COALESCE(
      'chat:' || (payload->'message'->'chat'->>'id'),
      'chat:' || (payload->'callback_query'->'message'->'chat'->>'id'),
      'update:' || update_id::text)""")
    op.execute("""UPDATE bot_inbox i SET search_epoch = u.delivery_epoch FROM bot_users u
      WHERE i.lane_key = 'chat:' || u.id::text AND i.state IN ('received', 'processing')""")
    op.alter_column("bot_inbox", "lane_key", server_default=None)
    op.create_index(
        "ix_bot_inbox_pending_lane",
        "bot_inbox",
        ["bot_id", "lane_key", "received_at", "update_id"],
        postgresql_where=sa.text("state IN ('received', 'processing')"),
    )
    op.create_index(
        "ix_bot_inbox_pending_due",
        "bot_inbox",
        ["bot_id", "next_attempt_at"],
        postgresql_where=sa.text("state IN ('received', 'processing')"),
    )


def downgrade():
    op.drop_index("ix_bot_inbox_pending_due", table_name="bot_inbox")
    op.drop_index("ix_bot_inbox_pending_lane", table_name="bot_inbox")
    for column in ("search_epoch", "lease_until", "lease_token", "lane_key"):
        op.drop_column("bot_inbox", column)
