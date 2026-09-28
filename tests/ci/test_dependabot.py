"""Drift tests for .github/dependabot.yml — the mechanism behind the cap policy.

The 0.x cap policy (notifier#30) converts "the resolver picks it up" into "a person
decides": every minor bump on a capped dependency needs a hand edit. Dependabot
is what proposes those edits (notifier#33), and its config fails completely silently —
a deleted or mistyped file just stops producing PRs, and the first symptom is
a security fix sitting unnoticed behind a cap.

The conditions asserted here are the ones a reader cannot check by eye:

* **The uv blocks must cover every dependency table.** The expected directory
  set is derived from ``TABLES`` in ``test_dependencies.py``, so a table added
  there without a matching dependabot directory fails here rather than
  drifting.
* **The versioning strategy must be pinned to ``increase-if-necessary``.**
  Floors in these tables mean "oldest supported" and must not ratchet on every
  bump; only a release outside the cap should touch the specifier. The default
  ``auto`` resolves to ``widen`` for anything classified as a library, and
  ``widen`` is unsupported for uv (dependabot-core#15290).
* **The workflows' actions are covered too.** They drift the same way and are
  gated the same way (by themselves, on the bump PR).
"""

import pytest
import yaml

from tests.ci.test_dependencies import REPO_ROOT, TABLES

CONFIG = REPO_ROOT / ".github" / "dependabot.yml"


def expected_directories() -> set[str]:
    """One dependabot directory per dependency table, in dependabot's shape."""
    return {
        "/" if path.parent == REPO_ROOT else f"/{path.parent.relative_to(REPO_ROOT).as_posix()}"
        for path in TABLES.values()
    }


def load() -> dict:
    """The parsed config, or an empty document while the file is missing.

    Tolerating absence keeps collection alive so the failure surfaces as
    ``test_config_exists``'s message rather than a collection error.
    """
    return yaml.safe_load(CONFIG.read_text()) if CONFIG.is_file() else {}


def blocks(ecosystem: str) -> list[dict]:
    return [u for u in load().get("updates", []) if u["package-ecosystem"] == ecosystem]


def directories(block: dict) -> set[str]:
    """A block's directories, whichever of the two spellings it uses."""
    return set(block.get("directories", [])) | (
        {block["directory"]} if "directory" in block else set()
    )


def test_config_exists():
    assert CONFIG.is_file(), (
        "no .github/dependabot.yml — nothing proposes the bumps the 0.x cap "
        "policy makes invisible to `uv lock` (notifier#33)"
    )


def test_config_is_version_2():
    """Version 1 configs are dead; dependabot ignores them without erroring."""
    assert load().get("version") == 2


def test_uv_blocks_cover_every_dependency_table():
    covered = set().union(*(directories(block) for block in blocks("uv")))
    missing = expected_directories() - covered
    assert not missing, (
        f"tables at {sorted(missing)} get no bump PRs — their caps drift "
        f"silently. Add the directory to the uv update block."
    )


@pytest.mark.parametrize("block", blocks("uv"), ids=lambda b: ",".join(sorted(directories(b))))
def test_uv_strategy_only_moves_a_cap_when_forced(block):
    """Floors mean "oldest supported"; only a release outside the cap should
    touch the specifier. The default `auto` can resolve to `widen`, which
    uv does not support (dependabot-core#15290)."""
    assert block.get("versioning-strategy") == "increase-if-necessary"


@pytest.mark.parametrize("block", blocks("uv"), ids=lambda b: ",".join(sorted(directories(b))))
def test_uv_dev_churn_is_grouped(block):
    """Eight separate PRs a week is how a bot gets muted (notifier#33). Dev-group
    tools carry most of the churn; runtime deps stay individual PRs."""
    groups = block.get("groups", {})
    assert any(group.get("dependency-type") == "development" for group in groups.values()), (
        "no group collapses development dependencies; ruff alone would open a PR most months"
    )


def test_actions_are_covered_too():
    covered = set().union(*(directories(block) for block in blocks("github-actions")))
    assert "/" in covered, (
        ".github/workflows/ pins actions/checkout and astral-sh/setup-uv; "
        "they drift exactly like the uv tables"
    )


@pytest.mark.parametrize("ecosystem", ["uv", "github-actions"], ids=str)
def test_every_block_runs_on_a_schedule(ecosystem):
    """A block without an interval is a config dependabot rejects — visibly
    only in the repo's Dependabot tab, which nobody watches."""
    for block in blocks(ecosystem):
        assert block.get("schedule", {}).get("interval"), (
            f"{ecosystem} block declares no schedule.interval"
        )
