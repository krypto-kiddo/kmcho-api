"""v2 opening balances and payments received

Revision ID: b5e1c7d3f402
Revises: a8d3f6b2c901
Create Date: 2026-11-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5e1c7d3f402"
down_revision: Union[str, Sequence[str], None] = "a8d3f6b2c901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opening_balances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("dues", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("source_rows", sa.String(), nullable=True),
        sa.Column("old_balance", sa.Numeric(10, 2), nullable=True),
        sa.Column("ledger_id", sa.Integer(), sa.ForeignKey("ledger.id"), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "payments_received",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("mode", sa.String(40), nullable=True),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("sheet_key", sa.String(200), nullable=False, unique=True),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("ledger_id", sa.Integer(), sa.ForeignKey("ledger.id"), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("payments_received")
    op.drop_table("opening_balances")
