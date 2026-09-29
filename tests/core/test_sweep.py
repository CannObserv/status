"""The pass that watches for silence (spec § The sweep).

Ported from notifier's sweep tests; delivery now leaves through the fake
notifier, and the one rule co-status changes is tested end to end: a monitor
marked ``missing`` while notifier was unreachable is still owed its alert,
and gets it — once, under the same idempotency key — when notifier answers.
"""

import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from src.core.alerting import EndpointMismatch, missing_key
from src.core.models import MonitorEvent
from src.core.models.monitor import Monitor
from src.core.monitors import MISSING_TITLE, MonitorState
from src.core.sweep import sweep_monitors
from tests.conftest import CHANNELS

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


async def _save(db_session, tenant, **overrides) -> Monitor:
    fields = {
        "tenant_id": tenant.id,
        "name": f"m-{secrets.token_hex(4)}",
        "interval_seconds": 600,
        "grace_seconds": 1200,
        "channel_ids": CHANNELS,
        "title_template": "t",
        "body_template": "b",
        "state": MonitorState.OK,
        "created_at": NOW - timedelta(days=2),
        "last_checkin_at": NOW - timedelta(hours=1),
    }
    fields.update(overrides)
    monitor = Monitor(**fields)
    db_session.add(monitor)
    await db_session.flush()
    return monitor


async def _events(db_session, monitor) -> list[MonitorEvent]:
    result = await db_session.execute(
        select(MonitorEvent).where(MonitorEvent.monitor_id == monitor.id)
    )
    return list(result.scalars().all())


class TestSweepMonitors:
    async def test_an_overdue_monitor_is_alerted_and_marked_missing(
        self, db_session, tenant, alerter, notifier
    ):
        monitor = await _save(db_session, tenant)
        expected_key = missing_key(monitor, NOW)

        report = await sweep_monitors(db_session, alerter, NOW)

        assert report.alerted == [str(monitor.id)]
        (sent,) = notifier.dispatched()
        assert sent["title_template"] == MISSING_TITLE
        assert sent["variables"]["name"] == monitor.name
        assert sent["idempotency_key"] == expected_key
        assert sent["metadata"] == {"monitor_id": str(monitor.id), "reason": "missing"}
        assert monitor.state == MonitorState.MISSING
        assert monitor.last_alert_at == NOW
        (event,) = await _events(db_session, monitor)
        assert event.kind == "missing"
        assert event.dispatch_id is not None

    async def test_a_healthy_monitor_is_left_alone(self, db_session, tenant, alerter, notifier):
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.checked == 1
        assert report.alerted == []
        assert monitor.state == MonitorState.OK
        assert not notifier.dispatch.called

    async def test_a_disabled_monitor_is_skipped_entirely(
        self, db_session, tenant, alerter, notifier
    ):
        await _save(db_session, tenant, enabled=False)
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.checked == 0
        assert not notifier.dispatch.called

    async def test_an_already_missing_monitor_is_not_re_alerted(
        self, db_session, tenant, alerter, notifier
    ):
        await _save(
            db_session,
            tenant,
            state=MonitorState.MISSING,
            last_alert_at=NOW - timedelta(minutes=5),
        )
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.alerted == []
        assert not notifier.dispatch.called

    async def test_a_failed_delivery_is_still_an_accepted_alert(
        self, db_session, tenant, alerter, notifier
    ):
        """notifier took it; that Slack bounced is notifier's record to keep.
        The state describes the consumer, not our luck reaching Slack."""
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
        monitor = await _save(db_session, tenant)
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.alerted == [str(monitor.id)]
        assert monitor.last_alert_at == NOW

    async def test_a_monitor_with_no_channels_is_marked_but_not_dispatched(
        self, db_session, tenant, alerter, notifier
    ):
        monitor = await _save(db_session, tenant, channel_ids=[])
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undeliverable == [str(monitor.id)]
        assert monitor.state == MonitorState.MISSING
        assert not notifier.dispatch.called

    async def test_defaults_to_the_wall_clock(self, db_session, tenant, alerter, notifier):
        await _save(db_session, tenant, last_checkin_at=datetime.now(UTC) - timedelta(days=1))
        report = await sweep_monitors(db_session, alerter)
        assert len(report.alerted) == 1


class TestTheOwedAlert:
    """The rule co-status changes: late instead of never."""

    async def test_an_unreachable_notifier_leaves_the_alert_owed(
        self, db_session, tenant, alerter, notifier
    ):
        notifier.dispatch.mock(side_effect=httpx.ConnectError("refused"))
        monitor = await _save(db_session, tenant)

        report = await sweep_monitors(db_session, alerter, NOW)

        assert report.owed == [str(monitor.id)]
        assert monitor.state == MonitorState.MISSING  # the consumer *is* missing
        assert monitor.last_alert_at is None  # but nobody has been told
        (event,) = await _events(db_session, monitor)
        assert (event.kind, event.dispatch_id) == ("missing", None)

    async def test_the_next_pass_delivers_it_once_under_the_same_key(
        self, db_session, tenant, alerter, notifier
    ):
        monitor = await _save(db_session, tenant)
        notifier.dispatch.mock(side_effect=httpx.ConnectError("refused"))
        await sweep_monitors(db_session, alerter, NOW)
        first_key = missing_key(monitor, NOW)

        notifier.dispatch.mock(side_effect=None)
        notifier.dispatch.respond(
            202,
            json={
                "id": "01J0000000000000000000DISP",
                "tenant_id": "01J0000000000000000000TENT",
                "template_id": None,
                "idempotency_key": first_key,
                "rendered_title": "t",
                "rendered_body": "b",
                "status": "succeeded",
                "metadata": {},
                "created_at": "2026-09-09T12:00:00Z",
                "attempts": [],
            },
        )
        report = await sweep_monitors(db_session, alerter, NOW + timedelta(minutes=1))

        assert report.alerted == [str(monitor.id)]
        assert monitor.last_alert_at == NOW + timedelta(minutes=1)
        last = notifier.dispatched()[-1]
        assert last["idempotency_key"] == first_key
        # One state change, one event: the retry is not a second outage.
        assert [e.kind for e in await _events(db_session, monitor)] == ["missing"]

        again = await sweep_monitors(db_session, alerter, NOW + timedelta(minutes=2))
        assert again.alerted == []

    async def test_notifier_unreachable_at_health_still_marks_and_owes(
        self, db_session, tenant, alerter, notifier
    ):
        notifier.health.mock(side_effect=httpx.ConnectError("refused"))
        monitor = await _save(db_session, tenant)
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.owed == [str(monitor.id)]
        assert not notifier.dispatch.called


class TestEndpointCheck:
    async def test_a_notifier_in_the_other_environment_fails_the_pass(
        self, db_session, tenant, alerter, notifier
    ):
        """Loudly, as a failed unit, and before anything is written."""
        notifier.health.respond(json={"status": "ok", "environment": "production"})
        monitor = await _save(db_session, tenant)
        with pytest.raises(EndpointMismatch):
            await sweep_monitors(db_session, alerter, NOW)
        assert monitor.state == MonitorState.OK
        assert not notifier.dispatch.called

    async def test_the_report_says_notifier_answered(self, db_session, tenant, alerter, notifier):
        """What the ``notifier-reachable`` heartbeat is built on (#1)."""
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.notifier_ok is True

    async def test_the_report_says_notifier_did_not_answer(
        self, db_session, tenant, alerter, notifier
    ):
        notifier.health.mock(side_effect=httpx.ConnectError("refused"))
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.notifier_ok is False
