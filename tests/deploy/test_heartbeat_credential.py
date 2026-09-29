"""The healthchecks.io ping key reaches the production sweep as a credential (#1).

Only ``status-sweep.service`` pings: the dev sweep is not watched, and the API
has nothing to report. The key is a credential for D13's reason — anyone
holding it can report a dead sweep as alive — so it is never in an env file.

**A missing key must not stop the sweep.** ``LoadCredential=`` on a missing
file fails the unit (243/CREDENTIALS), which would turn "not watched" into
"not sweeping": every dead-man's timer silenced by the thing meant to watch
them. A non-empty ``SetCredential=`` of the same name is systemd's fallback
when the file cannot be read; an empty one is not (both checked on systemd
255). The fallback is a lone newline, which :func:`read_credential` reads as
absent, and the sweep runs unwatched — which healthchecks then reports.
"""

import re
from pathlib import Path

import pytest

from src.core.heartbeat import CREDENTIAL_NAME

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
SWEEP = DEPLOY / "status-sweep.service"
NOT_PINGING = [
    DEPLOY / "status.service",
    DEPLOY / "status-dev.service",
    DEPLOY / "status-sweep-dev.service",
]

_LOAD = re.compile(rf"^LoadCredential={CREDENTIAL_NAME}:(\S+)\s*$", re.MULTILINE)
_SET = re.compile(rf"^SetCredential={CREDENTIAL_NAME}:(.*)$", re.MULTILINE)


def test_the_production_sweep_loads_the_key_from_etc_status():
    (path,) = _LOAD.findall(SWEEP.read_text())
    assert path == "/etc/status/hc-ping.key"


def test_a_missing_key_file_falls_back_to_a_lone_newline():
    (value,) = _SET.findall(SWEEP.read_text())
    assert value == r"\n", "an empty SetCredential= is no fallback; the unit fails with 243"


@pytest.mark.parametrize("unit", NOT_PINGING, ids=lambda p: p.name)
def test_no_other_unit_holds_the_key(unit):
    assert CREDENTIAL_NAME not in unit.read_text()


def test_the_key_is_never_environment():
    for line in SWEEP.read_text().splitlines():
        if line.startswith(("Environment=", "EnvironmentFile=")):
            assert "hc-ping" not in line.lower() and "HC_PING" not in line.upper(), line
