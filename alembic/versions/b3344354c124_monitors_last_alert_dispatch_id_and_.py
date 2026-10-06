"""monitors last_alert_dispatch_id and redeliver_at

Revision ID: b3344354c124
Revises: 535ffdac4d2f
Create Date: 2026-10-06 20:15:17.673502

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3344354c124'
down_revision: Union[str, Sequence[str], None] = '535ffdac4d2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no default (expand-only): a monitor already undelivered at
    # deploy has no dispatch to redeliver, and clears as before (#10).
    op.add_column('monitors', sa.Column('last_alert_dispatch_id', sa.String(), nullable=True))
    op.add_column('monitors', sa.Column('last_alert_redeliver_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('monitors', 'last_alert_redeliver_at')
    op.drop_column('monitors', 'last_alert_dispatch_id')
