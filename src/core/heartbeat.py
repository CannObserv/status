"""The pings that watch the sweep: healthchecks.io, outside the cohort (#1).

The sweep is the only thing watching for consumer silence, so it needs a
watcher that does not share its failure modes: not this host, not Postgres,
not notifier. Each completed production pass pings two healthchecks.io
checks, which alert over their own email and Slack channels when pings stop
or fail:

- ``co-status-sweep`` — the pass completed and committed (``/fail`` if it raised);
- ``notifier-reachable`` — notifier answered ``/health`` in this environment,
  accepted every alert the pass sent (nothing owed), **and** delivered the
  last alert of every missing monitor (nothing undelivered, #6).

Best effort by construction: a ping never raises and never delays a pass
beyond its timeout. The ping key is a credential (D13): anyone holding it can
report a dead sweep as alive, so it is never logged and never an env var.
"""

import asyncio
import json
import logging
import os
from collections import Counter
from collections.abc import Mapping

import httpx

from src.core.credentials import read_credential
from src.core.db_safety import database_name, environment_label
from src.core.logging import get_logger
from src.core.sweep import SweepReport

logger = get_logger(__name__)

#: The ``LoadCredential=`` name ``status-sweep.service`` uses.
CREDENTIAL_NAME = "hc-ping-key"
PING_BASE_URL = "https://hc-ping.com"
SWEEP_CHECK = "co-status-sweep"
NOTIFIER_CHECK = "notifier-reachable"

#: Two pings per pass, inside ``TimeoutStartSec=120``. No retry: the next pass
#: is 60 seconds away and each check's grace period absorbs a missed one.
PING_TIMEOUT_SECONDS = 5.0


class _RedactKey(logging.Filter):
    """Masks the ping key in httpx's ``HTTP Request: POST <url>`` INFO lines."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.key in message:
            record.msg, record.args = message.replace(self.key, "***"), ()
        return True


def _redact_in_httpx_log(key: str) -> None:
    """Install :class:`_RedactKey` on the ``httpx`` logger, once per key."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactKey) and f.key == key for f in httpx_logger.filters):
        httpx_logger.addFilter(_RedactKey(key))


class Heartbeat:
    """Pings healthchecks.io for one production sweep pass."""

    def __init__(self, ping_key: str, *, base_url: str = PING_BASE_URL) -> None:
        self._key = ping_key
        self._base_url = base_url
        # httpx logs every request URL at INFO, and this URL carries the key.
        _redact_in_httpx_log(ping_key)

    async def sweep_completed(self, report: SweepReport) -> None:
        """Signal a completed pass, and whether notifier took its alerts."""
        counts = {
            "checked": report.checked,
            "alerted": len(report.alerted),
            "owed": len(report.owed),
            "undeliverable": len(report.undeliverable),
            "undelivered": len(report.undelivered),
        }
        await self._ping(SWEEP_CHECK, ok=True, body=json.dumps(counts))
        problems = _notifier_problems(report)
        if problems:
            await self._ping(NOTIFIER_CHECK, ok=False, body="; ".join(problems))
        else:
            await self._ping(NOTIFIER_CHECK, ok=True, body="ok")

    async def sweep_failed(self, error: BaseException) -> None:
        """Signal a pass that raised. Sends the exception's type, never its message."""
        await self._ping(SWEEP_CHECK, ok=False, body=type(error).__name__)

    async def _ping(self, check: str, *, ok: bool, body: str) -> None:
        url = f"{self._base_url}/{self._key}/{check}" + ("" if ok else "/fail")
        try:
            # The bound is on the whole ping: httpx's timeout is per phase
            # (connect, write, read, pool), and DNS has none at all.
            async with asyncio.timeout(PING_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(timeout=PING_TIMEOUT_SECONDS) as client:
                    response = await client.post(url, content=body)
        except Exception as exc:
            # Anything at all: a ping must never fail the sweep that sent it.
            # The type only: an httpx message can carry the URL, and the URL the key.
            logger.warning(f"healthchecks ping for {check} failed: {type(exc).__name__}")
            return
        if not response.is_success:
            logger.warning(
                f"healthchecks ping for {check} answered {response.status_code}",
                extra={"check": check, "status_code": response.status_code},
            )


def _notifier_problems(report: SweepReport) -> list[str]:
    """Why ``notifier-reachable`` fails this pass; empty when it does not."""
    problems = []
    if not report.notifier_ok:
        problems.append("notifier unreachable at sweep start")
    if report.owed:
        problems.append(f"{len(report.owed)} alert(s) owed")
    if report.undelivered:
        by_status = Counter(report.undelivered.values())
        detail = ", ".join(f"{n} {status}" for status, n in sorted(by_status.items()))
        problems.append(f"{len(report.undelivered)} alert(s) undelivered ({detail})")
    return problems


def heartbeat_from_environment(environ: Mapping[str, str] = os.environ) -> Heartbeat | None:
    """The heartbeat this process should use, or ``None`` if it pings nothing.

    Production only: the dev sweep is not watched, and a dev process that
    somehow held the key must not report production as alive. Without the key
    the sweep still runs — the checks' own silence is then the alert.
    """
    try:
        environment = environment_label(database_name(environ.get("DATABASE_URL", "")))
    except ValueError:
        return None
    if environment != "production":
        return None
    key = read_credential(CREDENTIAL_NAME, environ)
    if not key:
        logger.warning(
            f"no {CREDENTIAL_NAME} under $CREDENTIALS_DIRECTORY; this sweep is not "
            "reporting to healthchecks.io, and nothing outside this host is watching it"
        )
        return None
    return Heartbeat(key)
