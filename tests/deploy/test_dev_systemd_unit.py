"""Drift tests for deploy/status-dev.service — the persistent dev endpoint.

The dev unit is the mirror image of the production one. `status.service`
must carry the production opt-in; this unit must never carry it. Anything
non-production points here, and the whole value
of a `development`-marked key is that it cannot reach production — a unit that
inherited the opt-in would quietly undo that.

It also must not spell out its own uvicorn line. `scripts/dev_server.sh`
already unsets the opt-in, swaps in DEV_DATABASE_URL, delegates the URL check
to src.core.db_safety, and refuses an unmigrated database (notifier#23). A second
ExecStart spelling is a second code path that can drift out from under all
four checks.
"""

import re
from pathlib import Path

from src.core import db_safety

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_UNIT = REPO_ROOT / "deploy" / "status-dev.service"
PROD_UNIT = REPO_ROOT / "deploy" / "status.service"


def directives(unit: Path) -> str:
    """The lines systemd acts on, with comments and blanks dropped.

    The absence checks below are about what the unit *does*, and this unit
    explains at length why it omits the production opt-in and why ExecStart is
    not a uvicorn line. Matching raw text would make those comments fail the
    tests they document.
    """
    return "\n".join(
        line
        for line in unit.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_dev_unit_exists():
    assert DEV_UNIT.is_file()


def test_dev_unit_never_carries_the_production_opt_in():
    """The mirror of test_unit_sets_the_production_opt_in_flag."""
    assert db_safety.ALLOW_PROD_ENV_VAR not in directives(DEV_UNIT), (
        "the production opt-in belongs to status.service alone; on this "
        "unit it would let the dev endpoint open the production database"
    )


def test_dev_unit_launches_through_the_guarded_script():
    """Not a hand-rolled uvicorn line — that bypasses every guard in notifier#22/#23."""
    body = directives(DEV_UNIT)
    assert "scripts/dev_server.sh" in body
    assert "uvicorn" not in body


def test_dev_unit_does_not_pin_a_database_url():
    """DATABASE_URL is derived from DEV_DATABASE_URL by the script, once."""
    assert "Environment=DATABASE_URL" not in directives(DEV_UNIT)


def test_dev_unit_disables_the_reloader():
    """A wedged reloader keeps running, so Restart=on-failure never fires."""
    assert "Environment=STATUS_DEV_RELOAD=0" in directives(DEV_UNIT)


def test_dev_unit_stays_off_the_production_port():
    assert "9000" not in directives(DEV_UNIT)


def test_dev_unit_bounds_its_restart_loop():
    """An unmigrated dev DB exits non-zero by design; don't loop on it forever."""
    body = directives(DEV_UNIT)
    assert "StartLimitBurst=" in body
    assert "Restart=on-failure" in body


def _seconds(unit: Path, key: str) -> int:
    """Read a systemd time-valued setting as whole seconds.

    Accepts a bare integer or an explicit ``s`` suffix, both of which systemd
    takes. Anything else — ``1min``, ``2h`` — is rejected with a message
    naming the parser rather than raising ValueError from int(), so the
    failure points at this helper instead of looking like a broken test.
    """
    for line in directives(unit).splitlines():
        name, _, value = line.partition("=")
        if name.strip() != key:
            continue
        raw = value.strip().removesuffix("s")
        assert raw.isdigit(), (
            f"{key}={value.strip()} in {unit.name} is a systemd time span this "
            f"helper does not parse — express it in seconds, or teach _seconds() the unit"
        )
        return int(raw)
    raise AssertionError(f"{key} not set in {unit.name}")


def test_dev_unit_survives_a_slow_database_at_boot():
    """The bound must outlast a boot race, not just an unmigrated database.

    A unit that trips StartLimitBurst stays failed until someone runs
    `systemctl reset-failed`. The production unit sets no limit at all, so it
    retries until Postgres is up; this one would give up permanently and leave
    the endpoint silently down — the exact outcome notifier#24 exists to prevent.
    `After=postgresql.service` orders startup but does not wait for the
    cluster to accept connections.

    One minute of retries distinguishes a slow boot from a real
    misconfiguration, which no amount of waiting will fix.
    """
    window = _seconds(DEV_UNIT, "RestartSec") * _seconds(DEV_UNIT, "StartLimitBurst")
    assert window >= 60, f"gives up after {window}s — too fast to ride out a slow Postgres"
    assert window <= _seconds(DEV_UNIT, "StartLimitIntervalSec"), (
        "StartLimitIntervalSec is shorter than the retries it is meant to count, "
        "so the limit can never actually trip and the loop runs forever"
    )


def test_dev_unit_reports_which_commit_it_is_serving():
    """/health on :9001 must not answer `dev` while :9000 answers a SHA (notifier#24).

    A bug reported against the dev endpoint is unactionable without it. Kept
    off the production unit's /run/status/build-id so the two units never
    race to write one file.
    """
    body = directives(DEV_UNIT)
    assert "BUILD_ID=" in body
    assert "build-id-dev" in body
    # Every mention must be the -dev file. An end-of-line check would miss the
    # one that matters: the write target sits mid-line inside the ExecStartPre
    # shell quoting, and reverting only that half is what recreates the race.
    assert re.search(r"/run/status/build-id(?!-dev)", body) is None


def test_dev_unit_starts_at_boot():
    """Persistent means persistent — it must survive a VM reboot (notifier#24)."""
    assert "WantedBy=multi-user.target" in directives(DEV_UNIT)


def test_the_two_units_do_not_collide_on_a_port():
    """Both ExecStarts are scripts now (notifier#43), so the ports live there."""
    assert "--port 9000" in (REPO_ROOT / "scripts" / "serve.sh").read_text()
    assert "9001" not in directives(PROD_UNIT)
    assert "9001" in (REPO_ROOT / "scripts" / "dev_server.sh").read_text()


def test_dev_unit_orders_after_tailscaled():
    """Same boot race as the production unit (notifier#43 R1 / observo#473)."""
    after = [line for line in directives(DEV_UNIT).splitlines() if line.startswith("After=")]
    assert after and any("tailscaled.service" in line for line in after)
