"""The pings that watch co-status: healthchecks.io, outside the cohort (#1, #13).

The sweep is the only thing watching for consumer silence, so it needs a
watcher that does not share its failure modes: not this host, not Postgres,
not notifier. Each production pass pings three healthchecks.io checks, which
alert over their own email and Slack channels when pings stop or fail:

- ``co-status-sweep`` — the pass completed and committed (``/fail`` if it raised);
- ``notifier-reachable`` — notifier answered ``/health`` in this environment,
  accepted every alert the pass sent (nothing owed), **and** delivered the
  last alert of every missing monitor (nothing undelivered, #6);
- ``co-status-api`` — the production API answered ``/ready`` (#13). The API
  is the half that records check-ins: down, it makes every consumer look
  ``missing`` when the fault is co-status.

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
from enum import StrEnum

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
API_CHECK = "co-status-api"
#: The production API by the name consumers use, over the tailnet: its bind,
#: MagicDNS and database, all at once. Production only, like every ping here.
API_READY_URL = "http://status:9000/ready"
#: How long a pass keeps asking before the API counts as down. A restart is
#: about a second (deploy.sh's forced pass on 2026-10-01 ended 0.2 s before
#: uvicorn listened); a broken API is still broken after 20.
API_WINDOW_SECONDS = 20.0
API_RETRY_SECONDS = 2.0
#: Of ``/ready``'s answer, in the ping body. Its JSON is about 100 bytes;
#: anything longer is not ``/ready`` talking.
API_BODY_LIMIT = 200

#: Three pings per pass, and the API's window, inside ``TimeoutStartSec=120``.
#: No retry: the next pass is 60 seconds away and each check's grace period
#: absorbs a missed one.
PING_TIMEOUT_SECONDS = 5.0


class Signal(StrEnum):
    """What a ping tells its check, as healthchecks.io's URL suffix."""

    UP = ""
    FAIL = "/fail"
    #: Recorded in the check's log; its state does not change (#12).
    LOG = "/log"


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

    def __init__(
        self, ping_key: str, *, base_url: str = PING_BASE_URL, api_url: str = API_READY_URL
    ) -> None:
        self._key = ping_key
        self._base_url = base_url
        self._api_url = api_url
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

    async def api_checked(self) -> None:
        """Signal whether the production API is ready to record check-ins (#13).

        Called after the pass and its own pings, so a slow API never delays an
        alert. Retried within :data:`API_WINDOW_SECONDS` before it counts as down.
        """
        ok, body = await _probe_api(self._api_url)
        if not ok:
            logger.warning(f"co-status API not ready at {self._api_url}: {body}")
        await self._ping(API_CHECK, ok=ok, body=body)

    async def _ping(self, check: str, *, ok: bool, body: str) -> None:
        await ping(
            self._key, check, Signal.UP if ok else Signal.FAIL, body, base_url=self._base_url
        )


async def ping(
    key: str, check: str, signal: Signal, body: str, *, base_url: str = PING_BASE_URL
) -> None:
    """Send *body* to *check* as *signal*. Never raises; a failure is a journal warning."""
    # httpx logs every request URL at INFO, and this URL carries the key.
    _redact_in_httpx_log(key)
    url = f"{base_url}/{key}/{check}{signal}"
    try:
        # The bound is on the whole ping: httpx's timeout is per phase
        # (connect, write, read, pool), and DNS has none at all.
        async with asyncio.timeout(PING_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(timeout=PING_TIMEOUT_SECONDS) as client:
                response = await client.post(url, content=body)
    except Exception as exc:
        # Anything at all: a ping must never fail the process that sent it.
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


async def _probe_api(url: str) -> tuple[bool, str]:
    """Ask *url* until it is ready or the window closes: ``(ready, what it said)``."""
    answer = "TimeoutError"
    try:
        # One bound on every try together: httpx's timeout is per phase, and
        # DNS has none at all.
        async with asyncio.timeout(API_WINDOW_SECONDS):
            async with httpx.AsyncClient(timeout=PING_TIMEOUT_SECONDS) as client:
                while True:
                    # What a try the window cuts off reports: it never returns
                    # to overwrite this, so the body says how the window ended.
                    answer = "TimeoutError"
                    ready, answer = await _ask_ready(client, url)
                    if ready:
                        return True, answer
                    await asyncio.sleep(API_RETRY_SECONDS)
    except TimeoutError:
        return False, answer


async def _ask_ready(client: httpx.AsyncClient, url: str) -> tuple[bool, str]:
    """One ``GET /ready``. Ready is a 200 from the production database, nothing less."""
    try:
        response = await client.get(url)
    except Exception as exc:
        # The message too, unlike a ping's: this URL carries no key, and the
        # message is what tells nothing listening from a name that won't resolve.
        message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return False, message[:API_BODY_LIMIT]
    answer = f"{response.status_code} {response.text[:API_BODY_LIMIT]}"
    try:
        payload = response.json()
    except ValueError:
        return False, answer
    ready = (
        response.status_code == 200
        and isinstance(payload, dict)
        and payload.get("environment") == "production"
    )
    return ready, answer


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
