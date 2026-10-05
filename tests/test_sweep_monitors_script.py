"""Tests for scripts/sweep_monitors.py — the timer's python entrypoint (notifier#56).

Thin by design: the sweep itself is tested in tests/core/test_sweep.py.
What is only true here is that the pass gets *committed* and that the run
leaves a line in the journal. A sweep that alerts and then rolls back would
re-alert on every tick forever; a sweep that says nothing gives an operator no
way to tell "no monitors are overdue" from "the timer has not fired in a week".
"""

import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from scripts import sweep_monitors
from scripts.sweep_monitors import run_sweep
from src.core import build, schema_state
from src.core.alerting import EndpointMismatch
from src.core.models import MonitorEvent
from src.core.models.monitor import Monitor
from src.core.monitors import EventKind, MonitorState
from src.core.schema_state import SchemaBehind
from src.core.sweep import SweepReport
from tests.conftest import CHANNELS


@pytest.fixture
async def overdue_monitor(db_session, tenant) -> Monitor:
    monitor = Monitor(
        tenant_id=tenant.id,
        name=f"m-{secrets.token_hex(4)}",
        interval_seconds=600,
        grace_seconds=1200,
        channel_ids=CHANNELS,
        title_template="t",
        body_template="b",
        state=MonitorState.OK,
        last_checkin_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(monitor)
    await db_session.flush()
    return monitor


async def test_the_pass_is_committed(db_session, overdue_monitor, alerter, notifier):
    """Not left in the session for a caller that never comes."""
    await run_sweep(db_session, alerter)

    await db_session.rollback()
    persisted = (
        await db_session.execute(select(Monitor).where(Monitor.id == overdue_monitor.id))
    ).scalar_one()
    assert persisted.state == MonitorState.MISSING


async def test_a_quiet_pass_still_reports_what_it_checked(
    db_session, overdue_monitor, alerter, notifier, caplog
):
    """`checked=N alerted=0` is the line that proves the timer is alive."""
    with caplog.at_level("INFO"):
        report = await run_sweep(db_session, alerter)

    assert report.checked >= 1
    assert any("sweep" in record.message for record in caplog.records)


async def test_the_line_names_the_undelivered_monitors(
    db_session, overdue_monitor, alerter, notifier, caplog
):
    """Where an operator goes from a `notifier-reachable` /fail to the monitor (#6)."""
    notifier.dispatch.respond(
        202,
        json={
            "id": "01J0000000000000000000DISP",
            "tenant_id": "01J0000000000000000000TENT",
            "template_id": None,
            "idempotency_key": "k",
            "rendered_title": "t",
            "rendered_body": "b",
            "status": "failed",
            "metadata": {},
            "created_at": "2026-09-09T12:00:00Z",
            "attempts": [],
        },
    )
    with caplog.at_level("INFO"):
        await run_sweep(db_session, alerter)

    (line,) = [r for r in caplog.records if r.message == "monitor sweep complete"]
    assert line.undelivered[str(overdue_monitor.id)] == "failed"


async def test_the_line_names_the_undelivered_notices(
    db_session, overdue_monitor, alerter, notifier, caplog
):
    """The same, for a recovery or report from the check-in path (#8)."""
    db_session.add(
        MonitorEvent(
            monitor_id=overdue_monitor.id,
            kind=EventKind.ALERT,
            at=datetime.now(UTC) - timedelta(hours=1),
            dispatch_id="01J0000000000000000000DISP",
            dispatch_status="partial",
        )
    )
    with caplog.at_level("INFO"):
        await run_sweep(db_session, alerter)

    (line,) = [r for r in caplog.records if r.message == "monitor sweep complete"]
    assert line.undelivered_notices == {str(overdue_monitor.id): {"report": "partial"}}


async def test_the_line_names_the_build_that_ran(
    db_session, overdue_monitor, alerter, notifier, caplog
):
    """#9: the sweep starts fresh every pass, so only its own line can say what ran."""
    with caplog.at_level("INFO"):
        await run_sweep(db_session, alerter)

    (line,) = [r for r in caplog.records if r.message == "monitor sweep complete"]
    assert line.build == build.build_id()


class FakeHeartbeat:
    """Records what the entrypoint told it; healthchecks itself is test_heartbeat's."""

    def __init__(self) -> None:
        self.completed: list[SweepReport] = []
        self.failed: list[BaseException] = []
        #: Every call, in order: ``completed``, ``failed``, ``api``.
        self.calls: list[str] = []

    async def sweep_completed(self, report: SweepReport) -> None:
        self.completed.append(report)
        self.calls.append("completed")

    async def sweep_failed(self, error: BaseException) -> None:
        self.failed.append(error)
        self.calls.append("failed")

    async def api_checked(self) -> None:
        self.calls.append("api")


class TestHeartbeat:
    """#1: the pass reports itself to healthchecks.io, after the commit."""

    async def test_a_completed_pass_is_reported_after_commit(
        self, db_session, overdue_monitor, alerter, notifier
    ):
        heartbeat = FakeHeartbeat()
        report = await run_sweep(db_session, alerter, heartbeat)
        assert heartbeat.completed == [report]
        assert heartbeat.failed == []

    async def test_a_pass_that_raises_is_reported_and_still_raises(
        self, db_session, overdue_monitor, alerter, notifier
    ):
        """A failed unit and a /fail ping: the journal and healthchecks both say so."""
        notifier.health.respond(json={"status": "ok", "environment": "production"})
        heartbeat = FakeHeartbeat()
        with pytest.raises(EndpointMismatch) as raised:
            await run_sweep(db_session, alerter, heartbeat)
        assert heartbeat.failed == [raised.value]
        assert heartbeat.completed == []

    async def test_a_database_behind_the_code_fails_the_pass_by_name(
        self, db_session, overdue_monitor, alerter, notifier
    ):
        """#9: 2026-09-29's 38 column errors, as one named /fail instead.

        The monitor is overdue and must stay unswept: nothing is marked or
        sent against a schema the code does not match.
        """
        oldest = schema_state.script_directory().get_base()
        await db_session.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": oldest})
        heartbeat = FakeHeartbeat()
        with pytest.raises(SchemaBehind) as raised:
            await run_sweep(db_session, alerter, heartbeat)
        assert heartbeat.failed == [raised.value]
        assert notifier.dispatched() == []

    async def test_no_heartbeat_is_a_pass_like_any_other(
        self, db_session, overdue_monitor, alerter, notifier
    ):
        report = await run_sweep(db_session, alerter, None)
        assert report.checked >= 1

    async def test_no_notifier_key_is_reported_as_a_failed_pass(self, monkeypatch):
        heartbeat = FakeHeartbeat()
        monkeypatch.setattr(sweep_monitors, "heartbeat_from_environment", lambda: heartbeat)
        monkeypatch.setattr(sweep_monitors, "alerter_from_environment", lambda: None)
        assert await sweep_monitors.main() == 1
        assert len(heartbeat.failed) == 1


@asynccontextmanager
async def _no_session():
    yield None


class TestApiCheck:
    """#13: every production pass ends by asking whether the API is ready.

    After the pass and its own pings, whatever the pass did: a slow API must
    not delay an alert, and a failed pass says nothing about the API.
    """

    @pytest.fixture
    def heartbeat(self, monkeypatch) -> FakeHeartbeat:
        heartbeat = FakeHeartbeat()
        monkeypatch.setattr(sweep_monitors, "heartbeat_from_environment", lambda: heartbeat)
        monkeypatch.setattr(sweep_monitors, "alerter_from_environment", lambda: object())
        monkeypatch.setattr(sweep_monitors, "get_session_factory", lambda: _no_session)
        return heartbeat

    async def test_after_a_completed_pass(self, heartbeat, monkeypatch):
        async def completes(session, alerter, heartbeat):
            await heartbeat.sweep_completed(SweepReport())

        monkeypatch.setattr(sweep_monitors, "run_sweep", completes)
        assert await sweep_monitors.main() == 0
        assert heartbeat.calls == ["completed", "api"]

    async def test_after_a_pass_that_raised_which_still_raises(self, heartbeat, monkeypatch):
        async def raises(session, alerter, heartbeat):
            await heartbeat.sweep_failed(RuntimeError())
            raise RuntimeError("boom")

        monkeypatch.setattr(sweep_monitors, "run_sweep", raises)
        with pytest.raises(RuntimeError, match="boom"):
            await sweep_monitors.main()
        assert heartbeat.calls == ["failed", "api"]

    async def test_without_a_notifier_key(self, heartbeat, monkeypatch):
        monkeypatch.setattr(sweep_monitors, "alerter_from_environment", lambda: None)
        assert await sweep_monitors.main() == 1
        assert heartbeat.calls == ["failed", "api"]
