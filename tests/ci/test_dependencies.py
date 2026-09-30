"""Drift tests for the repo's dependency tables.

Invariants that `uv sync --locked` cannot express, from notifier#30 and its review.

* **Every specifier carries an upper bound.** A resolver takes what the
  specifier permits: `cryptography>=46.0.6` let 47.0.0 into the lock with no
  diff and no review. Build-time breaks are cheap; the expensive majors are
  the ones that move behavior on the dispatch path (`apprise`'s URL grammar,
  Fernet's backing API) where a green suite is not evidence.
* **A `0.x` dependency is capped inside its `0.x` line.** `<1` is a cap in
  form only: 0.x projects ship breaking changes in *minor* bumps, so
  `referencing>=0.37.0,<1` still permits an unreviewed 0.38. The cap belongs
  at the next minor above what the lock resolves (`<0.38`).
* **Every declared runtime dependency has an importer under `src/`.** The
  lock is indifferent to a dependency nothing uses — which is how
  `procrastinate` sat unimported for months (notifier#29). The exceptions are real
  but few, so `IMPORT_LESS` states each one once, with a reason that has to
  be non-empty; a stale entry there fails as loudly as a missing one.
* **Every third-party module this repo imports is declared.** The mirror of
  the above, and the direction this repo was already in: `pydantic` backed
  every model in `src/api/schemas/` while riding in transitively through
  fastapi, so the version floor for code we wrote was a side effect of
  someone else's packaging (notifier#32). This check walks the AST rather than
  matching text — exhaustive is the whole point in this direction — and
  covers `alembic/` and `scripts/` alongside `src/`, because those run
  against production too and fail at run time rather than at test time.

The bound checks cover the one dependency table in this repo, runtime and dev
groups alike. The importer check covers `[project.dependencies]` only — dev
tools are run as commands or imported from `tests/`, so "unimported by
`src/`" is their normal condition and says nothing. It also
looks at `src/` alone: a dependency reached only from `scripts/` is a tool,
not something the service needs at run time.
"""

import ast
import re
import sys
import tomllib
from functools import cache
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

# Directories whose imports must be declared. `src/` is the service; the other
# two run against production — `alembic/env.py` under `alembic upgrade head`,
# `scripts/` by hand and from the systemd units — where an undeclared import
# surfaces at run time rather than in the suite.
IMPORT_AREAS = ("src", "alembic", "scripts")

# Every dependency table in the repo. One today; a second (a client package,
# say) belongs here the day it exists.
TABLES = {
    "service": REPO_ROOT / "pyproject.toml",
}

# Operators that put a ceiling on what a resolver may take. `~=` and `==`
# bound from above as surely as `<` does, so a pin is not a violation.
BOUNDING_OPERATORS = {"<", "<=", "==", "===", "~="}
CEILING_OPERATORS = {"<", "<="}
FLOOR_OPERATORS = {">=", ">", "==", "==="}

# Runtime dependencies that nothing under src/ imports, and why each is
# nonetheless required at runtime. Reached by name — a command line, a URL
# scheme, a framework's transport — rather than by `import`. Keys are
# canonicalized on the way in so an entry written `PyYAML` or
# `python_json_logger` still matches the name pyproject.toml declares.
IMPORT_LESS = {
    canonicalize_name(name): reason
    for name, reason in {
        "uvicorn": (
            "ASGI server, invoked as a command by scripts/dev_server.sh and "
            "the systemd units in deploy/"
        ),
        "asyncpg": (
            "SQLAlchemy's async driver, selected by the postgresql+asyncpg:// "
            "URL scheme rather than by import"
        ),
    }.items()
}


# Third-party modules src/ may import without the service declaring them, and
# why. Expected to stay empty: relying on a transitive dependency means the
# floor for our own code is set by whoever pulls it in. An entry here is a
# deliberate exception, not a shortcut past declaring the dependency.
UNDECLARED_OK: dict[str, str] = {}


@cache
def table(path: Path) -> dict:
    """Parsed contents of one pyproject.toml."""
    with path.open("rb") as handle:
        return tomllib.load(handle)


def declared(path: Path) -> list[Requirement]:
    """Every requirement a table declares, in any of its three sections.

    `optional-dependencies` is read even though neither table has extras
    today: an enforcement test that passes vacuously on the section someone
    adds next is not enforcement.
    """
    parsed = table(path)
    project = parsed.get("project", {})
    lines = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        lines.extend(extra)
    for group in parsed.get("dependency-groups", {}).values():
        lines.extend(group)
    return [Requirement(line) for line in lines]


def runtime_requirements() -> list[Requirement]:
    """The service's runtime dependencies — what a consumer installing it gets."""
    return [Requirement(line) for line in table(TABLES["service"])["project"]["dependencies"]]


