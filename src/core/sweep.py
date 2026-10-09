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

Nor is it given up on (#10): the pass redelivers that dispatch through
notifier, which retries only the channels that failed, under the same
dispatch. Spaced by :data:`~src.core.monitors.REDELIVERY_DELAYS` from
notifier's own attempts, until it is delivered — which takes the monitor out
of ``undelivered`` in that pass — or notifier caps it with a 409. Only here,
after every alert of the pass is sent, never on the check-in path: one
redelivery can take about 8 s per failed channel.

The check-in path's notices, recovery, report and cleared (#28), have no
pass of their own (#8). The route keeps each one's status on its
``monitor_events`` row, ``not_accepted`` when notifier never took it (#19),
and every pass here reports the latest of each kind that did not succeed,
until a later one of that kind does or :data:`NOTICE_WINDOW` ends. The sweep
resends none of them; a report nobody heard is resent by the fault's next
``alert`` check-in (#28).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.alerting import (
    DELIVERED,
    Alerter,
    AlertNotAccepted,
    AlertRejected,
    Delivery,
    NotifierUnavailable,
    RedeliveryCapped,
    missing_key,
)
from src.core.logging import get_logger
from src.core.models import MonitorEvent
from src.core.models.monitor import Monitor
from src.core.monitors import (
    EventKind,
    MonitorState,
    _as_utc,
    missing_notification,
    redelivery_due,
    should_alert,
    should_redeliver,
)

logger = get_logger(__name__)

#: How long an undelivered recovery, report or cleared notice stays reported when no later one
#: of its kind is delivered first. A one-off report has no later one, and
#: nothing resends it (#7): without an end it would hold ``notifier-reachable``
#: down for good. A working day, so somebody sees it (#8).
NOTICE_WINDOW = timedelta(hours=24)

#: No redelivery starts later than this into the pass — counted from its
#: start, so slow sends spend it too; the rest stay due for the next pass.
#: One can take ~8 s per failed channel, and the unit has
#: ``TimeoutStartSec=120`` for the pass and the heartbeat after it (#10,
#: ``tests/deploy/test_sweep_units.py``). One already sent is never
#: cancelled: it could land at notifier and spend an attempt unrecorded.
REDELIVERY_WINDOW = timedelta(seconds=45)

#: :attr:`SweepReport.redelivered`'s value for a dispatch notifier capped.
CAPPED = "capped"

#: The check-in path's notices: the event kind each is recorded as, and its
#: name in :attr:`SweepReport.undelivered_notices`.
CHECKIN_NOTICES = {
    EventKind.RECOVERED: "recovery",
    EventKind.ALERT: "report",
    EventKind.CLEARED: "cleared",
}


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
    #: Redelivered this pass (#10): monitor id → the status notifier returned,
    #: or ``capped`` when it refused because every failed channel is out of attempts.
    redelivered: dict[str, str] = field(default_factory=dict)
    #: Missing, and notifier accepted its last alert but did not deliver it:
    #: monitor id → ``failed`` or ``partial``. Every pass, not just the one that sent.
    undelivered: dict[str, str] = field(default_factory=dict)
    #: The latest recovery, report or cleared notice (#28) notifier did not
    #: deliver, or never took (``not_accepted``, #19), within
    #: :data:`NOTICE_WINDOW`: monitor id →
    #: ``{"recovery" | "report" | "cleared": status}`` (#8).
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

    Then it redelivers each missing alert notifier did not deliver, once due
    (#10). It also reports, without sending anything, what did not reach anyone:
    each missing monitor's last alert, if notifier accepted it and did not
    deliver it (``undelivered``, #6), and each monitor's latest recovery and
    report within :data:`NOTICE_WINDOW`, undelivered or never accepted, read
    from ``monitor_events`` (``undelivered_notices``, #8, #19).
    """
    now = now or datetime.now(UTC)
    started = monotonic()
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
    if report.notifier_ok:
        await _redeliver_due(alerter, monitors, now, report, started)
    for monitor in monitors:
        status = monitor.last_alert_status
        if monitor.state == MonitorState.MISSING and status and status != DELIVERED:
            report.undelivered[str(monitor.id)] = status
    report.undelivered_notices = await _undelivered_notices(
        session, [m.id for m in monitors], now - NOTICE_WINDOW
    )

    await session.flush()
    return report


def _record_delivery(monitor: Monitor, delivery: Delivery, now: datetime) -> None:
    """Keep the dispatch and its status, and when to redeliver it, if ever."""
    monitor.last_alert_status = delivery.status
    monitor.last_alert_dispatch_id = delivery.dispatch_id
    if delivery.status == DELIVERED:
        monitor.last_alert_redeliver_at = None
    else:
        attempt, started_at = delivery.latest_attempt or (1, now)
        monitor.last_alert_redeliver_at = redelivery_due(attempt, started_at)


async def _redeliver_due(
    alerter: Alerter,
    monitors: Sequence[Monitor],
    now: datetime,
    report: SweepReport,
    started: float,
) -> None:
    """Redeliver every due missing alert, oldest due first, until the window
    closes; *started* is the pass's own ``monotonic()`` start."""
    due = sorted(
        (m for m in monitors if should_redeliver(m, now)),
        key=lambda m: _as_utc(m.last_alert_redeliver_at),
    )
    for n, monitor in enumerate(due):
        if monotonic() - started >= REDELIVERY_WINDOW.total_seconds():
            logger.warning(f"{len(due) - n} redelivery(ies) deferred to the next pass")
            return
        await _redeliver(alerter, monitor, now, report)


async def _redeliver(
    alerter: Alerter, monitor: Monitor, now: datetime, report: SweepReport
) -> None:
    """Redeliver one monitor's last missing alert, and record what came of it."""
    monitor_id = str(monitor.id)
    dispatch_id = monitor.last_alert_dispatch_id
    extra = {"monitor_id": monitor_id, "dispatch_id": dispatch_id}
    try:
        delivery = await alerter.redeliver(dispatch_id)
    except RedeliveryCapped as exc:
        # Terminal for this dispatch; it stays undelivered, so a person is paged (#6).
        monitor.last_alert_redeliver_at = None
        report.redelivered[monitor_id] = CAPPED
        logger.error(
            f"missing alert for {monitor.name} capped by notifier; no more redeliveries "
            f"(channels {', '.join(exc.channel_ids) or 'unnamed'})",
            extra=extra,
        )
    except AlertNotAccepted as exc:
        if isinstance(exc, AlertRejected) and exc.status_code == 404:
            # notifier no longer has it: asking again cannot bring it back.
            monitor.last_alert_redeliver_at = None
        logger.error(f"redelivery of missing alert for {monitor.name} failed: {exc}", extra=extra)
    else:
        _record_delivery(monitor, delivery, now)
        report.redelivered[monitor_id] = delivery.status


async def _undelivered_notices(
    session: AsyncSession, monitor_ids: list[str], since: datetime
) -> dict[str, dict[str, str]]:
    """Each monitor's latest owed notice of each kind after *since*, if undelivered.

    *monitor_ids* are the pass's enabled monitors: disabled ones are left
    out, as the missing alerts are. Naming them is also what lets Postgres
    read only the window from ``(monitor_id, at)``, not every event ever
    written (CR 1). Latest *with a status*: one notifier never took is
    ``not_accepted`` and reported in an earlier failure's place (#19); a null
    owed nothing, or predates #19, so it does not hide one.
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
        monitor.last_alert_dispatch_id = None
        monitor.last_alert_redeliver_at = None
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
            _record_delivery(monitor, delivery, now)
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
