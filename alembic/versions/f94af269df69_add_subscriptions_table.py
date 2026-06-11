"""add_subscriptions_table

Revision ID: f94af269df69
Revises: e5991df7cd1f
Create Date: 2026-06-11 13:22:59.600526

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = 'f94af269df69'
down_revision: Union[str, Sequence[str], None] = 'e5991df7cd1f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        'subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, unique=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('paused_until', sa.Date(), nullable=True),
        sa.Column('bowl', JSONB(), nullable=True),
        sa.Column('broth', JSONB(), nullable=True),
        sa.Column('protein_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('base_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

def downgrade():
    op.drop_table('subscriptions')
