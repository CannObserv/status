"""Tests for src/core/drift.py — does live lag origin/main in code that runs (#12)?

GitHub's answers are built here in the shapes its REST API returns: the
compare (``status``, ``total_commits``, ``commits``, ``files``) and the CI
workflow's runs. The pure half is tested on those; the half that asks GitHub
goes through respx, never the network.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from src.core import drift
from src.core.drift import (
    COMPARE_FILES_LIMIT,
    GITHUB_API,
    GRACE,
    RUNS_PATH,
    Push,
    Verdict,
    assess,
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

    def test_a_file_moved_out_of_a_runtime_path_counts(self):
        """Its old path left the release: ``previous_filename`` says where it was."""
        answer = compare(1, files=[])
        answer["files"] = [{"filename": "docs/old.py", "previous_filename": "src/core/x.py"}]
        assert diff_counts(answer)

    def test_a_file_moved_within_paths_that_never_run_does_not_count(self):
        answer = compare(1, files=[])
        answer["files"] = [{"filename": "docs/b.md", "previous_filename": "docs/a.md"}]
        assert not diff_counts(answer)


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

    def test_no_runs_at_all_is_the_tips_commit_date(self):
        answer = compare(1)
        answer["commits"][0] = commit(1, NOW - timedelta(hours=3))
        assert pushes(answer, runs()) == [Push(sha(1), NOW - timedelta(hours=3))]

    def test_a_tip_with_no_run_landed_no_earlier_than_the_pushes_under_it(self):
        """An old ``[skip ci]`` commit pushed on top: never sorted before main's pushes (CR 10)."""
        answer = compare(1, 2)
        answer["commits"][1] = commit(2, NOW - timedelta(days=3))
        found = pushes(answer, runs(run(1, NOW - timedelta(hours=2))))
        assert found[-1] == Push(sha(2), NOW - timedelta(hours=2)), "the walk relies on it"


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
        """The clock's push, never "in code since": unwalked, it may be docs only (CR 4)."""
        since = Push(sha(1), NOW - GRACE + timedelta(minutes=1))
        verdict = lagging(LIVE, compare(1, 2), since, ci="success", now=NOW)
        assert verdict.signal is Signal.UP
        assert verdict.body == (
            f"live {LIVE}, main {sha(2)[:12]}: 2 commits ahead, the clock started at the push "
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


@pytest.fixture
def github():
    """GitHub's REST API for this repo; each test routes what it asks."""
    with respx.mock(base_url=GITHUB_API, assert_all_called=False) as mock:
        yield mock


def _route_compare(github, head: str, answer: dict):
    return github.get(f"/compare/{LIVE}...{head}").respond(200, json=answer)


def _route_runs(github, answer: dict):
    path, _, query = RUNS_PATH.partition("?")
    return github.get(f"/{path}", params=dict(p.split("=") for p in query.split("&"))).respond(
        200, json=answer
    )


class TestAssess:
    async def test_unstamped_asks_github_nothing(self, github):
        verdict = await assess("dev", now=NOW)
        assert verdict.signal is Signal.FAIL
        assert not github.calls

    async def test_in_sync_is_one_call(self, github):
        _route_compare(github, "main", compare(status="identical", files=[]))
        assert (await assess(LIVE, now=NOW)).signal is Signal.UP
        assert len(github.calls) == 1

    async def test_docs_alone_never_ask_for_runs(self, github):
        _route_compare(github, "main", compare(1, files=["docs/a.md"]))
        assert (await assess(LIVE, now=NOW)).signal is Signal.UP
        assert len(github.calls) == 1

    async def test_code_within_grace_is_two_calls(self, github):
        _route_compare(github, "main", compare(1))
        _route_runs(github, runs(run(1, NOW - timedelta(hours=1))))
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.UP
        assert "1.0 h ago" in verdict.body
        assert len(github.calls) == 2

    async def test_code_past_grace_fails(self, github):
        _route_compare(github, "main", compare(1))
        _route_runs(github, runs(run(1, NOW - timedelta(hours=9))))
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.FAIL
        assert "main's CI: success" in verdict.body
        assert len(github.calls) == 2

    async def test_old_docs_then_new_code_starts_the_clock_at_the_code(self, github):
        """Live sat behind a docs push for two days; code pushed an hour ago is not late."""
        _route_compare(github, "main", compare(1, 2))
        _route_runs(github, runs(run(1, NOW - timedelta(days=2)), run(2, NOW - timedelta(hours=1))))
        _route_compare(github, sha(1), compare(1, files=["docs/a.md"]))
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.UP
        assert "1.0 h ago" in verdict.body

    async def test_old_code_then_new_docs_is_late(self, github):
        _route_compare(github, "main", compare(1, 2))
        _route_runs(github, runs(run(1, NOW - timedelta(days=2)), run(2, NOW - timedelta(hours=1))))
        _route_compare(github, sha(1), compare(1))
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.FAIL
        assert "48.0 h ago" in verdict.body

    @staticmethod
    def _docs_pushes_then_code(github, third: timedelta):
        """Pushes 4 and 3 days ago, docs only; a third *third* ago; main now."""
        _route_compare(github, "main", compare(1, 2, 3, 4))
        _route_runs(
            github,
            runs(
                run(1, NOW - timedelta(days=4)),
                run(2, NOW - timedelta(days=3)),
                run(3, NOW - third),
                run(4, NOW),
            ),
        )
        for n in (1, 2, 3):
            _route_compare(github, sha(n), compare(n, files=["docs/a.md"]))

    async def test_an_old_skip_ci_tip_on_new_code_is_not_late(self, github):
        """Code pushed 2 h ago under a [skip ci] commit written 3 days ago (CR 10)."""
        answer = compare(1, 2)
        answer["commits"][1] = commit(2, NOW - timedelta(days=3))
        _route_compare(github, "main", answer)
        _route_runs(github, runs(run(1, NOW - timedelta(hours=2))))
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.UP
        assert "2.0 h ago" in verdict.body

    async def test_the_walk_stops_at_the_first_push_inside_the_grace(self, github):
        """It, or a newer push, brought the code: up either way, so ask no further (CR 14)."""
        _route_compare(github, "main", compare(1, 2, 3))
        _route_runs(
            github,
            runs(
                run(1, NOW - timedelta(days=2)),
                run(2, NOW - timedelta(hours=1)),
                run(3, NOW),
            ),
        )
        _route_compare(github, sha(1), compare(1, files=["docs/a.md"]))
        asked_about_2 = _route_compare(github, sha(2), compare(1, 2, files=["docs/a.md"]))
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.UP
        assert "1.0 h ago" in verdict.body
        assert not asked_about_2.called

    async def test_the_walk_stops_at_its_limit_at_the_first_push_unchecked(
        self, github, monkeypatch
    ):
        """Pushes 1 and 2 are known not to count: the code came with 3 at the earliest (CR 1)."""
        monkeypatch.setattr(drift, "WALK_LIMIT", 2)
        self._docs_pushes_then_code(github, timedelta(days=2))
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.FAIL, "the code may be as old as push 3"
        assert "48.0 h ago" in verdict.body
        assert len(github.calls) == 2 + 2

    async def test_past_the_limit_recent_code_is_not_late(self, github, monkeypatch):
        monkeypatch.setattr(drift, "WALK_LIMIT", 2)
        self._docs_pushes_then_code(github, timedelta(hours=1))
        assert (await assess(LIVE, now=NOW)).signal is Signal.UP


class TestAssessWhenGitHubCannotSay:
    async def test_a_refusal_is_logged_with_githubs_message(self, github):
        github.get(url__regex=r".*").respond(
            403, json={"message": "API rate limit exceeded for 1.2.3.4."}
        )
        verdict = await assess(LIVE, now=NOW)
        assert verdict == Verdict(
            Signal.LOG, "GitHub did not answer: 403 API rate limit exceeded for 1.2.3.4."
        )

    async def test_live_unknown_to_github_fails(self, github):
        """GitHub answers 404 for a base it does not know: not on main, said so (CR 2)."""
        github.get(f"/compare/{LIVE}...main").respond(404, json={"message": "Not Found"})
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.FAIL
        assert verdict.body.startswith(f"GitHub does not know live {LIVE} (404 Not Found)")

    async def test_a_404_on_anything_else_is_logged(self, github):
        _route_compare(github, "main", compare(1))
        github.get(url__regex=r".*/runs").respond(404, json={"message": "Not Found"})
        verdict = await assess(LIVE, now=NOW)
        assert verdict == Verdict(Signal.LOG, "GitHub did not answer: 404 Not Found")

    async def test_an_error_page_that_is_not_json_is_logged_by_status(self, github):
        github.get(url__regex=r".*").respond(502, text="<html>Bad gateway</html>")
        verdict = await assess(LIVE, now=NOW)
        assert verdict == Verdict(Signal.LOG, "GitHub did not answer: 502")

    async def test_unreachable_is_logged(self, github):
        github.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("refused"))
        verdict = await assess(LIVE, now=NOW)
        assert verdict == Verdict(Signal.LOG, "GitHub did not answer: ConnectError: refused")

    @pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"status": "ahead"}'], ids=repr)
    async def test_unexpected_json_is_logged(self, github, body):
        github.get(url__regex=r".*").respond(200, content=body)
        verdict = await assess(LIVE, now=NOW)
        assert verdict.signal is Signal.LOG
        assert "not the JSON expected" in verdict.body

    async def test_unexpected_json_leaves_its_traceback_in_the_journal(self, github, caplog):
        """A bug here would read as GitHub's fault without it (CR 3)."""
        github.get(url__regex=r".*").respond(200, json={"status": "ahead"})
        with caplog.at_level("WARNING"):
            await assess(LIVE, now=NOW)
        (record,) = [r for r in caplog.records if r.name == drift.logger.name]
        assert record.exc_info and record.exc_info[0] is KeyError

    async def test_a_stall_is_cut_off(self, github, monkeypatch):
        monkeypatch.setattr(drift, "CHECK_TIMEOUT_SECONDS", 0.05)

        async def stall(request):
            await asyncio.sleep(5)
            return httpx.Response(200)

        github.get(url__regex=r".*").mock(side_effect=stall)
        verdict = await assess(LIVE, now=NOW)
        assert verdict == Verdict(Signal.LOG, "GitHub did not answer: TimeoutError")
