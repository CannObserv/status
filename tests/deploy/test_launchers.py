"""Drift tests for the three launch scripts the units run (#9, spec R5).

**No sync at start or pass.** A bare ``uv run`` syncs the environment first, so
an edit to ``pyproject.toml`` or ``uv.lock`` changed production on the sweep's
next pass. Syncing is a build step of ``scripts/deploy.sh``; the launchers run
exactly what was built (the cohort template's ``--frozen --no-sync``).

**Physical paths.** The units start through the ``/srv/status/{live,dev}``
symlinks. ``cd -P`` resolves the release once, so a later swap cannot hand a
running API a module imported from a different release.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = [REPO_ROOT / "scripts" / name for name in ("serve.sh", "sweep.sh", "dev_server.sh")]


def commands(script: Path) -> str:
    """The lines bash runs, with comments and blank lines dropped."""
    return "\n".join(
        line
        for line in script.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("script", LAUNCHERS, ids=lambda p: p.name)
def test_every_uv_run_is_frozen_and_never_syncs(script):
    """Commands only: a heredoc telling a person to run ``uv run alembic`` is advice."""
    body = commands(script)
    runs = re.findall(r"(?:^\s*(?:exec\s+)?|\$\()(uv run[^\n]*)", body, re.MULTILINE)
    assert runs, f"{script.name} runs nothing through uv"
    for run in runs:
        assert run.startswith("uv run --frozen --no-sync "), run


@pytest.mark.parametrize("script", LAUNCHERS, ids=lambda p: p.name)
def test_resolves_the_physical_release_directory(script):
    assert 'cd -P "$(dirname "${BASH_SOURCE[0]}")/.."' in commands(script)
