"""Drift tests for deploy/status.service.

The production opt-in flag must live in the unit itself. Anything placed in an
EnvironmentFile is inherited by every process that sources that file, which
re-opens the hole for exactly the hand-run servers the guard targets. The
units load /etc/status/.env (live) and /etc/status/dev.env (dev), and no repo
.env since #9.
"""

from pathlib import Path

import pytest

from src.core import db_safety

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT = REPO_ROOT / "deploy" / "status.service"
SERVE = REPO_ROOT / "scripts" / "serve.sh"


def directives(unit: Path) -> str:
    """The lines systemd acts on, with comments and blanks dropped.

    Mirrors the helper in test_dev_systemd_unit.py: the absence checks are
    about what the unit *does*, and this unit explains at length why ExecStart
    is not a uvicorn line.
    """
    return "\n".join(
        line
        for line in unit.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_unit_sets_the_production_opt_in_flag():
    body = UNIT.read_text()
    assert f"Environment={db_safety.ALLOW_PROD_ENV_VAR}=1" in body


@pytest.mark.parametrize(
    "env_file", [Path("/etc/status/.env"), Path("/etc/status/dev.env")], ids=["live", "dev"]
)
def test_opt_in_flag_is_not_in_any_env_file_on_the_unit_load_path(env_file):
    """VM-local check. Skips loudly off-VM rather than passing vacuously."""
    if not env_file.exists():
        pytest.skip(f"{env_file} not present on this machine")
    assert db_safety.ALLOW_PROD_ENV_VAR not in env_file.read_text(), (
        f"{env_file} is on the unit's EnvironmentFile path and would leak "
        f"the opt-in to every process that sources it"
    )


def test_unit_launches_through_the_guarded_script():
    """ExecStart is the launch script, not a uvicorn line.

    The dev unit has made this argument since notifier#24: a second uvicorn spelling is
    a second code path that drifts out from under the guards. notifier#43 gives the
    production unit the same reason. It must resolve this host's tailnet
    address before binding, and systemd reads ``EnvironmentFile=`` *before*
    ``ExecStartPre`` runs — so no ExecStartPre can hand the address to
    ExecStart. Resolving and binding have to happen in one process.
    """
    body = directives(UNIT)
    assert "scripts/serve.sh" in body
    assert "uvicorn" not in body


def test_serve_script_still_serves_port_9000():
    assert "--port 9000" in SERVE.read_text()


def test_serve_script_binds_the_tailnet_address_only():
    """notifier#43 D3 — never 0.0.0.0, which exe.dev's proxy would republish on :9000."""
    body = SERVE.read_text()
    assert "tailnet_bind.sh" in body
    assert "0.0.0.0" not in body


def test_unit_orders_after_tailscaled():
    """Necessary but not sufficient — After= does not wait for an address.

    The wait itself lives in tailnet_bind.sh; this only stops the unit from
    racing tailscaled's *start*, which is the cheap half of observo#473.
    """
    after = [line for line in directives(UNIT).splitlines() if line.startswith("After=")]
    assert after, "unit declares no ordering at all"
    assert any("tailscaled.service" in line for line in after)


def test_unit_bounds_its_restart_loop_around_the_tailnet_wait():
    """Retry long enough to ride out tailscaled, then give up somewhere visible.

    Each failed start now costs the full tailnet wait before RestartSec even
    begins, so the default 10s StartLimitIntervalSec can never trip and a
    genuinely broken config would retry forever — the failure mode the dev
    unit's bound exists to prevent, arriving here by a different route.
    """
    body = directives(UNIT)
    assert "StartLimitBurst=" in body
    assert "StartLimitIntervalSec=" in body
    assert "Restart=on-failure" in body


BIND_OVERRIDE = "STATUS_BIND_HOST"


def test_bind_override_is_not_baked_into_either_unit():
    """The override is for tests and diagnosis, never for a deployment.

    Same rule as STATUS_ALLOW_PROD_DB one file over: a unit or env file that
    carried this would move the bind off the tailnet permanently and silently,
    undoing notifier#43 D3 while every health check stayed green.
    """
    for unit in (UNIT, REPO_ROOT / "deploy" / "status-dev.service"):
        assert BIND_OVERRIDE not in directives(unit), f"{unit.name} pins the bind override"


@pytest.mark.parametrize(
    "env_file", [Path("/etc/status/.env"), Path("/etc/status/dev.env")], ids=["live", "dev"]
)
def test_bind_override_is_not_in_any_env_file_on_the_unit_load_path(env_file):
    """VM-local check. Skips loudly off-VM rather than passing vacuously."""
    if not env_file.exists():
        pytest.skip(f"{env_file} not present on this machine")
    assert BIND_OVERRIDE not in env_file.read_text()
