"""Independent wallet bindings and connection attempts for each user's region."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    # Change keys in place: no settings, orders, session secrets or delivery history rewrite.
    for table in ("bot_wallet_accounts", "bot_wallet_sessions"):
        primary = sa.inspect(op.get_bind()).get_pk_constraint(table)
        op.drop_constraint(primary["name"], table, type_="primary")
        op.create_primary_key(primary["name"], table, ["user_id", "region"])
    op.add_column("bot_wallet_sessions", sa.Column(
        "auto_enable", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))


def downgrade():
    # Refuse a lossy downgrade once a user has connected multiple regions.
    for table in ("bot_wallet_accounts", "bot_wallet_sessions"):
        if op.get_bind().execute(sa.text(
            f"SELECT user_id FROM {table} GROUP BY user_id HAVING COUNT(*) > 1 LIMIT 1"
        )).first():
            raise RuntimeError("Regional wallets exist; refusing to discard connections")
    op.drop_column("bot_wallet_sessions", "auto_enable")
    for table in ("bot_wallet_accounts", "bot_wallet_sessions"):
        primary = sa.inspect(op.get_bind()).get_pk_constraint(table)
        op.drop_constraint(primary["name"], table, type_="primary")
        op.create_primary_key(primary["name"], table, ["user_id"])