def versions_for(requirement: Requirement, operators: set[str]) -> list[Version]:
    """Parsed versions of a requirement's specifiers using the given operators.

    Wildcards (`==1.4.*`) and unparseable arbitrary-equality strings are
    skipped rather than raised on; neither carries version ordering worth
    asserting against.
    """
    parsed = []
    for spec in requirement.specifier:
        if spec.operator not in operators:
            continue
        try:
            parsed.append(Version(spec.version.rstrip(".*")))
        except InvalidVersion:
            continue
    return parsed


def is_zero_version(requirement: Requirement) -> bool:
    """Whether the requirement's floor sits on a 0.x release line."""
    floors = versions_for(requirement, FLOOR_OPERATORS)
    return bool(floors) and min(floors).major == 0


def every_declared_requirement() -> list[tuple[str, Requirement]]:
    return [(label, req) for label, path in TABLES.items() for req in declared(path)]


def zero_version_requirements() -> list[tuple[str, Requirement]]:
    return [(label, req) for label, req in every_declared_requirement() if is_zero_version(req)]


def label_of(case: tuple[str, Requirement]) -> str:
    return f"{case[0]}:{case[1]}"


@cache
def module_names(name: str) -> frozenset[str]:
    """Top-level modules a distribution installs.

    Read from installed metadata, so `python-json-logger` resolves to
    `pythonjsonlogger` without a hand-kept name map. Falls back to the
    distribution name for metadata that describes no top-level module.
    """
    canonical = canonicalize_name(name)
    modules = {
        module
        for module, dists in packages_distributions().items()
        if any(canonicalize_name(dist) == canonical for dist in dists)
    }
    return frozenset(modules or {name.replace("-", "_")})


@cache
def python_sources(area: str) -> tuple[tuple[str, str], ...]:
    """Every module under one tracked directory, read once and reused."""
    return tuple(
        (str(path.relative_to(REPO_ROOT)), path.read_text())
        for path in sorted((REPO_ROOT / area).rglob("*.py"))
    )


def src_sources() -> tuple[tuple[str, str], ...]:
    """Every module under src/ — what the importer check reads."""
    return python_sources("src")


def is_installed(name: str) -> bool:
    """Whether the distribution is present in the environment running pytest."""
    try:
        distribution(name)
    except PackageNotFoundError:
        return False
    return True


def importers(name: str) -> list[str]:
    """Paths under src/ importing any module the distribution installs.

    Textual, not an AST walk: a line whose first non-whitespace text is
    `import X` counts, so an import quoted inside a docstring would too.
    Commented-out imports do not match. The permissive direction is a
    dependency justified by prose, which is cheap to spot in review; the
    strict direction — missing a real import — is what would matter, and a
    line-start match cannot.
    """
    alternatives = "|".join(re.escape(module) for module in sorted(module_names(name)))
    pattern = re.compile(rf"^\s*(?:from|import)\s+(?:{alternatives})\b", re.MULTILINE)
    return [path for path, source in src_sources() if pattern.search(source)]


@pytest.mark.parametrize("case", every_declared_requirement(), ids=label_of)
def test_every_dependency_carries_an_upper_bound(case):
    """A floor alone delegates the next major to whoever runs the resolver."""
    label, requirement = case
    assert any(spec.operator in BOUNDING_OPERATORS for spec in requirement.specifier), (
        f"{label}: {requirement} may resolve to any future major. Add a cap "
        f"(house style is <next-major) so crossing one takes a reviewed diff."
    )


@pytest.mark.parametrize("case", zero_version_requirements(), ids=label_of)
def test_zero_version_dependency_is_capped_inside_its_zero_line(case):
    """`<1` on a 0.x dependency is a cap in form only.

    Pre-1.0 projects put breaking changes in minor bumps, so the boundary a
    review should gate is 0.N → 0.N+1, not 0.x → 1.0.
    """
    label, requirement = case
    ceilings = versions_for(requirement, CEILING_OPERATORS)
    assert ceilings and all(ceiling.major == 0 for ceiling in ceilings), (
        f"{label}: {requirement} is a 0.x dependency capped outside its 0.x "
        f"line, which permits an unreviewed 0.N+1 — where pre-1.0 projects "
        f"put their breaking changes. Cap at the next minor above the locked "
        f"version instead."
    )


