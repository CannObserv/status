"""MonitorEvent model — what happened to a monitor, and when (spec D9).

State changes only: the first check-in, going missing, recovering, an
``alert`` check-in, being paused or resumed, and the import that began the
record. Not every check-in — that would be ~290 rows a day that nothing
planned reads.

These rows are what future uptime figures and incident lists are built from,
and a history not recorded now cannot be rebuilt later. They never carry a
consumer's ``variables``: a public status page will be built on this table.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base, ULIDType, generate_ulid

#: Spelled here as well as in ``src.core.monitors.EventKind`` because a
#: migration must not import domain code; ``tests/core/test_monitor_models.py``
#: holds the two in step.
EVENT_KINDS = (
    "imported",
    "first_checkin",
    "missing",
    "recovered",
    "alert",
    "paused",
    "resumed",
)


class MonitorEvent(Base):
    """One state change of one monitor."""

    __tablename__ = "monitor_events"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in EVENT_KINDS) + ")",
            name="ck_monitor_events_kind",
        ),
        # History is read per monitor, newest first.
        Index("ix_monitor_events_monitor_at", "monitor_id", "at"),
    )

    id: Mapped[str] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    monitor_id: Mapped[str] = mapped_column(
        ULIDType, ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: notifier's dispatch id for the alert this event sent, when notifier
    #: accepted one. A pointer into another service's log, so not a key.
    dispatch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: notifier's delivery ``status`` for that dispatch (``succeeded``,
    #: ``partial`` or ``failed``); ``None`` when there is no dispatch id, or
    #: the row predates #8 (not known undelivered).
    #: Accepted is not delivered: the sweep surfaces a check-in notice whose
    #: status is not ``succeeded`` (#8).
    dispatch_status: Mapped[str | None] = mapped_column(String, nullable=True)
