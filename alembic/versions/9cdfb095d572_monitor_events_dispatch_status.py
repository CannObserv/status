"""monitor_events dispatch_status

Revision ID: 9cdfb095d572
Revises: 2f3447a8d894
Create Date: 2026-10-05 03:55:34.027484

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9cdfb095d572'
down_revision: Union[str, Sequence[str], None] = '2f3447a8d894'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no default: a notice already sent reads as "not known
    # undelivered" (#8). Expand-only; the previous release ignores it.
    op.add_column('monitor_events', sa.Column('dispatch_status', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('monitor_events', 'dispatch_status')
