"""The dead-man's timer: deadlines, the alert decision, and the wording (notifier#56).

Alert on the *absence* of reports, not only on their contents. A findings-only
push is silent in exactly the cases that matter most — a dead probe, a stopped
timer, a wedged process, a dead node all produce zero findings and zero
traffic, which is indistinguishable from a healthy consumer.
CannObserv/observo#473 is what that costs: a crash-looped Redis left a cluster
starved for two weeks because nothing was watching for silence.

Ported from notifier's ``src/core/monitors.py``. Pure: nothing here touches
the database or the network. The sweep that acts on it lives in
``src/core/sweep.py``, and every alert it decides on reaches notifier through
``src/core/alerting.py``.
"""

import enum
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.core.models.monitor import Monitor
from src.core.utils import format_utc_iso


class MonitorState(enum.StrEnum):
    """Where a monitor stands relative to its deadline."""

    #: Created, but no check-in has ever arrived.
    PENDING = "pending"
    #: Reporting on time.
    OK = "ok"
    #: Past its deadline. Someone has been told, or is owed an alert.
    MISSING = "missing"


class CheckinStatus(enum.StrEnum):
    """What a consumer says about itself when it checks in.

    Deliberately two values. Whether a report *warrants* a notification is the
    consumer's judgement — a broker maps ``finding_count > 0`` onto ``alert``
    — because the alternative is co-status learning a consumer's taxonomy.
    """

    OK = "ok"
    ALERT = "alert"


class EventKind(enum.StrEnum):
    """What ``monitor_events`` records (spec D9): state changes, never reports."""

    #: The import script created this monitor; history begins here.
    IMPORTED = "imported"
    #: ``pending`` → ``ok``.
    FIRST_CHECKIN = "first_checkin"
    #: The sweep marked the monitor ``missing``.
    MISSING = "missing"
    #: A check-in arrived while ``missing``.
    RECOVERED = "recovered"
    #: The consumer checked in with ``status: alert``.
    ALERT = "alert"
    #: ``enabled`` set to ``false`` — planned downtime, not an outage.
    PAUSED = "paused"
    #: ``enabled`` set back to ``true``.
    RESUMED = "resumed"
    #: An ``ok`` check-in ended an open fault (#28).
    CLEARED = "cleared"


@dataclass(frozen=True)
class Notice:
    """A built-in notification, in the shape notifier's ``/dispatch`` takes.

    A *fixed* template and the facts as ``variables``, never text with the
    facts pasted in. notifier renders with Jinja, so a monitor name spliced
    into the template would be parsed as template syntax: a name containing
    ``{{`` becomes a 422, and the alert is lost.
    """

    title_template: str
    body_template: str
    variables: dict[str, str] = field(default_factory=dict)


MISSING_TITLE = "[co-status] {{ name }} has stopped reporting"
MISSING_BODY = (
    "No check-in from **{{ name }}** for {{ silence }}.\n\n"
    "- Expected: {{ cadence }}\n"
    "- Last check-in: {{ last_checkin }}\n"
    "- Deadline passed: {{ deadline }}\n\n"
    "Silence is the alert: the reporter itself may be down, wedged, or never started."
)
RECOVERY_TITLE = "[co-status] {{ name }} has recovered"
RECOVERY_BODY = (
    "**{{ name }}** is reporting again after {{ silence }} of silence.\n\n"
    "- Expected: {{ cadence }}\n"
    "- Previous check-in: {{ last_checkin }}"
)
CLEARED_TITLE = "[co-status] {{ name }} has cleared"
CLEARED_BODY = (
    "**{{ name }}** checked in `ok` after {{ duration }} reporting `alert`.\n\n"
    "- Alerting since: {{ since }}\n"
    "- Last alert check-in: {{ last_alert }}"
)

#: Delivery statuses that mean nobody was told: notifier never took the
#: report (co-status's ``not_accepted``, #19), or every channel failed. A
#: ``partial`` reached someone, so it is heard (#28, F4). Spelled here, not
#: imported, because this module never imports ``alerting``.
UNHEARD = frozenset({"not_accepted", "failed"})


