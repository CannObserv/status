"""Dead-man's-timer logic: deadlines, the alert decision, and the wording.

A findings-only push is silent in exactly the cases that matter — a stopped
timer, a wedged process, a dead node all produce zero findings and zero
traffic, indistinguishable from health. CannObserv/observo#473 is what that
costs. So the alerting condition tested here is *absence*: a monitor whose
deadline has passed is itself the alert.

The sweep that acts on this is tested in ``tests/test_sweep.py``.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest

from src.core.models.monitor import Monitor
from src.core.monitors import (
    REDELIVERY_DELAYS,
    CheckinStatus,
    EventKind,
    MonitorState,
    Notice,
    cleared_notification,
    deadline_for,
    format_duration,
    is_overdue,
    missing_notification,
    opens_fault,
    recovery_notification,
    redelivery_due,
    report_due,
    should_alert,
    should_redeliver,
)

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


def _monitor(**overrides) -> Monitor:
    """An unsaved Monitor with the broker's cadence: ten minutes, 20m grace."""
    fields = {
        "tenant_id": "01J0000000000000000000000A",
        "name": "co-broker",
        "enabled": True,
        "interval_seconds": 600,
        "grace_seconds": 1200,
        "renotify_seconds": None,
        "channel_ids": [],
        "title_template": "t",
        "body_template": "b",
        "state": MonitorState.PENDING,
        "created_at": NOW - timedelta(minutes=5),
        "last_checkin_at": None,
        "last_alert_at": None,
    }
    fields.update(overrides)
    return Monitor(**fields)


class TestDeadline:
    """When the next report is due, and what the clock starts from."""

    def test_runs_from_the_last_checkin(self):
        monitor = _monitor(last_checkin_at=NOW - timedelta(minutes=1))
        assert deadline_for(monitor) == NOW - timedelta(minutes=1) + timedelta(seconds=1800)

    def test_falls_back_to_creation_when_nothing_has_ever_checked_in(self):
        """A monitor that never reports at all must still alert.

        Anchoring on ``last_checkin_at`` alone would make a probe that was
        never wired up — the most likely misconfiguration of all — the one
        case the timer stays silent about, forever.
        """
        monitor = _monitor(created_at=NOW - timedelta(hours=2), last_checkin_at=None)
        assert deadline_for(monitor) == NOW - timedelta(hours=2) + timedelta(seconds=1800)

    def test_grace_extends_the_deadline_past_a_single_missed_tick(self):
        """Ten-minute cadence, 20 minutes of grace: two ticks may be lost."""
        monitor = _monitor(last_checkin_at=NOW, interval_seconds=600, grace_seconds=1200)
        assert deadline_for(monitor) == NOW + timedelta(minutes=30)

    def test_naive_timestamps_are_read_as_utc(self):
        """Postgres hands back aware datetimes; a hand-built row may not.

        Comparing a naive stored value against an aware ``now`` raises
        TypeError inside the sweep, which would take out every monitor in the
        batch rather than the one bad row.
        """
        monitor = _monitor(last_checkin_at=datetime(2026, 9, 9, 11, 0, 0))
        assert deadline_for(monitor) == datetime(2026, 9, 9, 11, 30, tzinfo=UTC)


class TestIsOverdue:
    def test_not_overdue_before_the_deadline(self):
        assert is_overdue(_monitor(last_checkin_at=NOW - timedelta(minutes=29)), NOW) is False

    def test_overdue_after_the_deadline(self):
        assert is_overdue(_monitor(last_checkin_at=NOW - timedelta(minutes=31)), NOW) is True

    def test_the_deadline_itself_is_not_yet_late(self):
        assert is_overdue(_monitor(last_checkin_at=NOW - timedelta(minutes=30)), NOW) is False


