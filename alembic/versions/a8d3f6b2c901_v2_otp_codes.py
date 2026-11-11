"""v2: otp_codes for WhatsApp login

Revision ID: a8d3f6b2c901
Revises: e7b2c4a9d1f3
Create Date: 2026-11-11 16:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a8d3f6b2c901"
down_revision: Union[str, Sequence[str], None] = "e7b2c4a9d1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        "otp_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("wa_phone", sa.String(15), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(12), nullable=False, server_default="sent"),
        sa.Column("wa_message_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('sent', 'failed', 'used', 'dead', 'replaced')", name="ck_otp_codes_status"),
    )
    op.create_index("ix_otp_codes_phone_created", "otp_codes", ["wa_phone", "created_at"])
    op.create_index("ix_otp_codes_created", "otp_codes", ["created_at"])


def downgrade():
    op.drop_index("ix_otp_codes_created", table_name="otp_codes")
    op.drop_index("ix_otp_codes_phone_created", table_name="otp_codes")
    op.drop_table("otp_codes")
