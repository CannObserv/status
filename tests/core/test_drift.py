"""Tests for src/core/drift.py — does live lag origin/main in code that runs (#12)?

GitHub's answers are built here in the shapes its REST API returns: the
compare (``status``, ``total_commits``, ``commits``, ``files``) and the CI
workflow's runs. The pure half is tested on those; the half that asks GitHub
goes through respx, never the network.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.drift import (
    COMPARE_FILES_LIMIT,
    GRACE,
    Push,
    Verdict,
    counts,
    diff_counts,
    first_look,
    lagging,
    pushes,
    tip_ci,
)
from src.core.heartbeat import Signal

NOW = datetime(2026, 10, 2, 22, 0, tzinfo=UTC)
LIVE = "deff23b0c287"


def sha(n: int) -> str:
    """A distinct full-length commit id."""
    return f"{n:040x}"


def commit(n: int, committed: datetime = NOW) -> dict:
    return {"sha": sha(n), "commit": {"committer": {"date": committed.isoformat()}}}


def compare(*shas: int, files=("src/core/sweep.py",), status="ahead", total=None) -> dict:
    """GitHub's ``compare/<live>...main``: *shas* oldest first, the last one main's."""
    answer = {
        "status": status,
        "total_commits": len(shas) if total is None else total,
        "commits": [commit(n) for n in shas],
    }
    if files is not None:
        answer["files"] = [{"filename": f} for f in files]
    return answer


def run(
    n: int,
    created: datetime,
    *,
    event="push",
    branch="main",
    status="completed",
    conclusion="success",
) -> dict:
    return {
        "head_sha": sha(n),
        "event": event,
        "head_branch": branch,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
    }


def runs(*items: dict) -> dict:
    return {"workflow_runs": list(items)}


class TestCounts:
    @pytest.mark.parametrize(
        "path",
        [
            "src/core/sweep.py",
            "scripts/deploy.sh",
            "alembic/versions/0001_x.py",
            "alembic.ini",
            "pyproject.toml",
            "uv.lock",
            "deploy/status.service",
            "something/new.py",
        ],
    )
    def test_what_runs_counts(self, path):
        assert counts(path)

    @pytest.mark.parametrize(
        "path",
        [
            "docs/plans/2026-10-01-ci-gate.md",
            "AGENTS.md",
            "src/README.md",
            "tests/core/test_sweep.py",
            ".github/workflows/ci.yml",
            ".claude/skills/x",
            "skills/brainstorming",
            "skills-vendor/obra-superpowers",
            ".skills/doctor.sh",
            ".gitmodules",
            ".gitignore",
            ".pre-commit-config.yaml",
            "LICENSE",
        ],
    )
    def test_what_never_runs_does_not(self, path):
        assert not counts(path)


class TestDiffCounts:
    def test_docs_alone_do_not_count(self):
        assert not diff_counts(compare(1, files=["docs/a.md", "tests/test_a.py"]))

    def test_one_path_that_runs_is_enough(self):
        assert diff_counts(compare(1, files=["docs/a.md", "src/core/drift.py"]))

    def test_no_file_list_counts(self):
        assert diff_counts(compare(1, files=None))

    def test_a_file_list_at_githubs_limit_counts(self):
        """GitHub cuts the list off there; what follows could be anything."""
        assert diff_counts(compare(1, files=["docs/a.md"] * COMPARE_FILES_LIMIT))

    def test_a_commit_list_cut_short_counts(self):
        assert diff_counts(compare(1, files=["docs/a.md"], total=300))


class TestFirstLook:
    def test_unstamped_fails(self):
        verdict = first_look("dev", compare())
        assert verdict.signal is Signal.FAIL
        assert "unstamped" in verdict.body

    def test_identical_is_up(self):
        verdict = first_look(LIVE, compare(status="identical", files=[]))
        assert verdict == Verdict(Signal.UP, f"live {LIVE} is main")

    @pytest.mark.parametrize("status", ["diverged", "behind"])
    def test_live_not_on_main_fails(self, status):
        verdict = first_look(LIVE, compare(1, status=status))
        assert verdict.signal is Signal.FAIL
        assert f"live {LIVE} is not on main" in verdict.body
        assert status in verdict.body

    def test_ahead_in_docs_alone_is_up(self):
        """Today's case: 1625627, a plan marked done, one commit past the deploy."""
        verdict = first_look(LIVE, compare(1, 2, files=["docs/plans/x.md"]))
        assert verdict.signal is Signal.UP
        assert "2 commits ahead, none that runs" in verdict.body
        assert sha(2)[:12] in verdict.body

    def test_ahead_in_code_needs_the_clock(self):
        assert first_look(LIVE, compare(1)) is None


