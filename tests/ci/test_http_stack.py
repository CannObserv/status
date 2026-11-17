"""One HTTP library in the process that runs: httpx2, never httpx (#125).

notifier-client 0.4.0 sends through httpx2 and re-raises httpx2's exceptions.
An ``except httpx.TransportError`` around an SDK call stops matching, and
nothing fails to say so: an unreachable notifier escapes as a crash instead of
``NotifierUnavailable``. So the service moved whole, and these checks keep the
split from coming back.

The suite cannot see the split by itself. ``tests/conftest.py`` aliases
``httpx`` to httpx2 for respx, process-wide, so a stray ``import httpx`` in
``src/`` resolves to httpx2 under test and to real httpx in production. Hence
an AST walk, and a fresh interpreter without the alias.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: What runs against production: the service, its migrations, its scripts.
AREAS = ("src", "alembic", "scripts")
FORBIDDEN = {"httpx", "httpcore"}


def _imports(path: Path) -> set[str]:
    """Top-level modules one file imports, anywhere in its body."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _sources() -> list[Path]:
    return sorted(path for area in AREAS for path in (REPO_ROOT / area).rglob("*.py"))


def test_sources_were_found():
    """A walk over nothing would pass every check below."""
    assert any(path.name == "alerting.py" for path in _sources())


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_nothing_that_runs_imports_httpx(path):
    found = _imports(path) & FORBIDDEN
    assert not found, (
        f"{path.relative_to(REPO_ROOT)} imports {', '.join(sorted(found))}. The SDK "
        f"raises httpx2's exceptions; import httpx2 (and httpcore2) instead."
    )


#: Imports every module under src/ and reports which of httpx/httpcore loaded.
_PROBE = """
import importlib, json, pkgutil, sys
import src
for module in pkgutil.walk_packages(src.__path__, "src."):
    importlib.import_module(module.name)
print(json.dumps(sorted(m for m in sys.modules if m.split(".")[0] in {"httpx", "httpcore"})))
"""


def test_the_service_loads_no_httpx_at_run_time():
    """Transitively too: a dependency importing httpx would split the stack again.

    A fresh interpreter, so this suite's alias is not in it.
    """
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == []
