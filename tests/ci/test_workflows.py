"""Drift tests for .github/workflows/ — the gates that run unattended.

Every other check in this repo is something a human or agent chose to run.
These workflows are the only ones that fire on their own, so the conditions
that make them meaningful have to be asserted somewhere that fails loudly.

The first thing asserted is that each gate still *does* something: a `test`
job that installs dependencies and stops would satisfy every other check in
this file, and "present in form, doing nothing" is precisely what notifier#27 was
filed about. The rest are conditions a reader cannot see in the YAML:

* **Both databases must be named with a ``_test`` suffix.** ``db_safety``
  matches the suffix, not a substring, and every connection crosses it,
  ``alembic/env.py``'s included (#15). A service container named ``status``
  fails the whole suite at fixture setup, or the migrations job at
  ``upgrade head``, with what reads like a security refusal rather than the
  config typo it is.
* **The migrations job needs its own database.** ``tests/conftest.py`` builds
  schema with ``Base.metadata.create_all``; pointing ``alembic upgrade head``
  at a database pytest has touched fails on "relation already exists".
* **The interpreter must be pinned.** ``[tool.coverage.run] core = "sysmon"``
  falls back silently on CPython < 3.12 and reports ~6 points low against a
  live ``fail_under = 80`` (issue notifier#27).
* **Dependencies must install from the lockfile.** ``coverage`` is pinned
  nowhere in ``pyproject.toml`` but sysmon needs >= 7.10, so an unlocked
  resolve can change what the coverage gate measures.
* **Only one step may skip, and only on a pull request** (#133). shipping-work's
  ``push.sh`` posts ``gate/local`` after the local gate passed on the head it
  pushed, and ``test`` then skips ``Run tests``. A bot's or a fork's PR never
  skips, and nor does a push to ``main``: that run is the one ``deploy.sh``
  reads (#11). ``lint`` and ``migrations`` never skip; the local gate runs no
  alembic.
"""

