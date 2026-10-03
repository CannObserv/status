"""Run one drift check: does live lag origin/main in code that runs? (#12)

Entry point for ``deploy/status-drift.service``, which ``status-drift.timer``
starts every hour. Launched through ``scripts/drift.sh``, never directly.

Reads the live release's ``REVISION`` (:mod:`src.core.build`), asks GitHub
(:mod:`src.core.drift`), and pings healthchecks.io's ``co-status-drift``. No
database and no notifier: this watches what is deployed, not what it does.
Without the ping key it still runs and logs; the check's silence is the alert.
"""

import asyncio
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime

from src.core import build
from src.core.credentials import read_credential
from src.core.drift import DRIFT_CHECK, assess
from src.core.heartbeat import CREDENTIAL_NAME, Signal, ping
from src.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


async def main(environ: Mapping[str, str] = os.environ) -> int:
    """Check once, log the verdict, ping it. Always 0: the ping is the signal."""
    live = build.build_id()
    verdict = await assess(live, now=datetime.now(UTC))
    # /log too: GitHub silent for hours is what the RUNBOOK's silent row looks for (CR 5).
    level = logger.info if verdict.signal is Signal.UP else logger.warning
    level(f"drift check: {verdict.body}", extra={"build": live, "signal": verdict.signal.name})
    key = read_credential(CREDENTIAL_NAME, environ)
    if not key:
        logger.warning(
            f"no {CREDENTIAL_NAME} under $CREDENTIALS_DIRECTORY; {DRIFT_CHECK} is not "
            "pinged, and its silence will alert"
        )
        return 0
    await ping(key, DRIFT_CHECK, verdict.signal, verdict.body)
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(asyncio.run(main()))