class TestShouldAlert:
    """Which overdue monitors get a notification on this pass."""

    def test_alerts_on_the_first_crossing(self):
        monitor = _monitor(last_checkin_at=NOW - timedelta(hours=1), state=MonitorState.OK)
        assert should_alert(monitor, NOW) is True

    def test_a_disabled_monitor_never_alerts(self):
        """Paused for planned downtime, not deleted."""
        monitor = _monitor(
            enabled=False, last_checkin_at=NOW - timedelta(hours=1), state=MonitorState.OK
        )
        assert should_alert(monitor, NOW) is False

    def test_does_not_re_alert_while_already_missing(self):
        """Without this the sweep pages every 60 seconds until someone acts."""
        monitor = _monitor(
            last_checkin_at=NOW - timedelta(hours=1),
            state=MonitorState.MISSING,
            last_alert_at=NOW - timedelta(minutes=5),
        )
        assert should_alert(monitor, NOW) is False

    def test_renotifies_once_the_configured_interval_has_elapsed(self):
        monitor = _monitor(
            last_checkin_at=NOW - timedelta(hours=6),
            state=MonitorState.MISSING,
            last_alert_at=NOW - timedelta(hours=4),
            renotify_seconds=3600,
        )
        assert should_alert(monitor, NOW) is True

    def test_holds_the_renotify_interval(self):
        monitor = _monitor(
            last_checkin_at=NOW - timedelta(hours=6),
            state=MonitorState.MISSING,
            last_alert_at=NOW - timedelta(minutes=10),
            renotify_seconds=3600,
        )
        assert should_alert(monitor, NOW) is False

    def test_missing_with_no_alert_accepted_is_still_owed_one(self):
        """The one rule co-status changes (spec § The sweep).

        ``last_alert_at`` is set only when notifier accepted the dispatch. A
        monitor marked ``missing`` while notifier was unreachable has had no
        alert, so every later pass tries again — the alert arrives late
        instead of never. Without a renotify interval the old rule dropped it.
        """
        monitor = _monitor(
            last_checkin_at=NOW - timedelta(hours=6),
            state=MonitorState.MISSING,
            last_alert_at=None,
            renotify_seconds=None,
        )
        assert should_alert(monitor, NOW) is True

    def test_renotify_with_no_prior_alert_fires(self):
        """A row that reached ``missing`` before an alert could be delivered."""
        monitor = _monitor(
            last_checkin_at=NOW - timedelta(hours=6),
            state=MonitorState.MISSING,
            last_alert_at=None,
            renotify_seconds=3600,
        )
        assert should_alert(monitor, NOW) is True


CHANNELS = ["01J00000000000000000000CH1"]


class TestReportDue:
    """Which ``alert`` check-ins send a report (#28, spec F2–F4).

    One per fault, a reminder per ``renotify_seconds`` landing on the
    check-in nearest it, and a resend while nobody has heard. Everything
    else is recorded and suppressed.
    """

    def _open(self, **overrides) -> Monitor:
        """A nightly backup with a fault open since a day ago."""
        fields = {
            "interval_seconds": 86400,
            "grace_seconds": 7200,
            "renotify_seconds": 86400,
            "channel_ids": CHANNELS,
            "state": MonitorState.OK,
            "fault_since": NOW - timedelta(days=1),
            "fault_key": None,
        }
        fields.update(overrides)
        return _monitor(**fields)

    def test_the_alert_that_opens_a_fault_reports(self):
        monitor = self._open(fault_since=None)
        assert opens_fault(monitor, None)
        assert report_due(monitor, NOW, None, None)

    def test_a_repeat_inside_the_renotify_period_is_suppressed(self):
        monitor = self._open(interval_seconds=300, renotify_seconds=3600)
        heard = (NOW - timedelta(minutes=5), "succeeded")
        assert not opens_fault(monitor, None)
        assert not report_due(monitor, NOW, None, heard)

    def test_a_nightly_failure_23h50m_after_the_last_reports(self):
        """The hazard: ``RandomizedDelaySec=10min`` puts two runs 23h50m
        apart, and plain ``>= renotify`` would drop the second night."""
        monitor = self._open()
        assert report_due(monitor, NOW, None, (NOW - timedelta(hours=23, minutes=50), "succeeded"))

    def test_the_reminder_lands_on_the_checkin_nearest_renotify(self):
        """Due from renotify minus half the interval: 12 h for the backups."""
        monitor = self._open()
        assert report_due(monitor, NOW, None, (NOW - timedelta(hours=12), "succeeded"))
        assert not report_due(
            monitor, NOW, None, (NOW - timedelta(hours=11, minutes=59), "succeeded")
        )

    def test_an_hourly_probe_is_reminded_on_the_nearest_tick_to_a_day(self):
        monitor = self._open(interval_seconds=3600, renotify_seconds=86400)
        assert not report_due(monitor, NOW, None, (NOW - timedelta(hours=23), "succeeded"))
        assert report_due(monitor, NOW, None, (NOW - timedelta(hours=23, minutes=30), "succeeded"))

    def test_a_cadence_longer_than_twice_renotify_reports_every_alert(self):
        monitor = self._open(interval_seconds=600, renotify_seconds=60)
        assert report_due(monitor, NOW, None, (NOW - timedelta(seconds=1), "succeeded"))

    def test_without_renotify_a_fault_reports_once(self):
        monitor = self._open(interval_seconds=600, renotify_seconds=None)
        assert not report_due(monitor, NOW, None, (NOW - timedelta(days=30), "succeeded"))

    @pytest.mark.parametrize("status", ["not_accepted", "failed"])
    def test_a_report_nobody_heard_is_resent(self, status):
        """Late, never lost: as for the owed missing alert (F4)."""
        monitor = self._open(renotify_seconds=None)
        assert report_due(monitor, NOW, None, (NOW - timedelta(minutes=1), status))

    def test_a_partial_report_was_heard(self):
        """The channels are redundant: one delivered is someone told."""
        monitor = self._open(renotify_seconds=None)
        assert not report_due(monitor, NOW, None, (NOW - timedelta(minutes=1), "partial"))

    def test_a_fault_with_no_report_yet_reports(self):
        """Channels added mid-fault: nothing was owed before, now it is."""
        assert report_due(self._open(), NOW, None, None)

    def test_a_monitor_with_no_channels_never_owes_a_report(self):
        monitor = self._open(fault_since=None, channel_ids=[])
        assert opens_fault(monitor, None)
        assert not report_due(monitor, NOW, None, None)

    @pytest.mark.parametrize(
        ("open_key", "fault"),
        [("lag", "off_main"), (None, "lag"), ("lag", None), ({"a": [1]}, {"a": [2]}), (1, True)],
        ids=["changed", "added", "dropped", "nested", "json-not-python"],
    )
    def test_another_fault_key_opens_a_new_fault(self, open_key, fault):
        """The consumer's opt-in (F2), compared as JSON: ``True`` is not ``1``."""
        monitor = self._open(fault_key=open_key, renotify_seconds=None)
        heard = (NOW - timedelta(minutes=1), "succeeded")
        assert opens_fault(monitor, fault)
        assert report_due(monitor, NOW, fault, heard)

    def test_the_same_fault_key_is_the_same_fault(self):
        """Key order is not identity: Postgres's jsonb reorders keys."""
        monitor = self._open(fault_key={"b": 1, "a": [1, 2]}, renotify_seconds=None)
        assert not opens_fault(monitor, {"a": [1, 2], "b": 1})


