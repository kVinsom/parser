"""Wallet purchase intents, confirmations and durable payment ambiguity guard."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_wallet_accounts",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("bot_users.id"), primary_key=True),
        sa.Column("region", sa.String(2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("max_total", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("contract_hash", sa.String(64)),
        sa.Column("active_order", sa.String(32)),
    )
    op.create_table(
        "bot_wallet_orders",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("bot_users.id"), nullable=False),
        sa.Column("item_id", sa.String(20), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("listed_price", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("quote", sa.JSON()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.String(64)),
        sa.Column("external_order_id", sa.String(80)),
        sa.UniqueConstraint("user_id", "item_id", name="uq_wallet_user_item"),
    )
    op.create_index("ix_bot_wallet_orders_user_id", "bot_wallet_orders", ["user_id"])
    op.create_index("ix_bot_wallet_orders_state", "bot_wallet_orders", ["state"])


def downgrade():
    op.drop_table("bot_wallet_orders")
    op.drop_table("bot_wallet_accounts")
