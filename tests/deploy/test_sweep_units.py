"""Drift tests for the four sweep units and scripts/sweep.sh (notifier#56).

The sweep is what turns silence into an alert, and it runs from a systemd
timer rather than inside the API process on purpose: an alerter that rides the
thing it watches stops reporting exactly when it is needed. That decision only
holds if the units keep the properties below — a sweep that opens the wrong
database, or that never fires, fails the way the feature exists to prevent,
which is silently.

Mirrors the shape of test_systemd_unit.py and test_dev_systemd_unit.py: the
production sweep must carry the production opt-in, the dev sweep must never.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from src.core import db_safety, heartbeat, sweep

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
SWEEP_SCRIPT = REPO_ROOT / "scripts" / "sweep.sh"
SWEEP_ENTRYPOINT = REPO_ROOT / "scripts" / "sweep_monitors.py"

PROD_SERVICE = DEPLOY / "status-sweep.service"
PROD_TIMER = DEPLOY / "status-sweep.timer"
DEV_SERVICE = DEPLOY / "status-sweep-dev.service"
DEV_TIMER = DEPLOY / "status-sweep-dev.timer"

#: Environment variable that puts scripts/sweep.sh on the dev database.
DEV_FLAG = "STATUS_SWEEP_DEV"


def directives(unit: Path) -> str:
    """The lines systemd acts on, with comments and blanks dropped.

    Same helper as the other two unit drift tests: the absence checks are
    about what a unit *does*, and these units explain at length why the dev
    one omits the production opt-in.
    """
    return "\n".join(
        line
        for line in unit.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _seconds(unit: Path, key: str) -> int:
    """Read a systemd time-valued setting as whole seconds."""
    for line in directives(unit).splitlines():
        name, _, value = line.partition("=")
        if name.strip() != key:
            continue
        raw = value.strip().removesuffix("s")
        assert raw.isdigit(), (
            f"{key}={value.strip()} in {unit.name} is a systemd time span this "
            f"helper does not parse — express it in seconds"
        )
        return int(raw)
    raise AssertionError(f"{key} not set in {unit.name}")


@pytest.mark.parametrize(
    "unit", [PROD_SERVICE, PROD_TIMER, DEV_SERVICE, DEV_TIMER], ids=lambda p: p.name
)
def test_unit_exists(unit):
    assert unit.is_file()


def test_sweep_script_exists_and_is_executable():
    assert SWEEP_SCRIPT.is_file()
    assert SWEEP_SCRIPT.stat().st_mode & stat.S_IXUSR


class TestDatabaseTargeting:
    """The whole point of two timers is that they watch two databases."""

    def test_production_sweep_carries_the_production_opt_in(self):
        assert f"Environment={db_safety.ALLOW_PROD_ENV_VAR}=1" in directives(PROD_SERVICE)

    def test_dev_sweep_never_carries_the_production_opt_in(self):
        """It would alert on production monitors from the dev endpoint's timer,
        and dispatch to production channels to do it."""
        assert db_safety.ALLOW_PROD_ENV_VAR not in directives(DEV_SERVICE)

    def test_dev_sweep_asks_the_script_for_the_dev_database(self):
        assert f"Environment={DEV_FLAG}=1" in directives(DEV_SERVICE)

    def test_production_sweep_does_not_set_the_dev_flag(self):
        assert DEV_FLAG not in directives(PROD_SERVICE)

    def test_neither_unit_pins_a_database_url(self):
        """DATABASE_URL comes from the env file or from DEV_DATABASE_URL via
        the script — never hardcoded where it can drift out of sync."""
        for unit in (PROD_SERVICE, DEV_SERVICE):
            assert "Environment=DATABASE_URL" not in directives(unit), unit.name

    @pytest.mark.parametrize("unit", [PROD_SERVICE, DEV_SERVICE], ids=lambda p: p.name)
    def test_both_launch_through_the_one_guarded_script(self, unit):
        """One spelling, as with serve.sh and dev_server.sh. A second inline
        `uv run python` line is a second code path that drifts out from under
        the checks in the script."""
        body = directives(unit)
        assert "scripts/sweep.sh" in body
        assert "uv run" not in body


class TestScheduling:
    """A timer that does not fire is indistinguishable from a healthy fleet."""

    @pytest.mark.parametrize("timer", [PROD_TIMER, DEV_TIMER], ids=lambda p: p.name)
    def test_timer_repeats_often_enough_to_bound_alert_latency(self, timer):
        """The interval is the worst-case lateness of every missing alert, on
        top of the monitor's own grace. Minutes, not tens of minutes."""
        assert _seconds(timer, "OnUnitActiveSec") <= 300

    @pytest.mark.parametrize("timer", [PROD_TIMER, DEV_TIMER], ids=lambda p: p.name)
    def test_timer_starts_itself_after_a_reboot(self, timer):
        """A sweep that has to be started by hand is one nobody restarts."""
        assert "WantedBy=timers.target" in directives(timer)
        assert "OnBootSec=" in directives(timer)

    @pytest.mark.parametrize("timer", [PROD_TIMER, DEV_TIMER], ids=lambda p: p.name)
    def test_timer_pins_its_accuracy(self, timer):
        """systemd's default AccuracySec is 1 minute, which lets it coalesce a
        60-second timer into something much coarser to save wakeups."""
        assert "AccuracySec=" in directives(timer)

    def test_each_timer_names_its_own_service(self):
        assert f"Unit={PROD_SERVICE.name}" in directives(PROD_TIMER)
        assert f"Unit={DEV_SERVICE.name}" in directives(DEV_TIMER)

    @pytest.mark.parametrize("unit", [PROD_SERVICE, DEV_SERVICE], ids=lambda p: p.name)
    def test_service_is_oneshot_and_not_started_at_boot(self, unit):
        """The timer starts it. An [Install] section on the service would run
        a sweep at boot outside the timer's schedule, and Type=simple would
        have systemd call it 'running' the moment it forked."""
        body = directives(unit)
        assert "Type=oneshot" in body
        assert "WantedBy=multi-user.target" not in body

    @pytest.mark.parametrize("unit", [PROD_SERVICE, DEV_SERVICE], ids=lambda p: p.name)
    def test_service_is_bounded_in_time(self, unit):
        """Apprise delivery is network I/O. Without a timeout a wedged
        endpoint holds the sweep open past the next tick, and systemd skips
        every firing while the last one is still running — the timer looks
        alive and nothing is being checked."""
        assert "TimeoutStartSec=" in directives(unit)
        assert _seconds(unit, "TimeoutStartSec") <= 300

    def test_the_heartbeat_leaves_the_pass_most_of_its_bound(self):
        """#13: the API's window and three pings run after the pass, inside the same
        TimeoutStartSec. Past it systemd kills the pass before any /fail, every pass."""
        heartbeat_seconds = heartbeat.API_WINDOW_SECONDS + 3 * heartbeat.PING_TIMEOUT_SECONDS
        assert heartbeat_seconds <= _seconds(PROD_SERVICE, "TimeoutStartSec") / 2

    def test_redeliveries_and_the_heartbeat_fit_the_bound(self):
        """#10: no redelivery starts past REDELIVERY_WINDOW into the pass. 30 s is
        left for the one still in flight (~8 s per failed channel) and the commit."""
        heartbeat_seconds = heartbeat.API_WINDOW_SECONDS + 3 * heartbeat.PING_TIMEOUT_SECONDS
        window = sweep.REDELIVERY_WINDOW.total_seconds()
        assert window + heartbeat_seconds <= _seconds(PROD_SERVICE, "TimeoutStartSec") - 30