import os
import shlex
import subprocess
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
    """The trigger that matters: main's push run is the only one deploy.sh
    reads (#11), and the only run a merge made on GitHub gets.

    A pull_request-only gate would leave what is deployed untested — the
    shape of notifier#27.
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
    """Read-only, and only what is read: the checkout, and the commit statuses
    the attest step asks about (#133). No job widens it."""
    grants = ci["permissions"]
    assert set(grants) == {"contents", "statuses"}
    assert set(grants.values()) == {"read"}
    for name, job in ci["jobs"].items():
        assert "permissions" not in job, name


def test_every_run_block_splits_into_shell_words(ci):
    """tests/ci reads `run:` as shell words. A comment inside one is a shell
    comment shlex can't skip, so an apostrophe in it (`context's`) is an
    unbalanced quote that errors every test touching that job. Comments go in
    YAML, above the step (#133; watcher's finding on gregoryfoster/skills#688).
    """
    for name, job in ci["jobs"].items():
        for step in steps(job):
            run = step.get("run", "")
            shlex.split(run)
            assert not any(line.lstrip().startswith("#") for line in run.splitlines()), (
                f"{name}: {step.get('name', step.get('id'))} carries a comment in run:"
            )


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


def test_migrations_job_database_carries_the_non_production_suffix(ci):
    """alembic crosses `db_safety` too (#15), and the job has no opt-in."""
    job = ci["jobs"]["migrations"]
    name = service_env(job)["POSTGRES_DB"]
    assert name.endswith("_test"), f"{name!r} reads as production to alembic/env.py"
    assert job["env"]["DATABASE_URL"].endswith(f"/{name}")


def test_migrations_job_builds_from_scratch_and_checks_for_drift(ci):
    """The one gate the suite structurally cannot be: conftest builds schema
    with create_all, so a broken migration chain surfaces only on deploy."""
    runs = run_lines(ci["jobs"]["migrations"])
    assert "alembic upgrade head" in runs
    assert "alembic check" in runs


def test_no_job_carries_the_production_opt_in(ci):
    """The opt-in belongs to the live units, deploy.sh's commands against
    live, and deliberate hand-run operations. CI opens only ``_test``
    databases, alembic included, so no job needs it — and none may grow one.

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


# The local-gate attestation (#133, gregoryfoster/skills#688).

#: The heavy tier's `if:`, term by term, as shipping-work's reference gives it.
SKIP_TERMS = (
    "steps.attest.outputs.attested != 'yes'",
    "(github.event_name != 'push' && github.event_name != 'pull_request')",
    "startsWith(github.ref, 'refs/tags/')",
    "github.event_name == 'pull_request'",
    "github.event.pull_request.user.type == 'Bot'",
    "github.event.pull_request.head.repo.full_name != github.repository",
)

#: The shell Actions runs a bash `run:` with.
ACTIONS_BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]


def only_step(job: dict, **match) -> dict:
    found = [s for s in steps(job) if all(s.get(k) == v for k, v in match.items())]
    assert len(found) == 1, f"{match}: {len(found)} steps"
    return found[0]


def attest_step(ci: dict) -> dict:
    return only_step(ci["jobs"]["test"], id="attest")


def run_tests_step(ci: dict) -> dict:
    return only_step(ci["jobs"]["test"], name="Run tests")


def test_attest_step_reads_gate_local_on_the_pr_head(ci):
    """On pull_request, github.sha is the merge commit, which nobody gated;
    the combined status keeps each context's latest state only."""
    step = attest_step(ci)
    assert step["env"]["HEAD"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    run = step["run"]
    assert "commits/$HEAD/status?per_page=100" in run
    assert '.context == "gate/local" and .state == "success"' in run
    assert "$GITHUB_OUTPUT" in run


def test_attest_step_reads_only_on_pull_requests(ci):
    """main's push run is a merge GitHub made, with no status, and the run
    deploy.sh reads (#11): reading there could skip nothing, and a refused read
    would fail main's `test`. Unset reads as not attested."""
    assert attest_step(ci)["if"] == "github.event_name == 'pull_request'"


def test_attest_step_runs_before_the_suite(ci):
    job = steps(ci["jobs"]["test"])
    assert job.index(attest_step(ci)) < job.index(run_tests_step(ci))


def test_the_suite_skips_only_when_attested_on_a_humans_pr(ci):
    """Every term of the reference's `if:`: an attested push or pull request,
    never a tag, a bot's PR (Dependabot) or a fork's."""
    condition = " ".join(run_tests_step(ci)["if"].split())
    for term in SKIP_TERMS:
        assert term in condition, term
    assert run_tests_step(ci)["run"] == "uv run pytest"


def test_nothing_else_can_skip(ci):
    """Step-level: a skipped step leaves `test` success. lint and migrations
    never skip (the local gate runs no alembic), and no job carries an `if:`."""
    for name, job in ci["jobs"].items():
        assert "if" not in job, name
        conditional = {s.get("id") or s.get("name") for s in steps(job) if "if" in s}
        assert conditional == ({"attest", "Run tests"} if name == "test" else set()), name


@pytest.mark.parametrize(
    ("answer", "attested"), [("1", "yes"), ("2", "yes"), ("0", "no")], ids=["one", "two", "none"]
)
def test_attest_step_writes_and_logs_its_verdict(ci, tmp_path, answer, attested):
    out, called = run_attest(ci, tmp_path, f'echo "$*" >"$CALLED"; echo {answer}')
    assert out == f"attested={attested}\n"
    assert "repos/CannObserv/status/commits/0123abc/status?per_page=100" in called


def test_a_refused_read_fails_the_step_loudly(ci, tmp_path):
    """Never a silent skip: the step fails, and writes no verdict."""
    with pytest.raises(subprocess.CalledProcessError):
        run_attest(ci, tmp_path, 'echo "HTTP 403" >&2; exit 1')
    assert (tmp_path / "output").read_text() == ""


def run_attest(ci: dict, tmp_path: Path, gh_body: str) -> tuple[str, str]:
    """Run the attest step's script as Actions would, against a stub `gh`.

    Returns what it wrote to $GITHUB_OUTPUT and the stub's arguments; asserts
    the verdict was logged too.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/usr/bin/env bash\n{gh_body}\n")
    gh.chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    called = tmp_path / "called"
    script = attest_step(ci)["run"].replace("${{ github.repository }}", "CannObserv/status")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HEAD": "0123abc",
        "GITHUB_OUTPUT": str(output),
        "CALLED": str(called),
    }
    result = subprocess.run(
        [*ACTIONS_BASH, script], env=env, capture_output=True, text=True, check=True
    )
    written = output.read_text()
    assert written in result.stdout, "the verdict is logged, not only written"
    return written, called.read_text()
