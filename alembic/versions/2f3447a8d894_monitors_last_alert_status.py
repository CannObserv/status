"""monitors last_alert_status

Revision ID: 2f3447a8d894
Revises: 5910465f86f7
Create Date: 2026-09-29 18:05:51.065296

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f3447a8d894'
down_revision: Union[str, Sequence[str], None] = '5910465f86f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no default: a monitor already alerted reads as "not known
    # undelivered" until its next alert (#6).
    op.add_column('monitors', sa.Column('last_alert_status', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('monitors', 'last_alert_status')
