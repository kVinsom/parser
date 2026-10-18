"""Persist enabled saved filters, preserving existing searches by default."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bot_searches",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade():
    op.drop_column("bot_searches", "enabled")
