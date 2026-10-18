"""Subscription receipt requests and durable Telegram notifications."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_subscription_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("bot_users.id"), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("receipt_kind", sa.String(16)),
        sa.Column("receipt_file_id", sa.Text()),
        sa.Column("receipt_message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_bot_subscription_orders_user_id", "bot_subscription_orders", ["user_id"])
    op.create_table(
        "bot_notifications",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("recipient_id", sa.BigInteger(), sa.ForeignKey("bot_users.id"), nullable=False),
        sa.Column("admin_only", sa.Boolean(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(64)),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bot_notifications_state", "bot_notifications", ["state"])


def downgrade():
    op.drop_table("bot_notifications")
    op.drop_table("bot_subscription_orders")
