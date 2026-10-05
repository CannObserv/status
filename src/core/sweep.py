"""The pass that watches for silence: alert on every monitor gone quiet.

Run by a systemd timer (``deploy/status-sweep.timer``) rather than a task in
the API process, for the reason notifier#56 gives: an alerter that rides the
thing it watches stops reporting exactly when it is needed.

Ported from notifier's ``sweep_monitors``. What changed is delivery, which
goes through :mod:`src.core.alerting` to notifier, and with it the rule the
spec names (§ The sweep): ``last_alert_at`` moves only when notifier accepted
the alert, so a monitor that went missing while notifier was unreachable
stays owed an alert, and :func:`~src.core.monitors.should_alert` keeps
retrying it — under the same idempotency key — until notifier answers.

Accepted is not delivered (#6): notifier's delivery ``status`` is kept in
``last_alert_status``, and every pass reports each ``missing`` monitor whose
last alert did not succeed, until it recovers or a later alert gets through.

The check-in path's notices, recovery and report, have no pass of their own
(#8). The route keeps each one's status on its ``monitor_events`` row, and
every pass here reports the latest of each kind that did not succeed, until a
later one of that kind does or :data:`NOTICE_WINDOW` ends.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.alerting import (
    DELIVERED,
    Alerter,
    AlertNotAccepted,
    NotifierUnavailable,
    missing_key,
)
from src.core.logging import get_logger
from src.core.models import MonitorEvent
from src.core.models.monitor import Monitor
from src.core.monitors import EventKind, MonitorState, missing_notification, should_alert

logger = get_logger(__name__)

#: How long an undelivered recovery or report stays reported when no later one
#: of its kind is delivered first. A one-off report has no later one, and
#: nothing resends it (#7): without an end it would hold ``notifier-reachable``
#: down for good. A working day, so somebody sees it (#8).
NOTICE_WINDOW = timedelta(hours=24)

#: The check-in path's notices: the event kind each is recorded as, and its
#: name in :attr:`SweepReport.undelivered_notices`.
CHECKIN_NOTICES = {EventKind.RECOVERED: "recovery", EventKind.ALERT: "report"}


@dataclass
class SweepReport:
    """What one pass did — the payload of the timer's journald line."""

    checked: int = 0
    #: notifier accepted an alert for these.
    alerted: list[str] = field(default_factory=list)
    #: Overdue, and notifier took no alert: retried on the next pass.
    owed: list[str] = field(default_factory=list)
    #: Overdue with no channel configured at all: nowhere to send.
    undeliverable: list[str] = field(default_factory=list)
    #: Missing, and notifier accepted its last alert but did not deliver it:
    #: monitor id → ``failed`` or ``partial``. Every pass, not just the one that sent.
    undelivered: dict[str, str] = field(default_factory=dict)
    #: The latest recovery or report notifier accepted but did not deliver,
    #: within :data:`NOTICE_WINDOW`: monitor id → ``{"recovery" | "report": status}`` (#8).
    undelivered_notices: dict[str, dict[str, str]] = field(default_factory=dict)
    #: notifier's ``/health`` answered, in this environment, at the start of the pass.
    notifier_ok: bool = True


async def sweep_monitors(
    session: AsyncSession, alerter: Alerter, now: datetime | None = None
) -> SweepReport:
    """Alert on every enabled monitor that has missed its deadline.

    Does not commit — the caller owns the transaction, so
    ``scripts/sweep_monitors.py`` commits once for the whole pass.

    Checks notifier's environment first and raises
    :class:`~src.core.alerting.EndpointMismatch` before writing anything: a
    sweep pointed at the wrong notifier fails loudly, as a failed unit. An
    unreachable notifier is not that — the pass still marks what is missing,
    and owes the alerts.

    A monitor is marked ``missing`` whether or not the alert went out. The
    state describes the consumer, not our luck reaching notifier.
    """
    now = now or datetime.now(UTC)
    report = SweepReport()

    try:
        await alerter.check_endpoint()
    except NotifierUnavailable as exc:
        logger.error(f"notifier unreachable at sweep start; alerts will be owed: {exc}")
        report.notifier_ok = False

    result = await session.execute(select(Monitor).where(Monitor.enabled.is_(True)))
    monitors = result.scalars().all()
    for monitor in monitors:
        report.checked += 1
        if should_alert(monitor, now):
            await _alert(session, alerter, monitor, now, report)
        status = monitor.last_alert_status
        if monitor.state == MonitorState.MISSING and status and status != DELIVERED:
            report.undelivered[str(monitor.id)] = status
    report.undelivered_notices = await _undelivered_notices(
        session, [m.id for m in monitors], now - NOTICE_WINDOW
    )

    await session.flush()
    return report


