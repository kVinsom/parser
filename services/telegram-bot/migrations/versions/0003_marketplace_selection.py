"""Persist each user's enabled marketplaces without changing existing searches."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_users", sa.Column("enabled_sources", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("bot_users", "enabled_sources")
