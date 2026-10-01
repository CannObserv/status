"""Tests for alembic/env.py — migrations cross the production guard too (#15).

Until #15 alembic was exempt from ``src.core.db_safety``: ``main`` was the
deployed code, so a hand-run ``alembic upgrade head`` against production was
the deploy. Since #9 production runs releases and ``scripts/deploy.sh`` is
the one way to migrate it, opting in for live only. A hand-run migration
against ``status`` now skips dev's rehearsal and the deploy's order, and
``. scripts/load_env.sh`` leaves ``DATABASE_URL`` pointing there.

Run as a real process, the way an operator and ``deploy.sh`` run it, against
URLs with nothing listening: a refusal shows as ``ProductionDatabaseError``,
a pass as a failed connection, and neither can change a database.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.core import db_safety

REPO_ROOT = Path(__file__).resolve().parents[1]

NOTHING_LISTENING = "postgresql+asyncpg://u@127.0.0.1:1/{name}"


def alembic(*argv: str, name: str, opt_in: bool = False) -> subprocess.CompletedProcess:
    """Run ``alembic`` against database *name*, the opt-in set only if asked."""
    env = {k: v for k, v in os.environ.items() if k != db_safety.ALLOW_PROD_ENV_VAR}
    env["DATABASE_URL"] = NOTHING_LISTENING.format(name=name)
    if opt_in:
        env[db_safety.ALLOW_PROD_ENV_VAR] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *argv],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("argv", [("upgrade", "head"), ("check",), ("current",)], ids=" ".join)
def test_a_production_name_is_refused_without_the_opt_in(argv):
    """Every command that connects, not only ``upgrade``: the guard sits on
    the connection."""
    result = alembic(*argv, name="status")
    assert result.returncode != 0
    assert "ProductionDatabaseError" in result.stderr


def test_the_refusal_points_at_the_deploy_not_the_opt_in():
    """The generic message says to set the opt-in, which is the wrong advice
    for a hand-run migration: production is migrated by ``deploy.sh``. The
    last line, the one read, names no command: the refused one may be a
    ``check`` after ``. scripts/load_env.sh``, and ``upgrade head`` would then
    be advice to migrate dev."""
    result = alembic("check", name="status")
    last = result.stderr.strip().splitlines()[-1]
    assert "scripts/deploy.sh" in last
    assert "DEV_DATABASE_URL" in last
    assert "upgrade" not in last


def test_the_opt_in_lets_a_production_name_through():
    """What ``deploy.sh`` passes for live. Past the guard, the run reaches the
    connection, which nothing answers."""
    result = alembic("upgrade", "head", name="status", opt_in=True)
    assert result.returncode != 0
    assert "ProductionDatabaseError" not in result.stderr
    assert "ConnectionRefusedError" in result.stderr


def test_a_dev_name_needs_no_opt_in():
    """``DATABASE_URL="$DEV_DATABASE_URL" uv run alembic upgrade head``, the
    recipe ``dev_server.sh`` prints, keeps working."""
    result = alembic("upgrade", "head", name="status_dev")
    assert "ProductionDatabaseError" not in result.stderr
    assert "ConnectionRefusedError" in result.stderr


def test_offline_mode_is_exempt():
    """``--sql`` prints SQL and opens no connection, so there is nothing to
    guard: the URL only picks the dialect. Requiring the opt-in here would
    teach setting it for a command that cannot touch production."""
    result = alembic("upgrade", "head", "--sql", name="status")
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE alembic_version" in result.stdout
