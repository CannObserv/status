"""Import one monitor from notifier (spec § Cutover, step 2).

The row comes from notifier's database, exported read-only with the query in
``docs/RUNBOOK.md``. What the deadline depends on — ``created_at``,
``last_checkin_at``, the cadence, the state — arrives unchanged, so co-status's
copy is judged against exactly the clock notifier's was, and the monitor keeps
its id so a consumer's switch is a base URL and a key (spec D6).

The copy arrives **disabled**: notifier keeps watching until the handover, and
enabling co-status's copy is step 5. The event history begins here, with
``imported`` and ``paused``.

Rehearses by default and refuses rather than guesses, like the rest of the
operator scripts: a rehearsal that passes where the real run would fail
certifies the wrong thing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import Monitor, MonitorEvent, Tenant
from src.core.monitors import EventKind
from src.core.utils import format_utc_iso

#: Renamed on the way in to match the cohort's ``co-`` spelling.
TENANT_RENAMES = {"watcher": "co-watcher"}
MONITOR_RENAMES = {"watcher-backup": "co-watcher-backup"}

#: Every field the export query prints, and the only ones read.
FIELDS = (
    "id",
    "tenant_name",
    "name",
    "interval_seconds",
    "grace_seconds",
    "renotify_seconds",
    "channel_ids",
    "template_id",
    "title_template",
    "body_template",
    "state",
    "last_checkin_at",
    "last_status",
    "last_variables",
    "last_alert_at",
    "created_at",
)


class ImportRefused(ValueError):
    """The row cannot be imported as it stands; nothing was written."""


@dataclass(frozen=True)
class ImportedMonitor:
    """What was (or, in a rehearsal, would have been) created."""

    id: str
    tenant_name: str
    name: str
    channel_ids: list[str]
    state: str
    last_checkin_at: str | None


def target_tenant_name(row: dict[str, Any]) -> str:
    """The co-status tenant a notifier row lands on, after renaming."""
    name = str(row.get("tenant_name", ""))
    return TENANT_RENAMES.get(name, name)


def _when(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def import_monitor(
    session: AsyncSession,
    row: dict[str, Any],
    channel_map: dict[str, str],
    *,
    dry_run: bool = True,
) -> ImportedMonitor:
    """Create co-status's copy of the monitor in *row*; return what it is.

    Refuses a row missing any exported field, one with a ``template_id``, an
    id that already exists here, a tenant co-status does not have, and any
    channel id *channel_map* does not cover.
    """
    missing = [f for f in FIELDS if f not in row]
    if missing:
        raise ImportRefused(f"export row is missing {', '.join(missing)}")
    if row["template_id"] is not None:
        raise ImportRefused(
            f"monitor {row['id']} uses template_id {row['template_id']}; co-status stores no "
            "templates (spec D5) — move it to inline templates in notifier first"
        )
    unmapped = [cid for cid in row["channel_ids"] if cid not in channel_map]
    if unmapped:
        raise ImportRefused(f"no --channel mapping for {', '.join(unmapped)}")

    tenant_name = target_tenant_name(row)
    tenant = (
        await session.execute(select(Tenant).where(Tenant.name == tenant_name))
    ).scalar_one_or_none()
    if tenant is None:
        raise ImportRefused(f"co-status has no tenant {tenant_name!r}; seed it first")
    if await session.get(Monitor, row["id"]) is not None:
        raise ImportRefused(f"monitor {row['id']} already exists here")

    name = MONITOR_RENAMES.get(row["name"], row["name"])
    channel_ids = [channel_map[cid] for cid in row["channel_ids"]]
    now = datetime.now(UTC)
    try:
        monitor = Monitor(
            id=row["id"],
            tenant_id=tenant.id,
            name=name,
            enabled=False,
            interval_seconds=row["interval_seconds"],
            grace_seconds=row["grace_seconds"],
            renotify_seconds=row["renotify_seconds"],
            channel_ids=channel_ids,
            title_template=row["title_template"],
            body_template=row["body_template"],
            state=row["state"],
            last_checkin_at=_when(row["last_checkin_at"]),
            last_status=row["last_status"],
            last_variables=row["last_variables"],
            last_alert_at=_when(row["last_alert_at"]),
            created_at=_when(row["created_at"]),
        )
        session.add(monitor)
        await session.flush()
        session.add(MonitorEvent(monitor_id=monitor.id, kind=EventKind.IMPORTED, at=now))
        session.add(MonitorEvent(monitor_id=monitor.id, kind=EventKind.PAUSED, at=now))
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ImportRefused(f"the row breaks a constraint here: {exc.orig}") from exc

    imported = ImportedMonitor(
        id=str(monitor.id),
        tenant_name=tenant_name,
        name=name,
        channel_ids=channel_ids,
        state=monitor.state,
        last_checkin_at=format_utc_iso(monitor.last_checkin_at)
        if monitor.last_checkin_at
        else None,
    )
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return imported
