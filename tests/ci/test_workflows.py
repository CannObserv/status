"""Drift tests for .github/workflows/ — the gates that run unattended.

Every other check in this repo is something a human or agent chose to run.
These workflows are the only ones that fire on their own, so the conditions
that make them meaningful have to be asserted somewhere that fails loudly.

The first thing asserted is that each gate still *does* something: a `test`
job that installs dependencies and stops would satisfy every other check in
this file, and "present in form, doing nothing" is precisely what notifier#27 was
filed about. The rest are conditions a reader cannot see in the YAML:

* **The test database must be named with a ``_test`` suffix.** ``db_safety``
  matches the suffix, not a substring, and ``get_database_url()`` is the
  chokepoint every connection crosses. A service container named ``status``
  fails the whole suite at fixture setup with what reads like a security
  refusal rather than the config typo it is.
* **The migrations job needs its own database.** ``tests/conftest.py`` builds
  schema with ``Base.metadata.create_all``; pointing ``alembic upgrade head``
  at a database pytest has touched fails on "relation already exists".
* **The interpreter must be pinned.** ``[tool.coverage.run] core = "sysmon"``
  falls back silently on CPython < 3.12 and reports ~6 points low against a
  live ``fail_under = 80`` (issue notifier#27).
* **Dependencies must install from the lockfile.** ``coverage`` is pinned
  nowhere in ``pyproject.toml`` but sysmon needs >= 7.10, so an unlocked
  resolve can change what the coverage gate measures.
"""

import shlex
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"

# PyYAML resolves a bare `on:` key to the boolean True (the YAML 1.1 "Norway
# problem"). GitHub Actions means the string. Look up both rather than
# quoting the key in the workflow, which would be unidiomatic there.
ON = True


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def triggers(doc: dict) -> dict:
    return doc.get(ON, doc.get("on"))


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def run_lines(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in steps(job))


def run_tokens(job: dict) -> list[str]:
    """Every run line split into shell words.

    Token-level, not substring: `"-m" in run_lines(job)` also matches
    `--no-modify-path` and would fail a step about something else entirely
    with a message about marker expressions.
    """
    return [token for step in steps(job) for token in shlex.split(step.get("run", ""))]


def postgres(job: dict) -> dict:
    return job["services"]["postgres"]


def service_env(job: dict) -> dict:
    return postgres(job)["env"]


@pytest.fixture(scope="module")
def ci() -> dict:
    return load(CI)


def test_ci_workflow_exists():
    assert CI.is_file(), "the repo's only automated correctness gate"


def test_ci_fires_on_push_to_main(ci):
    """The trigger that matters: this repo commits straight to main.

    A pull_request-only gate is vacuous here — that is the whole of notifier#27.
    """
    assert "main" in triggers(ci)["push"]["branches"]


def test_ci_also_covers_pull_requests_and_manual_runs(ci):
    on = triggers(ci)
    assert "main" in on["pull_request"]["branches"]
    assert "workflow_dispatch" in on


def test_ci_never_cancels_a_run_on_main(ci):
    """Superseding a PR run is free; superseding main's loses that commit's
    only signal, and on this VM main is what is deployed."""
    concurrency = ci["concurrency"]
    assert "github.event_name == 'pull_request'" in str(concurrency["cancel-in-progress"])
    assert "github.head_ref || github.ref" in concurrency["group"]


def test_ci_takes_read_only_permissions(ci):
    assert ci["permissions"] == {"contents": "read"}


def test_ci_runs_lint_test_and_migrations(ci):
    assert {"lint", "test", "migrations"} <= set(ci["jobs"])


def test_lint_names_its_own_failure(ci):
    """Separate steps, so a red run says whether it was style or format."""
    runs = [step["run"] for step in steps(ci["jobs"]["lint"]) if "ruff" in step.get("run", "")]
    assert "uv run ruff check ." in runs
    assert "uv run ruff format --check ." in runs


def test_test_job_database_carries_the_non_production_suffix(ci):
    """`db_safety` matches the suffix; any other name fails all 240 tests."""
    name = service_env(ci["jobs"]["test"])["POSTGRES_DB"]
    assert name.endswith("_test"), (
        f"{name!r} reads as production to src/core/db_safety.py, which every "
        f"connection path crosses"
    )


def test_test_job_passes_that_database_as_test_database_url(ci):
    job = ci["jobs"]["test"]
    assert service_env(job)["POSTGRES_DB"] in job["env"]["TEST_DATABASE_URL"]


def test_test_job_actually_runs_the_suite(ci):
    """The absence checks below say what the step must not carry. This one
    says the step exists at all.

    Without it the whole file is satisfiable by a `test` job that installs
    dependencies and stops — present in form, doing nothing, which is the
    exact failure notifier#27 was filed about.
    """
    assert "uv run pytest" in run_lines(ci["jobs"]["test"])


def test_test_job_does_not_restate_the_marker_expression(ci):
    """`-m 'not integration'` is already in addopts. A second spelling on the
    command line overrides rather than composes, and can drift."""
    assert "-m" not in run_tokens(ci["jobs"]["test"])


def test_migrations_job_uses_a_database_pytest_never_touches(ci):
    """conftest's create_all would leave tables behind and break upgrade head."""
    test_db = service_env(ci["jobs"]["test"])["POSTGRES_DB"]
    migrations_db = service_env(ci["jobs"]["migrations"])["POSTGRES_DB"]
    assert migrations_db != test_db


def test_migrations_job_builds_from_scratch_and_checks_for_drift(ci):
    """The one gate the suite structurally cannot be: conftest builds schema
    with create_all, so a broken migration chain surfaces only on deploy."""
    runs = run_lines(ci["jobs"]["migrations"])
    assert "alembic upgrade head" in runs
    assert "alembic check" in runs


def test_no_job_carries_the_production_opt_in(ci):
    """alembic is exempt from the guard by design (src/core/db_safety.py), so
    the migrations job needs no escape hatch — and nothing else may grow one.

    Checked against the parsed workflow, not the raw text: ci.yml explains in
    a comment why the flag is absent, and matching raw text would make that
    comment fail the test it documents.
    """
    from src.core import db_safety

    flag = db_safety.ALLOW_PROD_ENV_VAR
    assert flag not in ci.get("env", {})
    for name, job in ci["jobs"].items():
        assert flag not in job.get("env", {}), name
        for step in steps(job):
            assert flag not in step.get("env", {}), name
            assert flag not in step.get("run", ""), name


@pytest.mark.parametrize("workflow", [CI], ids=["ci"])
def test_every_workflow_pins_the_interpreter(workflow):
    """sysmon falls back silently below 3.12 and reports ~6 points low."""
    doc = load(workflow)
    for name, job in doc["jobs"].items():
        pinned = [
            step
            for step in steps(job)
            if "setup-uv" in step.get("uses", "") and step.get("with", {}).get("python-version")
        ]
        assert pinned, f"{workflow.name}:{name} does not pin python-version"


@pytest.mark.parametrize("workflow", [CI], ids=["ci"])
def test_every_job_installs_from_the_lockfile(workflow):
    """`coverage` is pinned nowhere in pyproject but sysmon needs >= 7.10, so
    an unlocked resolve can change what the coverage gate measures.

    Walks parsed steps rather than grepping the file: raw text waves through
    `uv sync --no-dev`, misses a trailing `uv sync` with no newline after it,
    and cannot tell that *every* job installs this way.
    """
    for name, job in load(workflow)["jobs"].items():
        syncs = [
            shlex.split(step["run"])
            for step in steps(job)
            if step.get("run", "").startswith("uv sync")
        ]
        assert syncs, f"{workflow.name}:{name} installs nothing"
        for tokens in syncs:
            assert "--locked" in tokens, f"{workflow.name}:{name} runs {' '.join(tokens)}"


@pytest.mark.parametrize("workflow", [CI], ids=["ci"])
def test_every_job_bounds_its_own_runtime(workflow):
    """GitHub's default is 360 minutes. A wedged `uv sync` or a postgres that
    never comes up would burn six hours before anyone noticed; these runs
    finish in under a minute."""
    for name, job in load(workflow)["jobs"].items():
        assert job.get("timeout-minutes"), f"{workflow.name}:{name} has no timeout"


@pytest.mark.parametrize("job_name", ["test", "migrations"], ids=["test", "migrations"])
def test_database_services_are_waited_for(ci, job_name):
    """Without a health check the job races container startup, and the failure
    surfaces as an intermittent red — the hardest kind to attribute."""
    assert "pg_isready" in postgres(ci["jobs"][job_name])["options"]
