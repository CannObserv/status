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
from src.core.monitors import MISSING_TITLE, EventKind, MonitorState
from src.core.sweep import NOTICE_WINDOW, sweep_monitors
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


def _dispatch_record(status: str, key: str = "k") -> dict:
    """A dispatch notifier accepted, with delivery *status*."""
    return {
        "id": "01J0000000000000000000DISP",
        "tenant_id": "01J0000000000000000000TENT",
        "template_id": None,
        "idempotency_key": key,
        "rendered_title": "t",
        "rendered_body": "b",
        "status": status,
        "metadata": {},
        "created_at": "2026-09-09T12:00:00Z",
        "attempts": [],
    }


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
        notifier.dispatch.respond(202, json=_dispatch_record("failed"))
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
        notifier.dispatch.respond(202, json=_dispatch_record("succeeded", first_key))
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


class TestTheUndeliveredAlert:
    """Accepted is not delivered (#6): a missing monitor whose last alert
    notifier could not deliver stays in ``undelivered`` until that changes."""

    @pytest.mark.parametrize("status", ["failed", "partial"])
    async def test_an_undelivered_alert_is_reported(
        self, db_session, tenant, alerter, notifier, status
    ):
        notifier.dispatch.respond(202, json=_dispatch_record(status))
        monitor = await _save(db_session, tenant)

        report = await sweep_monitors(db_session, alerter, NOW)

        assert report.undelivered == {str(monitor.id): status}
        assert monitor.last_alert_status == status
        (event,) = await _events(db_session, monitor)
        assert (event.kind, event.dispatch_status) == ("missing", status)

    async def test_a_delivered_alert_is_not(self, db_session, tenant, alerter, notifier):
        monitor = await _save(db_session, tenant)
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered == {}
        assert monitor.last_alert_status == "succeeded"

    async def test_it_stays_reported_on_passes_that_send_nothing(
        self, db_session, tenant, alerter, notifier
    ):
        """One /fail followed by a success 60s later would read as resolved."""
        notifier.dispatch.respond(202, json=_dispatch_record("failed"))
        monitor = await _save(db_session, tenant)
        await sweep_monitors(db_session, alerter, NOW)

        report = await sweep_monitors(db_session, alerter, NOW + timedelta(minutes=1))

        assert report.alerted == []
        assert report.undelivered == {str(monitor.id): "failed"}

    async def test_a_delivered_renotify_clears_it(self, db_session, tenant, alerter, notifier):
        notifier.dispatch.respond(202, json=_dispatch_record("failed"))
        monitor = await _save(db_session, tenant, renotify_seconds=3600)
        await sweep_monitors(db_session, alerter, NOW)

        notifier.dispatch.respond(202, json=_dispatch_record("succeeded"))
        report = await sweep_monitors(db_session, alerter, NOW + timedelta(hours=1))

        assert report.alerted == [str(monitor.id)]
        assert report.undelivered == {}

    async def test_a_monitor_no_longer_missing_is_not_reported(
        self, db_session, tenant, alerter, notifier
    ):
        """Recovered: the outage the alert was about is over."""
        await _save(
            db_session,
            tenant,
            last_checkin_at=NOW - timedelta(minutes=5),
            last_alert_at=NOW - timedelta(hours=1),
            last_alert_status="failed",
        )
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered == {}

    async def test_a_new_outage_forgets_the_last_ones_status(
        self, db_session, tenant, alerter, notifier
    ):
        """Owed is not undelivered: nothing about this outage was sent yet."""
        notifier.dispatch.mock(side_effect=httpx.ConnectError("refused"))
        monitor = await _save(
            db_session,
            tenant,
            last_alert_at=NOW - timedelta(days=1),
            last_alert_status="failed",
        )

        report = await sweep_monitors(db_session, alerter, NOW)

        assert report.owed == [str(monitor.id)]
        assert report.undelivered == {}
        assert monitor.last_alert_status is None


async def _notice(
    db_session,
    monitor,
    kind: EventKind,
    status: str | None,
    at: datetime = NOW - timedelta(hours=1),
) -> MonitorEvent:
    """A check-in notice as the route records it; ``status=None`` was not accepted."""
    event = MonitorEvent(
        monitor_id=monitor.id,
        kind=kind,
        at=at,
        dispatch_id="01J0000000000000000000DISP" if status else None,
        dispatch_status=status,
    )
    db_session.add(event)
    await db_session.flush()
    return event


