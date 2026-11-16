"""Tests for ``.skills/import-targets``: what pre-ship's import check imports (#109).

Without the knob, ``shipping-work``'s ``pre-ship.sh`` guesses a package from the
``[project]`` name (``co_status``), finds none, and skips the check. The knob
names what to import instead, one module per line, parsed by
``detect-import-targets.sh`` (whole-line ``#`` comments and blank lines dropped,
whitespace trimmed). The reader is a submodule CI never checks out, so these
tests hold the file itself.

The rule for what it names: every module a unit's launcher hands to Python,
``uvicorn <module>:app``, ``python -m <module>`` and ``python scripts/<x>.py``
(as ``scripts.<x>``). A script, not only the ``src`` module behind it, because a
script's own imports can break while every ``src`` module still imports.

Each target is imported in a fresh interpreter with a scrubbed environment: an
in-process import would pass on any module the suite had already imported,
and an inherited ``DATABASE_URL`` would hide an import-time dependency on it.
"""

import re
import subprocess
import sys

from tests.ci.test_dependencies import REPO_ROOT

KNOB = REPO_ROOT / ".skills" / "import-targets"
UNITS = REPO_ROOT / "deploy"

LAUNCHER = re.compile(r"^ExecStart=\S*/(scripts/[\w-]+\.sh)\s*$", re.MULTILINE)
INVOCATION = re.compile(r"\b(?:python|uvicorn)\b")
PATTERNS = (
    re.compile(r"\buvicorn\s+([\w.]+):\w+"),
    re.compile(r"\bpython\s+-m\s+([\w.]+)"),
    re.compile(r"\bpython\s+(scripts/\w+)\.py\b"),
)


def targets() -> list[str]:
    """The knob's targets, as ``detect-import-targets.sh`` parses them."""
    parsed = []
    # Newlines only, as `read -r` splits: read_text() turns \r into \n, splitlines() breaks on \f.
    for line in KNOB.read_bytes().decode().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            parsed.append(line)
    return parsed


def launchers() -> set[str]:
    """Each ``scripts/*.sh`` a unit under ``deploy/`` starts."""
    found = set()
    for unit in sorted(UNITS.glob("*.service")):
        found.update(LAUNCHER.findall(unit.read_text()))
    return found


def launched_modules(launcher: str) -> set[str]:
    """The modules one launcher hands to Python, from its non-comment lines."""
    modules = set()
    for line in (REPO_ROOT / launcher).read_text().splitlines():
        if line.lstrip().startswith("#") or not INVOCATION.search(line):
            continue
        hits = [m.group(1) for p in PATTERNS for m in [p.search(line)] if m]
        assert hits, f"{launcher}: a Python invocation this test can't read: {line.strip()}"
        modules.update(hit.replace("/", ".") for hit in hits)
    return modules


def test_is_a_regular_file_with_targets():
    """No file, or none after comments, and pre-ship falls back to its guess and skips."""
    assert KNOB.is_file() and not KNOB.is_symlink()
    assert targets()


def test_names_every_module_a_unit_launches_and_nothing_else():
    """A new entry point can't skip the check, and a retired one can't linger."""
    found = launchers()
    assert {"scripts/serve.sh", "scripts/sweep.sh", "scripts/drift.sh"} <= found
    expected = set().union(*(launched_modules(launcher) for launcher in found))
    assert sorted(targets()) == sorted(expected)


def test_each_target_imports_in_a_fresh_scrubbed_interpreter():
    """What pre-ship runs, ``python -c "import <target>"`` from the root, without the env."""
    failures = {}
    for target in targets():
        result = subprocess.run(
            [sys.executable, "-c", f"import {target}"],
            cwd=REPO_ROOT,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            failures[target] = result.stderr.strip().splitlines()[-1:]
    assert not failures