@pytest.mark.parametrize(
    "requirement",
    [req for req in runtime_requirements() if canonicalize_name(req.name) not in IMPORT_LESS],
    ids=str,
)
def test_every_runtime_dependency_has_an_importer(requirement):
    """Declared and unimported is how procrastinate survived to notifier#29."""
    assert is_installed(requirement.name), (
        f"{requirement.name} is declared but not installed here, so this "
        f"check cannot tell an unused dependency from a stale environment. "
        f"Run `uv sync` and re-run."
    )
    assert importers(requirement.name), (
        f"nothing under src/ imports {requirement.name}. Drop it, or add it "
        f"to IMPORT_LESS in this file with the reason it is needed anyway."
    )


@pytest.mark.parametrize("name", sorted(IMPORT_LESS), ids=str)
def test_import_less_allowlist_names_a_declared_dependency(name):
    """An exception outliving its dependency is a claim nobody rechecks."""
    names = {canonicalize_name(req.name) for req in runtime_requirements()}
    assert name in names, (
        f"IMPORT_LESS excuses {name}, which pyproject.toml no longer declares; remove the entry."
    )


@pytest.mark.parametrize("name", sorted(IMPORT_LESS), ids=str)
def test_import_less_allowlist_states_a_reason(name):
    """The stated reason is the artifact; an entry without one is an assumption."""
    assert IMPORT_LESS[name].strip(), (
        f"IMPORT_LESS[{name!r}] states no reason. The point of the allowlist "
        f"is that each exception is written down once rather than assumed."
    )


@pytest.mark.parametrize("name", sorted(IMPORT_LESS), ids=str)
def test_import_less_allowlist_has_no_entry_that_is_imported(name):
    """Once src/ imports it, the ordinary check covers it — and an allowlist
    entry would then hide a later removal."""
    found = importers(name)
    assert not found, (
        f"src/ now imports {name} ({', '.join(found)}); remove its "
        f"IMPORT_LESS entry so the importer check applies."
    )


@cache
def imported_modules() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Third-party top-level modules imported anywhere in IMPORT_AREAS.

    An AST walk, unlike the textual match `importers()` uses: this direction
    has to be exhaustive, and a regex that misses an import silently reports
    the invariant as held. It also sees imports inside a function body, which
    the house style forbids and therefore does not look for. Standard-library
    and first-party modules are dropped, as are relative imports, which have
    no distribution to declare.

    Each module carries the paths that import it, so a failure names where to
    look rather than only what is missing.
    """
    sites: dict[str, set[str]] = {}
    for area in IMPORT_AREAS:
        for path, source in python_sources(area):
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    found = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found = {node.module.split(".")[0]}
                else:
                    continue
                for module in found:
                    sites.setdefault(module, set()).add(path)
    return tuple(
        (module, tuple(sorted(paths)))
        for module, paths in sorted(sites.items())
        if module not in sys.stdlib_module_names and module != SRC.name
    )


def declaring_distributions(module: str) -> list[str]:
    """Canonical names of the distributions that install a top-level module."""
    return [canonicalize_name(dist) for dist in packages_distributions().get(module, [])]


@pytest.mark.parametrize(
    "case",
    [case for case in imported_modules() if case[0] not in UNDECLARED_OK],
    ids=lambda case: case[0],
)
def test_every_imported_module_is_declared(case):
    """An undeclared import takes its floor from whoever pulls it in.

    Checked against the runtime table alone: these directories are what runs
    against production, so an import satisfied only by a dev-group entry is a
    packaging bug rather than a passing case.
    """
    module, paths = case
    declared_names = {canonicalize_name(req.name) for req in runtime_requirements()}
    providers = declaring_distributions(module)
    assert providers, (
        f"{module} is imported ({', '.join(paths)}) but no installed "
        f"distribution claims it. Run `uv sync`, or drop the import if it is dead."
    )
    assert any(name in declared_names for name in providers), (
        f"{module} is imported ({', '.join(paths)}) from {', '.join(providers)}, "
        f"which pyproject.toml does not declare — it arrives transitively, so "
        f"its version floor is set by whatever pulls it in. Declare it, or add "
        f"it to UNDECLARED_OK with the reason."
    )


@pytest.mark.parametrize("module", sorted(UNDECLARED_OK), ids=str)
def test_undeclared_ok_allowlist_states_a_reason(module):
    """Same rule as IMPORT_LESS: the reason is the artifact, not the key."""
    assert UNDECLARED_OK[module].strip(), (
        f"UNDECLARED_OK[{module!r}] states no reason. Relying on a transitive "
        f"dependency is a decision; it has to be written down once."
    )


@pytest.mark.parametrize("module", sorted(UNDECLARED_OK), ids=str)
def test_undeclared_ok_allowlist_has_no_stale_entry(module):
    """An exception outliving the import it excused is a claim nobody rechecks."""
    imported = {name for name, _ in imported_modules()}
    assert module in imported, (
        f"UNDECLARED_OK excuses {module}, which nothing imports any more; remove the entry."
    )
