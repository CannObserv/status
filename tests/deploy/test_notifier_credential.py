"""The notifier API key reaches co-status as a systemd credential (D13).

co-status holds exactly one delivery secret: the key it presents to
notifier's ``/api/v1/dispatch``. It is never an environment variable. An
``EnvironmentFile=`` is inherited by every process that sources it and lands
in ``/proc/<pid>/environ``; ``LoadCredential=`` hands the unit a private,
root-sourced file under ``$CREDENTIALS_DIRECTORY`` instead. The pattern is
watcher's (CannObserv/watcher#297).

Production and development read different files, because notifier refuses a
development key on ``:9000`` and a production key must never reach ``:9001``'s
database: sharing one file would make that mix-up a typo away.
"""

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
UNITS = {
    "production": DEPLOY / "status.service",
    "development": DEPLOY / "status-dev.service",
    "production-sweep": DEPLOY / "status-sweep.service",
    "development-sweep": DEPLOY / "status-sweep-dev.service",
}

#: The name alerting code reads under $CREDENTIALS_DIRECTORY.
CREDENTIAL_NAME = "notifier-key"

_LOAD = re.compile(r"^LoadCredential=([^:\s]+):(\S+)\s*$", re.MULTILINE)


def _credentials(unit: Path) -> dict[str, str]:
    return dict(_LOAD.findall(unit.read_text()))


@pytest.mark.parametrize("environment", UNITS)
def test_each_unit_loads_the_notifier_key_as_a_credential(environment):
    creds = _credentials(UNITS[environment])
    assert CREDENTIAL_NAME in creds, f"{UNITS[environment].name} has no LoadCredential"
    assert creds[CREDENTIAL_NAME].startswith("/etc/status/")


def test_production_and_development_read_different_files():
    prod = _credentials(UNITS["production"])[CREDENTIAL_NAME]
    dev = _credentials(UNITS["development"])[CREDENTIAL_NAME]
    assert prod != dev


@pytest.mark.parametrize("environment", ["production", "development"])
def test_each_sweep_reads_its_own_environments_key(environment):
    """The sweep is the other half of alerting: without the key it detects
    outages and tells no one."""
    api = _credentials(UNITS[environment])[CREDENTIAL_NAME]
    sweep = _credentials(UNITS[f"{environment}-sweep"])[CREDENTIAL_NAME]
    assert sweep == api


@pytest.mark.parametrize("environment", UNITS)
def test_no_unit_carries_the_key_as_environment(environment):
    """The whole point of the credential: nothing notifier-key-shaped is in the
    process environment."""
    body = UNITS[environment].read_text()
    for line in body.splitlines():
        if line.startswith(("Environment=", "EnvironmentFile=")):
            assert "NOTIFIER" not in line.upper(), line
