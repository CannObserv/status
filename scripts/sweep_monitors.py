"""Run one dead-man's-timer pass: alert on every monitor that has gone quiet.

Entry point for ``deploy/status-sweep.service``, which a systemd timer starts
every minute. Launched through ``scripts/sweep.sh``, never directly — that
script is where the database and the notifier key are checked.

Deliberately *not* a task inside the API process. An alerter that rides the
thing it watches stops reporting exactly when it is needed (notifier#56).
This process needs Postgres, notifier and no part of the API to be up.

Each production pass then reports itself to healthchecks.io (#1,
:mod:`src.core.heartbeat`): the watcher of the watcher, outside the cohort.
"""

import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.alerting import Alerter, alerter_from_environment
from src.core.database import get_session_factory
from src.core.heartbeat import Heartbeat, heartbeat_from_environment
from src.core.logging import configure_logging, get_logger
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
        report = await sweep_monitors(session, alerter)
        await session.commit()
    except Exception as exc:
        if heartbeat is not None:
            await heartbeat.sweep_failed(exc)
        raise
    logger.info(
        "monitor sweep complete",
        extra={
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
    """Open a session, run one pass, close it. Non-zero without a notifier key."""
    heartbeat = heartbeat_from_environment()
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
