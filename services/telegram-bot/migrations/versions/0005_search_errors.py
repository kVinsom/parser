"""Expose safe worker failure codes in the owner's search menu."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_search_sources", sa.Column("last_error", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("bot_search_sources", "last_error")
