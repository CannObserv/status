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
from pathlib import Path

from tests.ci.test_dependencies import REPO_ROOT

KNOB = REPO_ROOT / ".skills" / "import-targets"
UNITS = REPO_ROOT / "deploy"

EXEC = re.compile(r"^Exec\w*=(.*)$", re.MULTILINE)  # ExecStart=, ExecStartPre=, …
LAUNCHER = re.compile(r"/(scripts/[\w-]+\.sh)\b")
HELPER = re.compile(r"\b([\w-]+\.sh)\b")  # by name: launchers run helpers by their own dirname
PYTHON = r"python[\d.]*"  # python, python3, python3.12
INVOCATION = re.compile(rf"\b(?:{PYTHON}|uvicorn)\b")
PATTERNS = (
    re.compile(r"\buvicorn\s+([\w.]+):\w+"),
    re.compile(rf"\b{PYTHON}\s+-m\s+([\w.]+)"),
    re.compile(rf"\b{PYTHON}\s+(scripts/\w+)\.py\b"),
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


def modules_in(line: str) -> set[str]:
    """The modules one shell line hands to Python; fails on an invocation it can't read."""
    if not INVOCATION.search(line):
        return set()
    hits = {m.group(1).replace("/", ".") for p in PATTERNS for m in p.finditer(line)}
    assert hits, f"a Python invocation this test can't read: {line.strip()}"
    return hits


def script_lines(path: Path) -> list[str]:
    """A shell script's lines, comments dropped."""
    return [line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")]


def launched(units: list[str], root: Path = REPO_ROOT) -> tuple[set[str], set[str]]:
    """The scripts these unit files start, and every module those hand to Python."""
    found, modules = set(), set()
    for unit in units:
        for command in EXEC.findall(unit):
            found.update(LAUNCHER.findall(command))
            modules.update(modules_in(command))
    pending = sorted(found)
    while pending:
        for line in script_lines(root / pending.pop()):
            modules.update(modules_in(line))
            for name in HELPER.findall(line):
                helper = f"scripts/{name}"
                if helper not in found and (root / helper).is_file():
                    found.add(helper)
                    pending.append(helper)
    return found, modules


def unit_files() -> list[str]:
    """Every unit under ``deploy/`` that runs something."""
    return [unit.read_text() for unit in sorted(UNITS.glob("*.service"))]


def test_is_a_regular_file_with_targets():
    """No file, or none after comments, and pre-ship falls back to its guess and skips."""
    assert KNOB.is_file() and not KNOB.is_symlink()
    assert targets()


def test_names_every_module_a_unit_launches_and_nothing_else():
    """A new entry point can't skip the check, and a retired one can't linger."""
    found, expected = launched(unit_files())
    assert {"scripts/serve.sh", "scripts/sweep.sh", "scripts/drift.sh"} <= found
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


def test_reads_every_invocation_on_a_line():
    """Two on one line, ``&&``-chained, are two modules, not the first alone."""
    line = "uv run python -m src.a && exec uv run python -m src.b"
    assert modules_in(line) == {"src.a", "src.b"}


def test_reads_a_versioned_interpreter():
    """``python3`` and ``python3.12`` are Python too, not a line to pass over."""
    assert modules_in("exec uv run python3.12 -m src.a") == {"src.a"}
    assert modules_in("uv run python3 scripts/b.py") == {"scripts.b"}


def test_reads_every_exec_line_of_a_unit():
    """Arguments after a launcher, and Python a unit runs itself, ``ExecStartPre=`` too."""
    unit = (
        "[Service]\n"
        "ExecStartPre=/srv/status/live/.venv/bin/python -m src.a\n"
        "ExecStart=/srv/status/live/scripts/drift.sh --once\n"
    )
    found, modules = launched([unit])
    assert found == {"scripts/drift.sh"}
    assert modules == {"src.a", "scripts.check_drift"}


def test_follows_the_scripts_a_launcher_runs(tmp_path):
    """``serve.sh`` runs ``tailnet_bind.sh``: Python a helper starts is a unit's too."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "serve.sh").write_text('HOST="$("$(dirname "$0")/bind.sh")"\nexec uv run x\n')
    (scripts / "bind.sh").write_text("# python -m src.comment\nuv run python -m src.b\n")
    unit = "ExecStart=/srv/status/live/scripts/serve.sh\n"
    found, modules = launched([unit], root=tmp_path)
    assert found == {"scripts/serve.sh", "scripts/bind.sh"}
    assert modules == {"src.b"}
