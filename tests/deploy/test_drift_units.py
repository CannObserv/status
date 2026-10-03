"""Drift tests for the drift check's units (#12): status-drift.{service,timer}.

Production only and hourly. The service runs the live release's launcher,
holds the healthchecks.io ping key with the sweep's fallback (a missing key
never fails it), and holds nothing else: no database, no notifier key. A dev
twin would watch nothing anyone alerts on (#13: dev is unwatched by design).
"""

import re
import stat
from pathlib import Path

from src.core.drift import CHECK_TIMEOUT_SECONDS
from src.core.heartbeat import CREDENTIAL_NAME, PING_TIMEOUT_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
LAUNCHER = REPO_ROOT / "scripts" / "drift.sh"
SERVICE = DEPLOY / "status-drift.service"
TIMER = DEPLOY / "status-drift.timer"


def directives(unit: Path) -> list[str]:
    """The lines systemd acts on, with comments and blanks dropped."""
    return [
        line.strip()
        for line in unit.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def values(unit: Path, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in directives(unit) if line.startswith(f"{key}=")]


class TestService:
    def test_a_oneshot_as_exedev(self):
        assert values(SERVICE, "Type") == ["oneshot"]
        assert values(SERVICE, "User") == ["exedev"]

    def test_runs_the_live_releases_own_launcher(self):
        assert values(SERVICE, "WorkingDirectory") == ["/srv/status/live"]
        assert values(SERVICE, "ExecStart") == ["/srv/status/live/scripts/drift.sh"]

    def test_never_the_development_checkout(self):
        assert not [line for line in directives(SERVICE) if "/home/exedev" in line]

    def test_loads_the_ping_key_with_the_lone_newline_fallback(self):
        assert values(SERVICE, "LoadCredential") == [f"{CREDENTIAL_NAME}:/etc/status/hc-ping.key"]
        assert values(SERVICE, "SetCredential") == [rf"{CREDENTIAL_NAME}:\n"]

    def test_holds_nothing_else(self):
        body = "\n".join(directives(SERVICE))
        for absent in ("EnvironmentFile=", "STATUS_ALLOW_PROD_DB", "notifier-key", "Restart="):
            assert absent not in body, absent

    def test_its_timeout_covers_github_and_the_ping(self):
        (raw,) = values(SERVICE, "TimeoutStartSec")
        assert int(raw) >= CHECK_TIMEOUT_SECONDS + PING_TIMEOUT_SECONDS + 15


class TestTimer:
    def test_starts_the_service_hourly(self):
        """healthchecks.io's co-status-drift: period 1 h, grace 2 h (RUNBOOK)."""
        assert values(TIMER, "Unit") == ["status-drift.service"]
        assert values(TIMER, "OnUnitActiveSec") == ["1h"]

    def test_runs_after_boot(self):
        assert values(TIMER, "OnBootSec")

    def test_is_installable(self):
        assert values(TIMER, "WantedBy") == ["timers.target"]


def test_the_launcher_exists_and_is_executable():
    """Without the bit every run is 203/EXEC, and the check's silence names the timer (CR 13)."""
    assert LAUNCHER.is_file()
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR


def test_no_dev_twin():
    assert not [p.name for p in DEPLOY.iterdir() if re.match(r"status-drift-dev\.", p.name)]
