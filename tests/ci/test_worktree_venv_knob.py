"""Tests for ``.skills/worktree_venv``: each worktree gets its own venv (#146).

With the knob's default, ``link``, ``worktree-create.sh`` symlinks the main
checkout's ``.venv`` into every worktree. A worktree's ``uv sync`` or ``uv run``
then reinstalls the editable project into that one venv, pointing the main
checkout's ``_editable_impl_co_status.pth`` at the worktree; once the worktree
is removed, ``src`` stops resolving for the operator scripts that run from the
main checkout (``rotate_key.py``, ``seed_tenant.py``, ``import_monitors.py``).

The first test holds the file, read with the script's grammar (whole-line ``#``
comments and blank lines dropped, the first line left, trimmed): CI checks out
no submodule, so it can't run the script. The second runs the vendored
``worktree-create.sh`` against a scratch repo carrying this knob, so a submodule
bump that renames the knob or its value fails a local run.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ci.test_dependencies import REPO_ROOT

KNOB = REPO_ROOT / ".skills" / "worktree_venv"
CREATE = REPO_ROOT / "skills" / "using-git-worktrees" / "scripts" / "worktree-create.sh"


def configured(text: str) -> str:
    """The knob's value as ``worktree_venv_mode`` reads it."""
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    return lines[0] if lines else ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("none\n", "none"),
        ("# worktrees get their own venv\n\n  none  \n", "none"),
        ("", ""),
        ("# only a comment\n", ""),
    ],
)
def test_configured_reads_as_the_script_does(text: str, expected: str):
    """Comments and blank lines aren't values; whitespace around one is dropped."""
    assert configured(text) == expected


def test_knob_reads_none():
    """Tracked, so every clone's worktrees provision their own venv."""
    assert configured(KNOB.read_text()) == "none"


def git(cwd: Path, *args: str) -> None:
    """Run git quietly in ``cwd``, failing the test on error."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(not CREATE.is_file(), reason="skills-vendor not checked out (CI)")
def test_worktree_create_links_no_venv_under_this_knob(tmp_path: Path):
    """A primary with a venv and this knob yields a worktree with no ``.venv``, said on stderr."""
    primary = tmp_path / "primary"
    (primary / ".skills").mkdir(parents=True)
    shutil.copyfile(KNOB, primary / ".skills" / "worktree_venv")
    (primary / ".venv" / "bin").mkdir(parents=True)
    (primary / ".venv" / "bin" / "activate").touch()
    (primary / ".gitignore").write_text(".worktrees/\n.venv\n.skills/\n")
    git(primary, "init", "-q", "-b", "main")
    git(primary, "add", ".gitignore")
    git(primary, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    out = subprocess.run(
        ["bash", str(CREATE), "--new", "probe"],
        cwd=primary,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    assert out.returncode == 0, out.stderr
    worktree = Path(out.stdout.strip().splitlines()[-1])
    assert worktree.is_dir(), out.stdout
    assert not (worktree / ".venv").exists()
    assert not (worktree / ".venv").is_symlink()
    assert "worktree_venv=none" in out.stderr, out.stderr
