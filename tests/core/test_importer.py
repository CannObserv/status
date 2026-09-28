"""Importing one monitor from notifier (spec § Cutover, step 2).

The row is notifier's, exported read-only; everything the deadline depends on
must arrive unchanged, or the handover either fires a false alarm or leaves a
window nobody watches.
"""

import secrets
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.core.importer import ImportRefused, import_monitor
from src.core.models import Monitor, MonitorEvent, Tenant

OLD = ["01J0000000000000000000OLD1", "01J0000000000000000000OLD2"]
NEW = ["01J0000000000000000000NEW1", "01J0000000000000000000NEW2"]
MAP = dict(zip(OLD, NEW, strict=True))


def _row(**overrides) -> dict:
    """One monitor as the runbook's export query prints it."""
    row = {
        "id": "01J00000000000000000M0NTR1",
        "tenant_name": "watcher",
        "name": "watcher-backup",
        "enabled": True,
        "interval_seconds": 86400,
        "grace_seconds": 7200,
        "renotify_seconds": 86400,
        "channel_ids": OLD,
        "template_id": None,
        "title_template": "T {{ source }}",
        "body_template": "B",
        "state": "ok",
        "last_checkin_at": "2026-09-25T03:20:22.248198+00:00",
        "last_status": "ok",
        "last_variables": {"source": "watcher"},
        "last_alert_at": None,
        "created_at": "2026-09-14T20:40:00.000000+00:00",
    }
    row.update(overrides)
    return row


@pytest.fixture
async def co_watcher(db_session) -> Tenant:
    tenant = Tenant(name="co-watcher")
    db_session.add(tenant)
    await db_session.commit()
    return tenant


async def _get(db_session, monitor_id: str) -> Monitor | None:
    result = await db_session.execute(select(Monitor).where(Monitor.id == monitor_id))
    return result.scalar_one_or_none()


class TestImport:
    async def test_keeps_everything_the_deadline_depends_on(self, db_session, co_watcher):
        await import_monitor(db_session, _row(), MAP, dry_run=False)
        monitor = await _get(db_session, _row()["id"])
        assert monitor.id == "01J00000000000000000M0NTR1"  # consumers keep their id (D6)
        assert monitor.last_checkin_at == datetime(2026, 9, 25, 3, 20, 22, 248198, tzinfo=UTC)
        assert monitor.created_at == datetime(2026, 9, 14, 20, 40, tzinfo=UTC)
        assert (monitor.interval_seconds, monitor.grace_seconds) == (86400, 7200)
        assert monitor.renotify_seconds == 86400
        assert monitor.state == "ok"
        assert monitor.last_status == "ok"
        assert monitor.last_variables == {"source": "watcher"}
        assert monitor.last_alert_at is None
        assert monitor.title_template == "T {{ source }}"

    async def test_arrives_disabled(self, db_session, co_watcher):
        """Notifier watches until the handover (spec § Cutover, step 5)."""
        await import_monitor(db_session, _row(enabled=True), MAP, dry_run=False)
        assert (await _get(db_session, _row()["id"])).enabled is False

    async def test_renames_watcher_to_the_cohort_spelling(self, db_session, co_watcher):
        result = await import_monitor(db_session, _row(), MAP, dry_run=False)
        monitor = await _get(db_session, _row()["id"])
        assert monitor.tenant_id == co_watcher.id
        assert monitor.name == "co-watcher-backup"
        assert (result.tenant_name, result.name) == ("co-watcher", "co-watcher-backup")

    async def test_other_names_pass_through(self, db_session):
        db_session.add(Tenant(name="co-broker"))
        await db_session.commit()
        row = _row(id="01J00000000000000000M0NTR2", tenant_name="co-broker", name="co-broker")
        result = await import_monitor(db_session, row, MAP, dry_run=False)
        assert (result.tenant_name, result.name) == ("co-broker", "co-broker")

    async def test_rewrites_channel_ids_through_the_map(self, db_session, co_watcher):
        await import_monitor(db_session, _row(), MAP, dry_run=False)
        assert (await _get(db_session, _row()["id"])).channel_ids == NEW

    async def test_history_begins_with_the_import(self, db_session, co_watcher):
        await import_monitor(db_session, _row(), MAP, dry_run=False)
        events = (
            await db_session.execute(
                select(MonitorEvent)
                .where(MonitorEvent.monitor_id == _row()["id"])
                .order_by(MonitorEvent.kind)
            )
        ).scalars()
        assert sorted(e.kind for e in events) == ["imported", "paused"]


class TestRefusals:
    async def test_refuses_a_template_id(self, db_session, co_watcher):
        """No monitor uses one today; if one ever does, it cannot come here."""
        with pytest.raises(ImportRefused, match="template_id"):
            await import_monitor(db_session, _row(template_id="01J0TEMPLATE00000000000000"), MAP)

    async def test_refuses_an_id_that_already_exists(self, db_session, co_watcher):
        await import_monitor(db_session, _row(), MAP, dry_run=False)
        with pytest.raises(ImportRefused, match="already"):
            await import_monitor(db_session, _row(), MAP, dry_run=False)

    async def test_refuses_a_tenant_co_status_does_not_have(self, db_session):
        with pytest.raises(ImportRefused, match="co-watcher"):
            await import_monitor(db_session, _row(), MAP)

    async def test_refuses_a_channel_the_map_does_not_cover(self, db_session, co_watcher):
        with pytest.raises(ImportRefused, match=OLD[1]):
            await import_monitor(db_session, _row(), {OLD[0]: NEW[0]})

    async def test_refuses_a_row_missing_a_field(self, db_session, co_watcher):
        row = _row()
        del row["last_checkin_at"]
        with pytest.raises(ImportRefused, match="last_checkin_at"):
            await import_monitor(db_session, row, MAP)

    async def test_a_row_the_database_rejects_is_refused_by_name(self, db_session, co_watcher):
        with pytest.raises(ImportRefused, match="constraint"):
            await import_monitor(db_session, _row(state="asleep"), MAP)
        assert await _get(db_session, _row()["id"]) is None


class TestDryRun:
    async def test_is_the_default_and_writes_nothing(self, db_session, co_watcher):
        result = await import_monitor(db_session, _row(), MAP)
        assert result.name == "co-watcher-backup"
        assert await _get(db_session, _row()["id"]) is None

    async def test_still_refuses(self, db_session):
        """A rehearsal that passes where the real run would fail certifies
        the wrong thing."""
        with pytest.raises(ImportRefused):
            await import_monitor(db_session, _row(name=f"x-{secrets.token_hex(2)}"), MAP)
