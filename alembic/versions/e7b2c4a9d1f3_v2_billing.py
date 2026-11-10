"""v2 billing: per-delivery billing mode/amount, monthly amounts, change snapshots

Revision ID: e7b2c4a9d1f3
Revises: c3a9e1d4b7f2
Create Date: 2026-11-10 16:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e7b2c4a9d1f3"
down_revision: Union[str, Sequence[str], None] = "c3a9e1d4b7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # charge = delivered meal, billed its share
    # credit = counted slot, no charge; its share stays in the customer's balance (PAUSE)
    # skip   = not a slot; the month's amount is spread over the other slots (HOLIDAY)
    # hold   = sheet shows a formula error; counted, never billed automatically
    op.add_column("deliveries", sa.Column("billing", sa.String(8), nullable=True))
    op.execute("""
        UPDATE deliveries SET billing = CASE
            WHEN kind = 'error' THEN 'hold'
            WHEN billable THEN 'charge'
            ELSE 'credit' END
    """)
    op.alter_column("deliveries", "billing", nullable=False)
    op.create_check_constraint(
        "ck_deliveries_billing", "deliveries", "billing IN ('charge', 'credit', 'skip', 'hold')"
    )
    op.add_column("deliveries", sa.Column("amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("deliveries", sa.Column("billed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True))
    op.execute("UPDATE deliveries SET billed_at = now() WHERE billed_ledger_id IS NOT NULL")

    op.add_column("delivery_changes", sa.Column("old_billing", sa.String(8), nullable=True))
    op.add_column("delivery_changes", sa.Column("new_billing", sa.String(8), nullable=True))
    op.add_column("delivery_changes", sa.Column("new_cell", JSONB(), nullable=True))

    op.create_table(
        "monthly_amounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dog_id", sa.Integer(), sa.ForeignKey("dogs.id"), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("dog_id", "month", name="uq_monthly_amounts_dog_month"),
        sa.CheckConstraint("amount >= 0", name="ck_monthly_amounts_nonneg"),
    )

    # Prices now come only from the Payments tab (monthly_amounts); this was never filled.
    op.drop_column("dogs", "monthly_rate")


def downgrade():
    op.add_column("dogs", sa.Column("monthly_rate", sa.Numeric(10, 2), nullable=True))
    op.drop_table("monthly_amounts")
    op.drop_column("delivery_changes", "new_cell")
    op.drop_column("delivery_changes", "new_billing")
    op.drop_column("delivery_changes", "old_billing")
    op.drop_column("deliveries", "order_id")
    op.drop_column("deliveries", "billed_at")
    op.drop_column("deliveries", "amount")
    op.drop_constraint("ck_deliveries_billing", "deliveries", type_="check")
    op.drop_column("deliveries", "billing")