#: Each check-in notice's event kind, and its name in the report.
NOTICES = [(EventKind.RECOVERED, "recovery"), (EventKind.ALERT, "report")]


class TestTheUndeliveredNotice:
    """The check-in path's notices (#8): recovery and report. The latest of
    each kind, if notifier accepted it and did not deliver it, is reported on
    every pass until a later one of that kind is delivered, or the window ends."""

    @pytest.mark.parametrize("status", ["failed", "partial"])
    @pytest.mark.parametrize(("kind", "label"), NOTICES)
    async def test_an_undelivered_notice_is_reported(
        self, db_session, tenant, alerter, notifier, kind, label, status
    ):
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        await _notice(db_session, monitor, kind, status)

        report = await sweep_monitors(db_session, alerter, NOW)

        assert report.undelivered_notices == {str(monitor.id): {label: status}}
        assert not notifier.dispatch.called

    @pytest.mark.parametrize(("kind", "label"), NOTICES)
    async def test_a_delivered_notice_is_not(
        self, db_session, tenant, alerter, notifier, kind, label
    ):
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        await _notice(db_session, monitor, kind, "succeeded")
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered_notices == {}

    @pytest.mark.parametrize(("kind", "label"), NOTICES)
    async def test_a_later_delivered_one_of_its_kind_clears_it(
        self, db_session, tenant, alerter, notifier, kind, label
    ):
        """A consumer alerting every tick: its newest report got through."""
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        await _notice(db_session, monitor, kind, "failed", at=NOW - timedelta(hours=2))
        await _notice(db_session, monitor, kind, "succeeded", at=NOW - timedelta(hours=1))
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered_notices == {}

    async def test_a_delivered_recovery_does_not_clear_a_report(
        self, db_session, tenant, alerter, notifier
    ):
        """'It is back' does not carry 'here is what it found'."""
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        await _notice(db_session, monitor, EventKind.ALERT, "failed", at=NOW - timedelta(hours=2))
        await _notice(db_session, monitor, EventKind.RECOVERED, "succeeded")
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered_notices == {str(monitor.id): {"report": "failed"}}

    async def test_a_later_notice_notifier_did_not_take_does_not_hide_it(
        self, db_session, tenant, alerter, notifier
    ):
        """Not accepted is not delivered either."""
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        await _notice(db_session, monitor, EventKind.ALERT, "failed", at=NOW - timedelta(hours=2))
        await _notice(db_session, monitor, EventKind.ALERT, None)
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered_notices == {str(monitor.id): {"report": "failed"}}

    async def test_it_is_reported_until_the_window_ends(
        self, db_session, tenant, alerter, notifier
    ):
        """A one-off report has no next one to clear it, and nothing resends (#7)."""
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        await _notice(db_session, monitor, EventKind.ALERT, "failed", at=NOW - NOTICE_WINDOW)

        inside = await sweep_monitors(db_session, alerter, NOW - timedelta(seconds=1))
        after = await sweep_monitors(db_session, alerter, NOW + timedelta(seconds=1))

        assert inside.undelivered_notices == {str(monitor.id): {"report": "failed"}}
        assert after.undelivered_notices == {}

    async def test_a_monitor_can_carry_both(self, db_session, tenant, alerter, notifier):
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        await _notice(db_session, monitor, EventKind.RECOVERED, "partial")
        await _notice(db_session, monitor, EventKind.ALERT, "failed")
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered_notices == {
            str(monitor.id): {"recovery": "partial", "report": "failed"}
        }

    async def test_a_missing_events_status_is_not_a_notice(
        self, db_session, tenant, alerter, notifier
    ):
        """Missing alerts are #6's ``undelivered``, from ``last_alert_status``."""
        monitor = await _save(db_session, tenant, last_checkin_at=NOW - timedelta(minutes=5))
        await _notice(db_session, monitor, EventKind.MISSING, "failed")
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered_notices == {}

    async def test_a_disabled_monitor_is_not_reported(self, db_session, tenant, alerter, notifier):
        """As with #6: pausing hides it, resuming brings it back."""
        monitor = await _save(db_session, tenant, enabled=False)
        await _notice(db_session, monitor, EventKind.ALERT, "failed")
        report = await sweep_monitors(db_session, alerter, NOW)
        assert report.undelivered_notices == {}


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
