"""Monitor CRUD and the check-in endpoint — the dead-man's timer's front door.

A consumer POSTs a check-in **every tick, regardless of findings**. The
arrival resets the timer; the ``status`` field says whether the contents also
warrant a notification. What makes a report worth alerting on is the
consumer's judgement — a broker maps its own ``finding_count > 0`` onto
``alert`` — because the alternative is co-status learning a consumer's
taxonomy.

Ported from notifier's ``src/api/routes/monitors.py`` (notifier#56). What
changed is where alerts go: through :mod:`src.core.alerting` to notifier, never
rendered or delivered here, and never at the cost of the check-in itself (spec
§ The check-in). The absence half runs from a systemd timer, not this process.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_alerter, get_db_session, require_api_key
from src.api.schemas.dispatch import DispatchOut
from src.api.schemas.monitor import (
    CheckinRequest,
    CheckinResponse,
    MonitorCreate,
    MonitorOut,
    MonitorUpdate,
)
from src.api.schemas.types import ULIDStr
from src.core.alerting import (
    NOT_ACCEPTED,
    Alerter,
    AlertNotAccepted,
    Budget,
    EndpointMismatch,
    NotifierUnavailable,
    TemplateRejected,
    within_budget,
)
from src.core.alerting import recovery_key as build_recovery_key
from src.core.logging import get_logger
from src.core.models import MonitorEvent
from src.core.models.monitor import Monitor
from src.core.monitors import (
    CheckinStatus,
    EventKind,
    MonitorState,
    deadline_for,
    recovery_notification,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/monitors", tags=["monitors"])

AlerterDep = Annotated[Alerter | None, Depends(get_alerter)]


def _to_out(m: Monitor) -> MonitorOut:
    return MonitorOut(
        id=str(m.id),
        tenant_id=str(m.tenant_id),
        name=m.name,
        enabled=m.enabled,
        interval_seconds=m.interval_seconds,
        grace_seconds=m.grace_seconds,
        renotify_seconds=m.renotify_seconds,
        channel_ids=list(m.channel_ids),
        title_template=m.title_template,
        body_template=m.body_template,
        state=m.state,
        last_checkin_at=m.last_checkin_at,
        last_status=m.last_status,
        last_variables=m.last_variables,
        last_alert_at=m.last_alert_at,
        last_alert_status=m.last_alert_status,
        next_deadline_at=deadline_for(m),
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


async def _load_owned(session: AsyncSession, monitor_id: str, tenant_id: str) -> Monitor:
    result = await session.execute(
        select(Monitor).where(Monitor.id == monitor_id, Monitor.tenant_id == tenant_id)
    )
    monitor = result.scalar_one_or_none()
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return monitor


async def _check_channels(alerter: Alerter | None, channel_ids: list[str]) -> None:
    """422 unless notifier recognises every id; 503 if it cannot be asked.

    The ids name channels owned by co-status's own notifier tenant, so no
    foreign key can hold them: this call is the only check, made on every
    write (spec § Data model).
    """
    if not channel_ids:
        return
    if alerter is None:
        raise HTTPException(status_code=503, detail="co-status has no notifier key configured")
    try:
        known = await within_budget(alerter.known_channel_ids())
    except AlertNotAccepted as exc:
        raise HTTPException(
            status_code=503, detail=f"cannot verify channels with notifier: {exc}"
        ) from exc
    unknown = [cid for cid in channel_ids if cid not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={"message": "notifier has no such channel", "channel_ids": unknown},
        )


def _event(monitor: Monitor, kind: EventKind, at: datetime):
    """A ``monitor_events`` row that sent nothing."""
    return MonitorEvent(monitor_id=monitor.id, kind=kind, at=at)


def _notice_event(monitor: Monitor, kind: EventKind, at: datetime, sent: DispatchOut | None):
    """The event for a notice the check-in owed: what notifier did with it.

    With *sent*, its dispatch id and delivery status (#8). Without, it is
    ``not_accepted`` (#19), unless the monitor has no channels: then nothing
    was owed, as for the sweep's ``undeliverable``.
    """
    status = sent.status if sent else (NOT_ACCEPTED if monitor.channel_ids else None)
    return MonitorEvent(
        monitor_id=monitor.id,
        kind=kind,
        at=at,
        dispatch_id=sent.id if sent else None,
        dispatch_status=status,
    )


@router.get("")
async def list_monitors(
    tenant_id: Annotated[str, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[MonitorOut]:
    """List all monitors owned by the calling tenant."""
    result = await session.execute(
        select(Monitor).where(Monitor.tenant_id == tenant_id).order_by(Monitor.created_at)
    )
    return [_to_out(m) for m in result.scalars().all()]


@router.post("", status_code=201)
async def create_monitor(
    body: MonitorCreate,
    tenant_id: Annotated[str, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    alerter: AlerterDep,
) -> MonitorOut:
    """Create a monitor. It starts ``pending`` and its clock starts now.

    The deadline is anchored on creation until the first check-in arrives, so
    a probe that is configured here but never wired up on the consumer's side
    alerts rather than sitting silent.
    """
    await _check_channels(alerter, body.channel_ids)
    monitor = Monitor(tenant_id=tenant_id, **body.model_dump())
    session.add(monitor)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"A monitor named '{body.name}' already exists."
        ) from exc
    await session.refresh(monitor)
    return _to_out(monitor)


@router.get("/{monitor_id}")
async def get_monitor(
    monitor_id: ULIDStr,
    tenant_id: Annotated[str, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MonitorOut:
    """Fetch a single monitor, including its last report and next deadline."""
    return _to_out(await _load_owned(session, monitor_id, tenant_id))


@router.patch("/{monitor_id}")
async def update_monitor(
    monitor_id: ULIDStr,
    body: MonitorUpdate,
    tenant_id: Annotated[str, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    alerter: AlerterDep,
) -> MonitorOut:
    """Partially update a monitor. ``enabled: false`` pauses the timer.

    Pausing and resuming are recorded as events: planned downtime must not
    read as an outage in any uptime figure built later.
    """
    monitor = await _load_owned(session, monitor_id, tenant_id)
    payload = body.model_dump(exclude_unset=True)
    if payload.get("channel_ids"):
        await _check_channels(alerter, payload["channel_ids"])
    if "enabled" in payload and payload["enabled"] is not None:
        if payload["enabled"] != monitor.enabled:
            kind = EventKind.RESUMED if payload["enabled"] else EventKind.PAUSED
            session.add(_event(monitor, kind, datetime.now(UTC)))
    for field, value in payload.items():
        setattr(monitor, field, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="That monitor name is already taken.") from exc
    await session.refresh(monitor)
    return _to_out(monitor)


@router.delete("/{monitor_id}", status_code=204)
async def delete_monitor(
    monitor_id: ULIDStr,
    tenant_id: Annotated[str, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Delete a monitor and its event history."""
    monitor = await _load_owned(session, monitor_id, tenant_id)
    await session.delete(monitor)
    await session.commit()


async def _send(
    alerter: Alerter,
    budget: Budget,
    monitor: Monitor,
    reason: str,
    **dispatch: Any,
) -> DispatchOut | None:
    """One dispatch within the request budget; ``None`` if notifier took none."""
    try:
        delivery = await budget.run(
            alerter.send(
                channel_ids=list(monitor.channel_ids),
                metadata={
                    **dispatch.pop("metadata", {}),
                    "monitor_id": str(monitor.id),
                    "reason": reason,
                },
                **dispatch,
            )
        )
    except AlertNotAccepted as exc:
        logger.error(
            f"{reason} for monitor {monitor.name} was not accepted by notifier: {exc}",
            extra={"monitor_id": str(monitor.id), "reason": reason},
        )
        return None
    return DispatchOut.from_sdk(delivery.dispatch)


@router.post("/{monitor_id}/checkin", status_code=202)
async def checkin(
    monitor_id: ULIDStr,
    body: CheckinRequest,
    tenant_id: Annotated[str, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    alerter: AlerterDep,
) -> CheckinResponse:
    """Record a check-in, and dispatch anything it warrants.

    Send one every tick whether or not there is anything to report. Zero
    findings and zero traffic is what a dead probe looks like too, so the
    report's *arrival* is the part co-status cannot infer. The arrival is
    always recorded: nothing notifier does or fails to do can cost it.
    """
    monitor = await _load_owned(session, monitor_id, tenant_id)
    now = datetime.now(UTC)
    previous_state = monitor.state
    is_alert = body.status == CheckinStatus.ALERT
    recovering = previous_state == MonitorState.MISSING
    dispatches: list[DispatchOut] = []

    # Taken before last_checkin_at moves: both quote the silence that is ending.
    recovery = recovery_notification(monitor, now) if recovering else None
    recovery_idempotency_key = build_recovery_key(monitor) if recovering else None

    can_send = alerter is not None and (is_alert or recovering)
    budget = Budget() if can_send else None
    if alerter is None and (is_alert or recovering):
        logger.error(
            "check-in warrants an alert but this process has no notifier key",
            extra={"monitor_id": str(monitor.id)},
        )

    if can_send:
        try:
            await budget.run(alerter.check_endpoint())
        except (EndpointMismatch, AlertNotAccepted) as exc:
            logger.error(f"not sending for monitor {monitor.name}: {exc}")
            can_send = False

    # Render before recording anything: a report notifier cannot render is the
    # consumer's bug, and the 422 must leave the monitor exactly as it was.
    report_sendable = can_send and is_alert
    if report_sendable:
        try:
            await budget.run(
                alerter.check_template(
                    monitor.title_template or "", monitor.body_template or "", body.variables
                )
            )
        except TemplateRejected as exc:
            raise HTTPException(
                status_code=422, detail={"section": exc.section, "message": exc.message}
            ) from exc
        except (AlertNotAccepted, NotifierUnavailable) as exc:
            logger.error(f"cannot check the report template for {monitor.name}: {exc}")
            report_sendable = False

    if recovering:
        sent = None
        if can_send and recovery is not None:
            sent = await _send(
                alerter,
                budget,
                monitor,
                "recovered",
                title_template=recovery.title_template,
                body_template=recovery.body_template,
                variables=recovery.variables,
                idempotency_key=recovery_idempotency_key,
            )
        if sent is not None:
            dispatches.append(sent)
        session.add(_notice_event(monitor, EventKind.RECOVERED, now, sent))

    if previous_state == MonitorState.PENDING:
        session.add(_event(monitor, EventKind.FIRST_CHECKIN, now))
    monitor.last_checkin_at = now
    monitor.last_status = body.status
    monitor.last_variables = body.variables
    monitor.state = MonitorState.OK

    if is_alert:
        sent = None
        if report_sendable:
            sent = await _send(
                alerter,
                budget,
                monitor,
                "report",
                title_template=monitor.title_template,
                body_template=monitor.body_template,
                variables=body.variables,
                metadata=body.metadata,
            )
        if sent is not None:
            dispatches.append(sent)
        session.add(_notice_event(monitor, EventKind.ALERT, now, sent))

    await session.commit()
    await session.refresh(monitor)
    return CheckinResponse(
        monitor_id=str(monitor.id),
        previous_state=previous_state,
        state=monitor.state,
        last_checkin_at=monitor.last_checkin_at,
        next_deadline_at=deadline_for(monitor),
        dispatches=dispatches,
    )
