"""monitors fault and cleared kind

Revision ID: 83d7ac98f78d
Revises: b3344354c124
Create Date: 2026-10-09 16:37:38.284686

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '83d7ac98f78d'
down_revision: Union[str, Sequence[str], None] = 'b3344354c124'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KINDS = ('imported', 'first_checkin', 'missing', 'recovered', 'alert', 'paused', 'resumed')


def _kind_check(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(f"'{k}'" for k in kinds) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    # #28: the open fault, and the event that closes one. Expand-only: the
    # previous release ignores both columns and never writes 'cleared'. No
    # backfill, so a fault open at deploy reports once more (spec F10).
    op.add_column('monitors', sa.Column('fault_since', sa.DateTime(timezone=True), nullable=True))
    op.add_column('monitors', sa.Column('fault_key', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True))
    op.drop_constraint('ck_monitor_events_kind', 'monitor_events', type_='check')
    op.create_check_constraint('ck_monitor_events_kind', 'monitor_events', _kind_check(_KINDS + ('cleared',)))


def downgrade() -> None:
    """Downgrade schema."""
    # Fails, by design, while any 'cleared' row exists: the narrower check
    # is validated against every row. Delete them first to go back.
    op.drop_constraint('ck_monitor_events_kind', 'monitor_events', type_='check')
    op.create_check_constraint('ck_monitor_events_kind', 'monitor_events', _kind_check(_KINDS))
    op.drop_column('monitors', 'fault_key')
    op.drop_column('monitors', 'fault_since')
