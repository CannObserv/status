"""Tests for ``.skills/next-steps``: shipping-work's Step 8 rows for this repo (#100).

Its rows replace ``references/stack-fastapi.md``'s table (alembic upgrade, then
``systemctl restart``), none of which is how co-status ships. The skill's
``read-next-steps.sh`` reads the knob from the primary checkout, so from a
branch it answers for ``main``'s copy: these tests hold the file itself, parsed
with the reader's grammar (whole-line ``#`` comments and blank lines dropped,
whitespace trimmed). The reader is a submodule CI never checks out.

The rows that name a definition in the code point at it rather than copying
it, so they can't drift from it: what deploys is ``src/core/drift.py``'s
``counts()``, what is a host config is ``scripts/deploy.sh``'s ``HOST_CONFIGS``.
These tests fail if either name goes, or if a credential path the rows name
is no longer one a unit loads.
"""

import re

from src.core import drift
from tests.ci.test_dependencies import REPO_ROOT

KNOB = REPO_ROOT / ".skills" / "next-steps"
DEPLOY = REPO_ROOT / "scripts" / "deploy.sh"


def rows() -> list[str]:
    """The knob's rows, as ``read-next-steps.sh`` parses them."""
    parsed = []
    # Newlines only, as `read -r` splits: read_text() turns \r into \n, splitlines() breaks on \f.
    for line in KNOB.read_bytes().decode().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            parsed.append(line)
    return parsed


def expand(path: str) -> list[str]:
    """Bash brace expansion of one ``{a,b}`` group, as the rows write key pairs."""
    match = re.search(r"\{([^{}]*)\}", path)
    if not match:
        return [path]
    head, tail = path[: match.start()], path[match.end() :]
    return [head + alt + tail for alt in match.group(1).split(",")]


def test_is_a_regular_file_with_rows():
    """The reader's exit 2: a link, a non-file, or no row after comments."""
    assert KNOB.is_file() and not KNOB.is_symlink()
    assert rows()


def test_code_ships_through_deploy_sh():
    """The row that replaces the stack file's migrate-then-restart."""
    assert any("`scripts/deploy.sh`" in row and "Never a hand-run" in row for row in rows())
    assert any("scripts/deploy.sh --dev origin/" in row for row in rows())


def test_nothing_to_deploy_is_drift_counts():
    """The row defers to ``counts()``, which exists, rather than listing paths."""
    assert callable(drift.counts)
    row = next(row for row in rows() if "nothing to deploy" in row)
    assert "`src/core/drift.py`" in row and "`counts()`" in row
    for ignored in (*drift._IGNORED_DIRS, *drift._IGNORED_FILES, ".md"):
        assert ignored not in row, f"{ignored} is counts()'s to say"


def test_host_configs_are_deploy_sh_list():
    """The host-config row names the list ``deploy.sh`` compares, which exists."""
    assert re.search(r"^HOST_CONFIGS=\(", DEPLOY.read_text(), re.MULTILINE)
    assert any("`HOST_CONFIGS`" in row for row in rows())


def test_named_credentials_are_loaded_by_a_unit():
    """Each ``/etc/status/*.key`` a row names is one a unit's ``LoadCredential=`` reads."""
    units = "\n".join(path.read_text() for path in (REPO_ROOT / "deploy").glob("*.service"))
    loaded = set(re.findall(r"^LoadCredential=[^:]+:(\S+)$", units, re.MULTILINE))
    named = [
        path
        for row in rows()
        for token in re.findall(r"/etc/status/[^\s`]+\.key", row)
        for path in expand(token)
    ]
    assert named
    assert set(named) <= loaded, set(named) - loaded