def _as_utc(value: datetime) -> datetime:
    """Read a naive timestamp as UTC rather than raising mid-sweep.

    Postgres hands back aware datetimes for these columns, but a row built in
    a test or a fixture may not be. Comparing naive against aware raises
    TypeError, which inside the sweep would take out every monitor in the
    batch rather than the one bad row.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def deadline_for(monitor: Monitor) -> datetime:
    """When this monitor's next check-in stops being late and starts being an
    outage.

    Anchored on ``created_at`` before the first check-in ever arrives — a
    probe that was never wired up is the most likely misconfiguration of all,
    and anchoring on ``last_checkin_at`` alone would make it the one case the
    timer stays silent about forever.
    """
    anchor = monitor.last_checkin_at or monitor.created_at
    return _as_utc(anchor) + timedelta(
        seconds=monitor.interval_seconds + (monitor.grace_seconds or 0)
    )


def is_overdue(monitor: Monitor, now: datetime) -> bool:
    """True once ``now`` is past the deadline. The deadline itself is not late."""
    return now > deadline_for(monitor)


def should_alert(monitor: Monitor, now: datetime) -> bool:
    """Whether this pass should notify about this monitor.

    Fires on the crossing into ``missing``. Without the state check the sweep
    would page every 60 seconds until someone acted; ``renotify_seconds`` is
    the opt-in for the opposite failure, where the single alert is the one
    nobody saw.

    **The one rule co-status changes:** a ``missing`` monitor with no
    ``last_alert_at`` is still owed an alert. ``last_alert_at`` is set only
    when notifier accepted the dispatch, so a monitor marked ``missing`` while
    notifier was unreachable is retried on every pass until it answers — the
    alert arrives late instead of never.
    """
    if not monitor.enabled or not is_overdue(monitor, now):
        return False
    if monitor.state != MonitorState.MISSING or monitor.last_alert_at is None:
        return True
    if monitor.renotify_seconds is None:
        return False
    return now - _as_utc(monitor.last_alert_at) >= timedelta(seconds=monitor.renotify_seconds)


def _canonical(value: object) -> str:
    """A JSON value as one string: ``True`` is not ``1``, and key order,
    which Postgres's ``jsonb`` rewrites, is not identity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def opens_fault(monitor: Monitor, fault: object) -> bool:
    """Whether an ``alert`` carrying ``metadata.fault`` *fault* opens a new fault.

    A run of ``alert`` check-ins is one fault (#28, F1). None is open, or the
    consumer's key differs from the one it opened with (F2), compared as
    JSON. Without the key, every ``alert`` in the run carries null alike, so
    a fault is told apart from the next by ``status`` and time alone.
    """
    if monitor.fault_since is None:
        return True
    return _canonical(fault) != _canonical(monitor.fault_key)


def report_due(
    monitor: Monitor,
    now: datetime,
    fault: object,
    last_report: tuple[datetime, str] | None,
) -> bool:
    """Whether this ``alert`` check-in sends the monitor's report (#28, F2–F4).

    *last_report* is the open fault's latest report that owed a notice, as
    ``(at, dispatch_status)``, or ``None``. Due when the check-in opens a
    fault, when nobody heard the last report (:data:`UNHEARD`: late, never
    lost), or as a reminder once ``renotify_seconds`` minus half the
    interval has passed: the check-in *nearest* the renotify point, because
    check-ins arrive with jitter. A nightly backup whose timer runs up to 10
    minutes early is 23h50m from its last failure, and must still report.
    Without ``renotify_seconds``, a fault reports once. A monitor with no
    channels owes nothing.
    """
    if not monitor.channel_ids:
        return False
    if opens_fault(monitor, fault) or last_report is None:
        return True
    at, status = last_report
    if status in UNHEARD:
        return True
    if monitor.renotify_seconds is None:
        return False
    tolerance = timedelta(seconds=monitor.interval_seconds / 2)
    return now - _as_utc(at) >= timedelta(seconds=monitor.renotify_seconds) - tolerance


