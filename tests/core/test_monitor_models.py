"""Database-level rules for monitors and their event history.

The constraints live in Postgres, not only in Python, because the import
script and a hand-run ``psql`` reach the tables without passing through any
domain code.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from src.core.models import Monitor, MonitorEvent, Tenant
from src.core.models.monitor_event import EVENT_KINDS
from src.core.monitors import EventKind


async def _monitor(db_session, tenant, **overrides) -> Monitor:
    fields = {"tenant_id": tenant.id, "name": "co-broker", "interval_seconds": 600}
    fields.update(overrides)
    monitor = Monitor(**fields)
    db_session.add(monitor)
    await db_session.flush()
    return monitor


async def _count(db_session, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in where.items():
        stmt = stmt.where(getattr(model, column) == value)
    return int((await db_session.execute(stmt)).scalar_one())


class TestMonitor:
    async def test_starts_pending_enabled_and_silent(self, db_session, tenant):
        monitor = await _monitor(db_session, tenant)
        await db_session.refresh(monitor)
        assert monitor.state == "pending"
        assert monitor.enabled is True
        assert monitor.grace_seconds == 0
        assert monitor.channel_ids == []
        assert monitor.last_variables == {}

    async def test_keeps_the_dispatch_to_redeliver_and_when(self, db_session, tenant):
        """#10: the dispatch ``last_alert_status`` describes, and its next retry."""
        due = datetime(2026, 9, 9, 12, 1, tzinfo=UTC)
        monitor = await _monitor(
            db_session,
            tenant,
            last_alert_status="failed",
            last_alert_dispatch_id="01J0000000000000000000DISP",
            last_alert_redeliver_at=due,
        )
        await db_session.refresh(monitor)
        assert monitor.last_alert_dispatch_id == "01J0000000000000000000DISP"
        assert monitor.last_alert_redeliver_at == due

    async def test_has_no_template_id(self):
        """co-status stores no templates (spec D5); a template_id would name
        a row in a database it cannot read."""
        assert "template_id" not in Monitor.__table__.columns

    async def test_names_are_unique_per_tenant(self, db_session, tenant):
        await _monitor(db_session, tenant)
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await _monitor(db_session, tenant)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"state": "asleep"},
            {"interval_seconds": 0},
            {"grace_seconds": -1},
            {"renotify_seconds": 0},
        ],
        ids=["state", "interval", "grace", "renotify"],
    )
    async def test_check_constraints_hold(self, db_session, tenant, overrides):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await _monitor(db_session, tenant, **overrides)

    async def test_goes_with_its_tenant(self, db_session):
        tenant = Tenant(name="cascade-tenant")
        db_session.add(tenant)
        await db_session.flush()
        monitor = await _monitor(db_session, tenant)
        await db_session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        assert await _count(db_session, Monitor, id=monitor.id) == 0


class TestMonitorEvent:
    @pytest.mark.parametrize("kind", list(EventKind), ids=str)
    async def test_accepts_every_kind(self, db_session, tenant, kind):
        monitor = await _monitor(db_session, tenant)
        event = MonitorEvent(monitor_id=monitor.id, kind=kind, at=datetime.now(UTC))
        db_session.add(event)
        await db_session.flush()
        assert (event.dispatch_id, event.dispatch_status) == (None, None)

    async def test_keeps_its_dispatchs_delivery_status(self, db_session, tenant):
        """Accepted is not delivered (#8): the status sits beside the dispatch id."""
        monitor = await _monitor(db_session, tenant)
        event = MonitorEvent(
            monitor_id=monitor.id,
            kind=EventKind.ALERT,
            at=datetime.now(UTC),
            dispatch_id="01J0000000000000000000DISP",
            dispatch_status="partial",
        )
        db_session.add(event)
        await db_session.flush()
        await db_session.refresh(event)
        assert event.dispatch_status == "partial"

    async def test_latest_notice_of_a_kind_is_one_index_probe(self, db_session):
        """The API serves each monitor's latest recovery and report with a
        status (#20). Without this index, a monitor that reports every tick
        makes each read walk its whole history."""
        indexdef = (
            await db_session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_monitor_events_notice'")
            )
        ).scalar_one()
        assert "(monitor_id, kind, at)" in indexdef
        assert "WHERE (dispatch_status IS NOT NULL)" in indexdef

    async def test_refuses_an_unknown_kind(self, db_session, tenant):
        monitor = await _monitor(db_session, tenant)
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                db_session.add(
                    MonitorEvent(monitor_id=monitor.id, kind="exploded", at=datetime.now(UTC))
                )
                await db_session.flush()

    async def test_carries_no_variables(self):
        """History records that something happened, never what a consumer
        reported (spec D9): a public status page is built on these rows."""
        assert "variables" not in MonitorEvent.__table__.columns

    async def test_goes_with_its_monitor(self, db_session, tenant):
        monitor = await _monitor(db_session, tenant)
        db_session.add(
            MonitorEvent(monitor_id=monitor.id, kind=EventKind.IMPORTED, at=datetime.now(UTC))
        )
        await db_session.flush()
        await db_session.execute(delete(Monitor).where(Monitor.id == monitor.id))
        assert await _count(db_session, MonitorEvent, monitor_id=monitor.id) == 0

    async def test_the_constraint_and_the_enum_agree(self):
        """The migration spells the kinds without importing domain code, so
        this is the one place a kind added to one list and not the other
        fails."""
        assert EVENT_KINDS == tuple(k.value for k in EventKind)
