"""monitor_events notice index

Revision ID: 535ffdac4d2f
Revises: 9cdfb095d572
Create Date: 2026-10-06 00:52:32.348977

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '535ffdac4d2f'
down_revision: Union[str, Sequence[str], None] = '9cdfb095d572'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The API's latest notice of each kind (#20). Expand-only; the previous
    # release never reads it. Not CONCURRENTLY, which cannot run in the
    # migration's transaction: the table is small, so the write lock is brief.
    op.create_index('ix_monitor_events_notice', 'monitor_events', ['monitor_id', 'kind', 'at'], unique=False, postgresql_where=sa.text('dispatch_status IS NOT NULL'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_monitor_events_notice', table_name='monitor_events', postgresql_where=sa.text('dispatch_status IS NOT NULL'))