def format_duration(delta: timedelta) -> str:
    """Render a duration in human units — ``1h 30m 30s``, ``10m``, ``1d 1h``.

    Alert bodies quote elapsed time, and an operator woken at 3am should not
    be dividing seconds by 3600. Trailing zero units are dropped; a negative
    duration (a clock that moved backwards) reads as ``0s`` rather than
    printing something impossible.
    """
    total = int(max(delta.total_seconds(), 0))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [
        f"{value}{unit}"
        for value, unit in ((days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s"))
        if value
    ]
    return " ".join(parts) if parts else "0s"


def _cadence(monitor: Monitor) -> str:
    expected = format_duration(timedelta(seconds=monitor.interval_seconds))
    grace = format_duration(timedelta(seconds=monitor.grace_seconds or 0))
    return f"every {expected} (grace {grace})"


def _last_seen(monitor: Monitor) -> str:
    if monitor.last_checkin_at is None:
        return "never — no check-in has ever arrived"
    return format_utc_iso(_as_utc(monitor.last_checkin_at))


def _silence(monitor: Monitor, now: datetime) -> str:
    return format_duration(now - _as_utc(monitor.last_checkin_at or monitor.created_at))


def missing_notification(monitor: Monitor, now: datetime) -> Notice:
    """The notice for a monitor that has gone quiet."""
    return Notice(
        title_template=MISSING_TITLE,
        body_template=MISSING_BODY,
        variables={
            "name": monitor.name,
            "silence": _silence(monitor, now),
            "cadence": _cadence(monitor),
            "last_checkin": _last_seen(monitor),
            "deadline": format_utc_iso(deadline_for(monitor)),
        },
    )


def recovery_notification(monitor: Monitor, now: datetime) -> Notice:
    """The notice for a monitor that is reporting again.

    Built before ``last_checkin_at`` moves, so it quotes the silence that just
    ended. Without it the outage alert stays the last word on a problem that
    is over, and the next real one is read as noise.
    """
    return Notice(
        title_template=RECOVERY_TITLE,
        body_template=RECOVERY_BODY,
        variables={
            "name": monitor.name,
            "silence": _silence(monitor, now),
            "cadence": _cadence(monitor),
            "last_checkin": _last_seen(monitor),
        },
    )


def cleared_notification(monitor: Monitor, now: datetime) -> Notice:
    """The notice for a fault that an ``ok`` check-in has ended (#28, F6).

    Built before ``last_checkin_at`` moves: while a fault is open, the
    previous check-in is always its last ``alert``. Without it, once repeats
    are suppressed, the end of a fault reads as a report that got lost.
    """
    since = _as_utc(monitor.fault_since)
    return Notice(
        title_template=CLEARED_TITLE,
        body_template=CLEARED_BODY,
        variables={
            "name": monitor.name,
            "duration": format_duration(now - since),
            "since": format_utc_iso(since),
            "last_alert": _last_seen(monitor),
        },
    )


#: How long after an attempt the sweep redelivers a missing alert notifier
#: accepted and did not deliver (#10), by attempt number: the original send
#: is attempt 1. The last delay repeats; notifier's per-channel cap ends the
#: chain with a 409, so nothing here counts attempts. Retrying every 60 s
#: pass would spend the cap in four minutes on a channel that is usually
#: down for longer.
REDELIVERY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=60),
)


def redelivery_due(attempt: int, started_at: datetime) -> datetime:
    """When to redeliver after *attempt*, which notifier started at *started_at*."""
    index = min(max(attempt, 1), len(REDELIVERY_DELAYS)) - 1
    return _as_utc(started_at) + REDELIVERY_DELAYS[index]


def should_redeliver(monitor: Monitor, now: datetime) -> bool:
    """Whether this pass should redeliver the monitor's last missing alert.

    ``last_alert_redeliver_at`` is the switch: the sweep clears it once the
    alert is delivered or notifier caps it. A monitor that renotifies is
    included — ``redeliver()`` needs no new key and does not repeat channels
    that delivered, which were #7's reasons to leave it out.
    """
    if not monitor.enabled or monitor.state != MonitorState.MISSING:
        return False
    if not monitor.last_alert_dispatch_id or monitor.last_alert_redeliver_at is None:
        return False
    return now >= _as_utc(monitor.last_alert_redeliver_at)
