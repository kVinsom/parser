"""Manually granted subscription metadata, without payment or search restrictions."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_subscriptions",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("bot_users.id"), primary_key=True),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by", sa.BigInteger(), sa.ForeignKey("bot_users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("bot_subscriptions")