class TestRedeliveryDue:
    """Spaced from notifier's own attempt log, not the 60 s sweep (#10)."""

    def test_the_schedule_is_1_5_15_then_60_minutes(self):
        assert [d.total_seconds() / 60 for d in REDELIVERY_DELAYS] == [1, 5, 15, 60]

    @pytest.mark.parametrize(("attempt", "minutes"), [(1, 1), (2, 5), (3, 15), (4, 60)])
    def test_each_attempt_waits_its_delay(self, attempt, minutes):
        assert redelivery_due(attempt, NOW) == NOW + timedelta(minutes=minutes)

    def test_past_the_schedule_the_last_delay_repeats(self):
        """notifier's cap ends it with a 409, not a count kept here."""
        assert redelivery_due(9, NOW) == NOW + timedelta(minutes=60)

    def test_an_attempt_below_one_reads_as_the_first(self):
        assert redelivery_due(0, NOW) == NOW + timedelta(minutes=1)


class TestShouldRedeliver:
    def _due(self, **overrides) -> Monitor:
        fields = {
            "state": MonitorState.MISSING,
            "last_alert_at": NOW - timedelta(minutes=2),
            "last_alert_status": "failed",
            "last_alert_dispatch_id": "01J0000000000000000000DISP",
            "last_alert_redeliver_at": NOW - timedelta(minutes=1),
        }
        fields.update(overrides)
        return _monitor(**fields)

    def test_a_missing_monitor_past_its_redelivery_is_due(self):
        assert should_redeliver(self._due(), NOW)

    def test_the_due_time_itself_is_due(self):
        assert should_redeliver(self._due(last_alert_redeliver_at=NOW), NOW)

    def test_not_before_it(self):
        assert not should_redeliver(
            self._due(last_alert_redeliver_at=NOW + timedelta(seconds=1)), NOW
        )

    def test_none_scheduled_is_never_due(self):
        """Delivered, or capped by notifier."""
        assert not should_redeliver(self._due(last_alert_redeliver_at=None), NOW)

    def test_no_dispatch_is_never_due(self):
        assert not should_redeliver(self._due(last_alert_dispatch_id=None), NOW)

    @pytest.mark.parametrize("state", [MonitorState.OK, MonitorState.PENDING])
    def test_a_monitor_no_longer_missing_is_not(self, state):
        assert not should_redeliver(self._due(state=state), NOW)

    def test_a_disabled_monitor_is_not(self):
        assert not should_redeliver(self._due(enabled=False), NOW)

    def test_a_renotifying_monitor_is_included(self):
        """redeliver() needs no new key and skips channels that delivered: #7's
        reasons to leave these out do not apply, and a 24 h renotify is long."""
        assert should_redeliver(self._due(renotify_seconds=86400), NOW)

    def test_naive_timestamps_are_read_as_utc(self):
        due = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
        assert should_redeliver(self._due(last_alert_redeliver_at=due), NOW)


