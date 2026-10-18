"""Persist subscription quota periods and debit reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bot_subscriptions",
        sa.Column("period_started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.execute("UPDATE bot_subscriptions SET period_started_at = updated_at")
    op.alter_column("bot_subscriptions", "period_started_at", nullable=False)
    op.add_column("bot_wallet_orders", sa.Column("quota_period_start", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("bot_wallet_orders", "quota_period_start")
    op.drop_column("bot_subscriptions", "period_started_at")
