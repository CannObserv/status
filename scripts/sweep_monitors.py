"""Run one dead-man's-timer pass: alert on every monitor that has gone quiet.

Entry point for ``deploy/status-sweep.service``, which a systemd timer starts
every minute. Launched through ``scripts/sweep.sh``, never directly — that
script is where the database and the notifier key are checked.

Deliberately *not* a task inside the API process. An alerter that rides the
thing it watches stops reporting exactly when it is needed (notifier#56).
This process needs Postgres, notifier and no part of the API to be up.

Each production pass then reports itself to healthchecks.io (#1,
:mod:`src.core.heartbeat`): the watcher of the watcher, outside the cohort.
Last, it asks the production API's ``/ready`` and reports that too (#13). A
pass whose code is ahead of the database's migrations fails as ``SchemaBehind``
(#9, :mod:`src.core.schema_state`).
"""

import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from src.core import build
from src.core.alerting import Alerter, alerter_from_environment
from src.core.database import get_session_factory
from src.core.heartbeat import Heartbeat, heartbeat_from_environment
from src.core.logging import configure_logging, get_logger
from src.core.schema_state import require_current
from src.core.sweep import SweepReport, sweep_monitors

logger = get_logger(__name__)


class NoNotifierKey(RuntimeError):
    """The sweep has no notifier key, so it cannot alert: a failed pass."""


async def run_sweep(
    session: AsyncSession, alerter: Alerter, heartbeat: Heartbeat | None = None
) -> SweepReport:
    """Sweep, commit, log the outcome, and report it to *heartbeat*.

    The log line is emitted on every pass, including the quiet ones. An
    operator needs to be able to tell "nothing is overdue" from "the timer has
    not fired in a week", and only a line that appears either way does that.
    The heartbeat is the same line for someone who is not on this host, sent
    only once the pass is committed.
    """
    try:
        # Before anything is marked or sent: code that does not match the
        # schema fails here, by name, rather than on its first column (#9).
        await require_current(session)
        report = await sweep_monitors(session, alerter)
        await session.commit()
    except Exception as exc:
        if heartbeat is not None:
            await heartbeat.sweep_failed(exc)
        raise
    logger.info(
        "monitor sweep complete",
        extra={
            "build": build.build_id(),
            "checked": report.checked,
            "alerted": report.alerted,
            "owed": report.owed,
            "undeliverable": report.undeliverable,
            "undelivered": report.undelivered,
        },
    )
    if heartbeat is not None:
        await heartbeat.sweep_completed(report)
    return report


async def main() -> int:
    """Run one pass, then report the API (#13). Non-zero without a notifier key."""
    heartbeat = heartbeat_from_environment()
    try:
        return await _one_pass(heartbeat)
    finally:
        # Last, whatever the pass did: a slow API must not delay an alert, and a
        # pass that raised says nothing about whether check-ins are recorded.
        if heartbeat is not None:
            await heartbeat.api_checked()


async def _one_pass(heartbeat: Heartbeat | None) -> int:
    """Open a session, run one pass, close it. Non-zero without a notifier key."""
    alerter = alerter_from_environment()
    if alerter is None:
        if heartbeat is not None:
            await heartbeat.sweep_failed(NoNotifierKey())
        return 1
    factory = get_session_factory()
    async with factory() as session:
        await run_sweep(session, alerter, heartbeat)
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(asyncio.run(main()))
