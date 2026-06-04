"""add porter_link to orders

Revision ID: e5991df7cd1f
Revises: f0224fe50b45
Create Date: 2026-06-04 17:09:12.406723

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5991df7cd1f'
down_revision: Union[str, Sequence[str], None] = 'f0224fe50b45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('porter_link', sa.Text(), nullable=True))

def downgrade() -> None:
    op.drop_column('orders', 'porter_link')