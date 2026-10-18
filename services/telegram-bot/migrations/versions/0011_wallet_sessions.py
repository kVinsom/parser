"""Encrypted, durable self-service session connection attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_wallet_sessions",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("bot_users.id"), primary_key=True),
        sa.Column("attempt_id", sa.String(32), nullable=False),
        sa.Column("region", sa.String(2), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column("error", sa.String(64), nullable=True),
        sa.Column("account_key", sa.String(80), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("bot_wallet_sessions")
