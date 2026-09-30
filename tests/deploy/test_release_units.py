"""Drift tests: every unit runs a release, never the development checkout (#9).

On 2026-09-29 all four units ran ``/home/exedev/status``, working tree included,
and an unmigrated model edit crashed the production sweep for 40 minutes.

- **R2.** Units start through ``/srv/status/live`` or ``/srv/status/dev``, the
  symlinks ``scripts/deploy.sh`` swaps.
- **R10.** There is no git stamp at start: a release has no ``.git``, and its
  ``REVISION`` is the build id.
- **R11.** No unit reads a repo ``.env``. The live units read
  ``/etc/status/.env``, the dev units ``/etc/status/dev.env``. The repo ``.env``
  carries PATs and API keys that production has no business holding.
"""

from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
LIVE = {DEPLOY / "status.service", DEPLOY / "status-sweep.service"}
DEV = {DEPLOY / "status-dev.service", DEPLOY / "status-sweep-dev.service"}
ROOTS = {**dict.fromkeys(LIVE, "/srv/status/live"), **dict.fromkeys(DEV, "/srv/status/dev")}
ENV_FILES = {
    **dict.fromkeys(LIVE, ["/etc/status/.env"]),
    **dict.fromkeys(DEV, ["/etc/status/dev.env"]),
}
UNITS = sorted(ROOTS, key=lambda p: p.name)


def directives(unit: Path) -> list[str]:
    """The lines systemd acts on, with comments and blanks dropped."""
    return [
        line.strip()
        for line in unit.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def values(unit: Path, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in directives(unit) if line.startswith(f"{key}=")]


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_never_runs_the_development_checkout(unit):
    assert not [line for line in directives(unit) if "/home/exedev" in line]


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_works_in_its_release_symlink(unit):
    assert values(unit, "WorkingDirectory") == [ROOTS[unit]]


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_starts_the_release_own_launcher(unit):
    (exec_start,) = values(unit, "ExecStart")
    assert exec_start.startswith(f"{ROOTS[unit]}/scripts/")


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_no_git_stamp_at_start(unit):
    """A release has no ``.git``; ``REVISION`` is the build id (R10)."""
    body = "\n".join(directives(unit))
    for gone in ("rev-parse", "BUILD_ID", "/run/status"):
        assert gone not in body, f"{unit.name} still carries {gone}"


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_reads_only_its_etc_env_file(unit):
    """Required, not ``-``-optional: a missing file fails the start loudly."""
    assert values(unit, "EnvironmentFile") == ENV_FILES[unit]