class TestScript:
    def test_delegates_env_loading_to_the_shared_loader(self):
        """One env-loading recipe, in scripts/load_env.sh — not a second copy."""
        body = SWEEP_SCRIPT.read_text()
        assert "load_env.sh" in body
        assert "| xargs" not in body

    def test_dev_mode_swaps_in_the_dev_database_and_drops_the_opt_in(self):
        body = SWEEP_SCRIPT.read_text()
        assert "DEV_DATABASE_URL" in body
        assert f"unset {db_safety.ALLOW_PROD_ENV_VAR}" in body

    def test_refuses_to_run_without_the_notifier_key(self):
        """Without the key the sweep finds the overdue monitors and tells
        nobody — an outage detected and reported to no one. Refuse where the
        unit's status shows it (spec D13). Behavioural: run the script with
        no credential and watch it stop before `uv` is ever called."""
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CREDENTIALS_DIRECTORY", "STATUS_SWEEP_DEV"}
        }
        env["DATABASE_URL"] = "postgresql+asyncpg://u@h/status_dev"
        result = subprocess.run(
            [str(SWEEP_SCRIPT)], env=env, capture_output=True, text=True, timeout=60
        )
        assert result.returncode != 0
        assert "notifier-key" in result.stderr
        assert "Traceback" not in result.stderr

    def test_runs_the_python_entrypoint(self):
        assert SWEEP_ENTRYPOINT.is_file()
        assert "scripts/sweep_monitors.py" in SWEEP_SCRIPT.read_text()

    def test_never_binds_a_port(self):
        """The sweep is not a server; it needs no tailnet address and must not
        wait for one."""
        body = SWEEP_SCRIPT.read_text()
        assert "tailnet_bind" not in body
        assert "uvicorn" not in body