class TestFormatDuration:
    """Alert bodies quote elapsed time; seconds-since-epoch helps nobody."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s"),
            (45, "45s"),
            (60, "1m"),
            (615, "10m 15s"),
            (3600, "1h"),
            (5430, "1h 30m 30s"),
            (90000, "1d 1h"),
        ],
    )
    def test_renders_human_units(self, seconds, expected):
        assert format_duration(timedelta(seconds=seconds)) == expected

    def test_negative_durations_read_as_zero(self):
        assert format_duration(timedelta(seconds=-5)) == "0s"


PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")


def _placeholders(notice: Notice) -> set[str]:
    return set(PLACEHOLDER.findall(notice.title_template + notice.body_template))


def _cleared(monitor: Monitor, now: datetime) -> Notice:
    return cleared_notification(_with_fault(monitor), now)


def _with_fault(monitor: Monitor) -> Monitor:
    monitor.fault_since = NOW - timedelta(hours=3)
    return monitor


BUILT_IN = [missing_notification, recovery_notification, _cleared]


class TestBuiltInNotifications:
    """Missing and recovery wording is co-status's, not the consumer's.

    A consumer that has stopped reporting cannot supply a template for the
    fact that it stopped reporting, so these are built in. They go to
    notifier as a *fixed* template with the facts passed as ``variables``:
    notifier renders with Jinja, so a monitor name pasted into the template
    text would be read as template syntax — a name containing ``{{`` would
    become a 422 and a lost alert.
    """

    @pytest.mark.parametrize("build", BUILT_IN)
    def test_every_placeholder_has_a_variable(self, build):
        """notifier renders with StrictUndefined: one unbound name and the
        alert is a 422 instead of a notification."""
        notice = build(_monitor(last_checkin_at=NOW - timedelta(hours=1)), NOW)
        assert _placeholders(notice) <= set(notice.variables)

    @pytest.mark.parametrize("build", BUILT_IN)
    def test_the_monitor_name_is_data_not_template(self, build):
        name = "evil {{ x }} {% raw %}"
        notice = build(_monitor(name=name, last_checkin_at=NOW - timedelta(hours=1)), NOW)
        assert name not in notice.title_template + notice.body_template
        assert notice.variables["name"] == name

    @pytest.mark.parametrize("build", BUILT_IN)
    def test_the_title_says_who_sent_it(self, build):
        notice = build(_monitor(last_checkin_at=NOW - timedelta(hours=1)), NOW)
        assert notice.title_template.startswith("[co-status] ")

    def test_missing_names_the_monitor_and_the_silence(self):
        monitor = _monitor(name="co-broker", last_checkin_at=NOW - timedelta(minutes=47))
        variables = missing_notification(monitor, NOW).variables
        assert variables["name"] == "co-broker"
        assert variables["silence"] == "47m"
        assert "10m" in variables["cadence"]  # the expected cadence

    def test_missing_says_never_when_nothing_ever_arrived(self):
        monitor = _monitor(last_checkin_at=None, created_at=NOW - timedelta(hours=3))
        variables = missing_notification(monitor, NOW).variables
        assert "never" in variables["last_checkin"].lower()

    def test_missing_quotes_the_deadline_it_passed(self):
        monitor = _monitor(last_checkin_at=NOW - timedelta(minutes=47))
        variables = missing_notification(monitor, NOW).variables
        assert variables["deadline"] == "2026-09-09T11:43:00Z"

    def test_cleared_names_the_fault_and_how_long_it_lasted(self):
        """Built before ``last_checkin_at`` moves: the previous check-in was
        the fault's last ``alert``."""
        monitor = _monitor(
            name="co-observo-live",
            fault_since=NOW - timedelta(hours=1, minutes=5),
            last_checkin_at=NOW - timedelta(minutes=5),
        )
        notice = cleared_notification(monitor, NOW)
        assert notice.title_template == "[co-status] {{ name }} has cleared"
        assert notice.variables == {
            "name": "co-observo-live",
            "duration": "1h 5m",
            "since": "2026-09-09T10:55:00Z",
            "last_alert": "2026-09-09T11:55:00Z",
        }

    def test_recovery_names_the_silence_that_ended(self):
        monitor = _monitor(name="co-broker", last_checkin_at=NOW - timedelta(hours=2))
        variables = recovery_notification(monitor, NOW).variables
        assert variables["name"] == "co-broker"
        assert variables["silence"] == "2h"


class TestCheckinStatus:
    def test_values_are_the_wire_strings(self):
        assert CheckinStatus.OK == "ok"
        assert CheckinStatus.ALERT == "alert"

    def test_monitor_states_are_the_wire_strings(self):
        assert MonitorState.PENDING == "pending"
        assert MonitorState.OK == "ok"
        assert MonitorState.MISSING == "missing"

    def test_event_kinds_are_the_stored_strings(self):
        assert [k.value for k in EventKind] == [
            "imported",
            "first_checkin",
            "missing",
            "recovered",
            "alert",
            "paused",
            "resumed",
            "cleared",
        ]
