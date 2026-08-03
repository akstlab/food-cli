"""Initial schema: tokens, prefs, addresses, cache, orders, preferences.

This is the baseline. A database created before migrations existed is stamped
at this revision rather than re-created - see `food_cli.core.migrations.adopt`.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth",
        sa.Column("server", sa.Text, primary_key=True),
        sa.Column("tokens", sa.Text),
        sa.Column("client_info", sa.Text),
        sa.Column("updated_at", sa.Float),
    )
    op.create_table(
        "prefs",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text),
        sa.Column("updated_at", sa.Float),
    )
    op.create_table(
        "addresses",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("label", sa.Text),
        sa.Column("payload", sa.Text),
        sa.Column("is_default", sa.Integer, server_default="0"),
        sa.Column("updated_at", sa.Float),
    )
    op.create_table(
        "cache",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text),
        sa.Column("expires_at", sa.Float),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text),
        sa.Column("vendor", sa.Text),
        sa.Column("amount", sa.Float),
        sa.Column("original", sa.Float),
        sa.Column("discount", sa.Float),
        sa.Column("coupon", sa.Text),
        sa.Column("address_id", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("payload", sa.Text),
        sa.Column("ordered_at", sa.Text),
        sa.Column("source", sa.Text),
        sa.Column("created_at", sa.Float),
    )
    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("quantity", sa.Integer),
        sa.Column("kind", sa.Text),
        sa.Column("created_at", sa.Float),
    )
    op.create_table(
        "preferences",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text),
        sa.Column("source", sa.Text),
        sa.Column("confidence", sa.Float),
        sa.Column("evidence", sa.Text),
        sa.Column("updated_at", sa.Float),
    )
    op.create_index("idx_orders_created", "orders", ["created_at"])
    op.create_index("idx_items_name", "order_items", ["name"])


def downgrade() -> None:
    op.drop_index("idx_items_name", table_name="order_items")
    op.drop_index("idx_orders_created", table_name="orders")
    for table in ("preferences", "order_items", "orders", "cache",
                  "addresses", "prefs", "oauth"):
        op.drop_table(table)
