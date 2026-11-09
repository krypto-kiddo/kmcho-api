"""v2: users.wa_phone, dogs, deliveries, delivery_changes, sync_runs

Revision ID: c3a9e1d4b7f2
Revises: f94af269df69
Create Date: 2026-11-09 14:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c3a9e1d4b7f2"
down_revision: Union[str, Sequence[str], None] = "f94af269df69"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # WhatsApp number in one canonical form: 12 digits, "91" + 10-digit mobile
    op.add_column("users", sa.Column("wa_phone", sa.String(15), nullable=True))
    op.create_unique_constraint("uq_users_wa_phone", "users", ["wa_phone"])

    # Backfill from users.phone where it normalises cleanly and no two users share it.
    op.execute(r"""
        WITH digits AS (
            SELECT id, regexp_replace(coalesce(phone, ''), '\D', '', 'g') AS d FROM users
        ), norm AS (
            SELECT id,
                   CASE
                     WHEN length(d) = 10 THEN '91' || d
                     WHEN length(d) = 11 AND left(d, 1) = '0' THEN '91' || substr(d, 2)
                     WHEN length(d) = 12 AND left(d, 2) = '91' THEN d
                   END AS p
            FROM digits
        ), uniq AS (
            SELECT p FROM norm WHERE p IS NOT NULL GROUP BY p HAVING count(*) = 1
        )
        UPDATE users u SET wa_phone = n.p
        FROM norm n JOIN uniq USING (p)
        WHERE u.id = n.id
    """)

    op.create_table(
        "dogs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_key", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("monthly_rate", sa.Numeric(10, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "name_key", name="uq_dogs_user_name"),
    )
    op.create_index("ix_dogs_user_id", "dogs", ["user_id"])

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dog_id", sa.Integer(), sa.ForeignKey("dogs.id"), nullable=False),
        sa.Column("delivery_date", sa.Date(), nullable=False),
        sa.Column("raw_value", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("dish", sa.Text(), nullable=True),
        sa.Column("keyword", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("billable", sa.Boolean(), nullable=False),
        sa.Column("wa_message_id", sa.String(), nullable=True),
        sa.Column("billed_ledger_id", sa.Integer(), sa.ForeignKey("ledger.id"), nullable=True),
        sa.Column("sheet_tab", sa.String(), nullable=True),
        sa.Column("sheet_row", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("dog_id", "delivery_date", name="uq_deliveries_dog_date"),
        sa.CheckConstraint("kind IN ('meal', 'keyword', 'error')", name="ck_deliveries_kind"),
    )
    op.create_index("ix_deliveries_delivery_date", "deliveries", ["delivery_date"])

    op.create_table(
        "delivery_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_id", sa.Integer(), sa.ForeignKey("deliveries.id"), nullable=False),
        sa.Column("old_raw", sa.Text(), nullable=False),
        sa.Column("new_raw", sa.Text(), nullable=False),
        sa.Column("old_billable", sa.Boolean(), nullable=False),
        sa.Column("new_billable", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("ledger_id", sa.Integer(), sa.ForeignKey("ledger.id"), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_delivery_changes_delivery_id", "delivery_changes", ["delivery_id"])
    op.create_index("ix_delivery_changes_status", "delivery_changes", ["status"])

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("sheet_tab", sa.String(), nullable=False),
        sa.Column("stats", JSONB(), nullable=False),
        sa.Column("issues", JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("sync_runs")
    op.drop_index("ix_delivery_changes_status", table_name="delivery_changes")
    op.drop_index("ix_delivery_changes_delivery_id", table_name="delivery_changes")
    op.drop_table("delivery_changes")
    op.drop_index("ix_deliveries_delivery_date", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_index("ix_dogs_user_id", table_name="dogs")
    op.drop_table("dogs")
    op.drop_constraint("uq_users_wa_phone", "users", type_="unique")
    op.drop_column("users", "wa_phone")