class TestPushes:
    def test_each_push_is_its_runs_creation_oldest_first(self):
        found = pushes(
            compare(1, 2, 3),
            runs(run(3, NOW - timedelta(hours=1)), run(1, NOW - timedelta(hours=5))),
        )
        assert found == [
            Push(sha(1), NOW - timedelta(hours=5)),
            Push(sha(3), NOW - timedelta(hours=1)),
        ]

    def test_runs_of_deployed_commits_are_not_pushes_since(self):
        found = pushes(compare(2), runs(run(1, NOW - timedelta(days=2)), run(2, NOW)))
        assert found == [Push(sha(2), NOW)]

    def test_only_push_runs_on_main(self):
        found = pushes(
            compare(1, 2),
            runs(
                run(1, NOW - timedelta(hours=9), event="workflow_dispatch"),
                run(1, NOW - timedelta(hours=8), branch="12-drift-check"),
                run(2, NOW),
            ),
        )
        assert found == [Push(sha(2), NOW)]

    def test_a_commit_with_two_push_runs_was_pushed_at_the_first(self):
        found = pushes(compare(1), runs(run(1, NOW), run(1, NOW - timedelta(hours=2))))
        assert found == [Push(sha(1), NOW - timedelta(hours=2))]

    def test_a_tip_with_no_run_falls_back_to_its_commit_date(self):
        """``[skip ci]``: GitHub ran nothing, so the commit is the only clock."""
        answer = compare(1, 2)
        answer["commits"][1] = commit(2, NOW - timedelta(hours=3))
        found = pushes(answer, runs(run(1, NOW - timedelta(hours=4))))
        assert found == [
            Push(sha(1), NOW - timedelta(hours=4)),
            Push(sha(2), NOW - timedelta(hours=3)),
        ]


class TestTipCi:
    def test_the_newest_push_runs_conclusion(self):
        answer = runs(
            run(2, NOW - timedelta(hours=2), conclusion="failure"),
            run(2, NOW - timedelta(hours=1), conclusion="success"),
            run(1, NOW, conclusion="cancelled"),
        )
        assert tip_ci(answer, sha(2)) == "success"

    def test_unfinished_is_its_status(self):
        assert tip_ci(runs(run(2, NOW, status="in_progress")), sha(2)) == "in_progress"

    def test_no_run(self):
        assert tip_ci(runs(run(1, NOW)), sha(2)) == "no run"


class TestLagging:
    def test_within_grace_is_up(self):
        since = Push(sha(1), NOW - GRACE + timedelta(minutes=1))
        verdict = lagging(LIVE, compare(1, 2), since, ci="success", now=NOW)
        assert verdict.signal is Signal.UP
        assert verdict.body == (
            f"live {LIVE}, main {sha(2)[:12]}: 2 commits ahead, in code since the push "
            f"of 2026-10-02T14:01:00Z (8.0 h ago; grace 8 h)"
        )

    def test_past_grace_fails_and_names_mains_ci(self):
        since = Push(sha(1), NOW - GRACE - timedelta(minutes=30))
        verdict = lagging(LIVE, compare(1), since, ci="failure", now=NOW)
        assert verdict.signal is Signal.FAIL
        assert verdict.body.endswith(
            "(8.5 h ago; grace 8 h). main's CI: failure — deploy.sh refuses it until CI passes"
        )

    def test_past_grace_with_green_ci_says_deploy(self):
        since = Push(sha(1), NOW - timedelta(days=2))
        verdict = lagging(LIVE, compare(1), since, ci="success", now=NOW)
        assert verdict.body.endswith("main's CI: success — scripts/deploy.sh")

    def test_one_commit_is_singular(self):
        since = Push(sha(1), NOW)
        assert "1 commit ahead," in lagging(LIVE, compare(1), since, ci="success", now=NOW).body