async def _undelivered_notices(
    session: AsyncSession, monitor_ids: list[str], since: datetime
) -> dict[str, dict[str, str]]:
    """Each monitor's latest accepted notice of each kind after *since*, if undelivered.

    *monitor_ids* are the pass's enabled monitors: disabled ones are left
    out, as the missing alerts are. Naming them is also what lets Postgres
    read only the window from ``(monitor_id, at)``, not every event ever
    written (CR 1). Latest *accepted*: a later notice notifier never took
    delivered nothing either, so it does not hide an earlier failure.
    """
    if not monitor_ids:
        return {}
    latest = (
        select(MonitorEvent.monitor_id, MonitorEvent.kind, MonitorEvent.dispatch_status)
        .where(
            MonitorEvent.monitor_id.in_(monitor_ids),
            MonitorEvent.kind.in_(list(CHECKIN_NOTICES)),
            MonitorEvent.dispatch_status.is_not(None),
            MonitorEvent.at > since,
        )
        .ext(distinct_on(MonitorEvent.monitor_id, MonitorEvent.kind))
        .order_by(MonitorEvent.monitor_id, MonitorEvent.kind, MonitorEvent.at.desc())
    )
    found: dict[str, dict[str, str]] = {}
    for monitor_id, kind, status in (await session.execute(latest)).all():
        if status != DELIVERED:
            found.setdefault(str(monitor_id), {})[CHECKIN_NOTICES[kind]] = status
    return found


async def _alert(
    session: AsyncSession, alerter: Alerter, monitor: Monitor, now: datetime, report: SweepReport
) -> None:
    """Send, or owe, one overdue monitor's alert; mark it missing on first crossing."""
    first_crossing = monitor.state != MonitorState.MISSING
    if first_crossing:
        # A new outage: the last one's delivery says nothing about this one.
        monitor.last_alert_status = None
    dispatch_id = dispatch_status = None
    if not monitor.channel_ids:
        report.undeliverable.append(str(monitor.id))
        logger.warning(
            "monitor is overdue but has no channel to alert",
            extra={"monitor_id": str(monitor.id), "monitor_name": monitor.name},
        )
    elif not report.notifier_ok:
        report.owed.append(str(monitor.id))
    else:
        notice = missing_notification(monitor, now)
        try:
            delivery = await alerter.send(
                title_template=notice.title_template,
                body_template=notice.body_template,
                variables=notice.variables,
                channel_ids=list(monitor.channel_ids),
                idempotency_key=missing_key(monitor, now),
                metadata={"monitor_id": str(monitor.id), "reason": "missing"},
            )
        except AlertNotAccepted as exc:
            report.owed.append(str(monitor.id))
            logger.error(
                f"missing alert for {monitor.name} not accepted; owed: {exc}",
                extra={"monitor_id": str(monitor.id)},
            )
        else:
            dispatch_id, dispatch_status = delivery.dispatch_id, delivery.status
            monitor.last_alert_at = now
            monitor.last_alert_status = delivery.status
            report.alerted.append(str(monitor.id))

    if first_crossing:
        monitor.state = MonitorState.MISSING
        session.add(
            MonitorEvent(
                monitor_id=monitor.id,
                kind=EventKind.MISSING,
                at=now,
                dispatch_id=dispatch_id,
                dispatch_status=dispatch_status,
            )
        )
