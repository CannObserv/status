"""Tests for src/core/build.py — the release's own record of its commit (#9, R10).

``scripts/deploy.sh`` writes ``REVISION`` last into every release, so the code
that ran reports itself: the sweep, which never had a stamp, included. A
checkout has no ``REVISION`` and reports ``dev``.
"""

import subprocess

import pytest

from src.core import build


def test_reads_the_release_revision(tmp_path):
    (tmp_path / "REVISION").write_text("0123456789ab\n")
    assert build.build_id(tmp_path) == "0123456789ab"


def test_a_checkout_without_one_is_dev(tmp_path):
    assert build.build_id(tmp_path) == "dev"


@pytest.mark.parametrize("value", ["", "  \n"], ids=["empty", "whitespace"])
def test_a_blank_revision_is_dev_not_blank(tmp_path, value):
    """``{"build": ""}`` reads as a broken endpoint, not an unstamped one."""
    (tmp_path / "REVISION").write_text(value)
    assert build.build_id(tmp_path) == "dev"


def test_defaults_to_the_code_root():
    """The root is the directory holding ``src/``: a release's, or a checkout's."""
    assert build.CODE_ROOT == build.CODE_ROOT.resolve()
    assert (build.CODE_ROOT / "src" / "core" / "build.py").is_file()


def test_a_revision_in_a_checkout_is_ignored_by_git():
    """CR 35: only deploy.sh writes one, into a release; a stray one in a checkout
    would report a false build and must never be committed."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "REVISION"], cwd=build.CODE_ROOT, check=False
    )
    assert result.returncode == 0
