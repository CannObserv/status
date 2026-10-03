"""Does live lag origin/main in code that runs? The drift check (#12).

Since #9, production changes only when someone runs ``scripts/deploy.sh``,
so work pushed to ``main`` and never deployed goes unnoticed. An hourly
production timer asks GitHub how far the live release's ``REVISION`` is
behind ``main`` and pings healthchecks.io's ``co-status-drift``:

- **up** while live is ``main``, behind only in paths that never run
  (:func:`counts`), or behind in code for less than :data:`GRACE`;
- **/fail** once code has waited longer than that since the push that
  brought it, naming ``main``'s CI result (#11's gate refuses a red one);
  and at once when live is not on ``main`` at all;
- **/log** when GitHub cannot say. A long outage goes silent, and the
  check's own grace turns that into an alert.

The clock starts at the push, never the commit: a CI run's ``created_at``
is when its push landed, and a commit can be days older than that.
Unauthenticated, like the deploy gate: the repo is public, and the usual hour
costs two of the 60 requests GitHub allows an address.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from src.core.build import UNSTAMPED
from src.core.heartbeat import Signal
from src.core.logging import get_logger
from src.core.utils import format_utc_iso

logger = get_logger(__name__)

DRIFT_CHECK = "co-status-drift"
#: How long code may sit on ``main`` undeployed. Deploys are by hand; CI takes
#: about two minutes.
GRACE = timedelta(hours=8)
GITHUB_API = "https://api.github.com/repos/CannObserv/status"
#: CI's push runs on main, newest first: when each push landed (#11's gate asks the same).
RUNS_PATH = "actions/workflows/ci.yml/runs?event=push&branch=main&per_page=100"
#: At most this many extra compares to find the push that brought code. Past
#: it, the first push left unchecked starts the clock: the earliest the code
#: can have come, so an alert sooner, never later (CR 1).
WALK_LIMIT = 8
#: Every call together, inside the unit's ``TimeoutStartSec``.
CHECK_TIMEOUT_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 10.0
#: GitHub lists at most this many files in a compare; a list this long may be
#: cut short, and what was cut could be anything.
COMPARE_FILES_LIMIT = 300

#: Paths that never change what the units run. Anything else counts, so a new
#: directory of runtime code counts the day it appears.
_IGNORED_DIRS = ("docs/", "tests/", ".github/", ".claude/", "skills/", "skills-vendor/", ".skills/")
_IGNORED_FILES = {".gitmodules", ".gitignore", ".pre-commit-config.yaml", "LICENSE"}


@dataclass(frozen=True)
class Verdict:
    """What to tell ``co-status-drift``."""

    signal: Signal
    body: str


@dataclass(frozen=True)
class Push:
    """A push to ``main``: its newest commit, and when it landed."""

    sha: str
    at: datetime


def counts(path: str) -> bool:
    """Whether a change to *path* changes what a release runs."""
    if path in _IGNORED_FILES or path.endswith(".md"):
        return False
    return not path.startswith(_IGNORED_DIRS)


def diff_counts(compare: Mapping) -> bool:
    """Whether a GitHub compare touches a path that :func:`counts`, or may."""
    files = compare.get("files")
    if files is None or len(files) >= COMPARE_FILES_LIMIT:
        return True
    if len(compare["commits"]) < compare["total_commits"]:
        return True
    return any(counts(f["filename"]) for f in files)


def first_look(live: str, compare: Mapping) -> Verdict | None:
    """The verdict from ``compare/<live>...main`` alone, or ``None`` if it needs the clock."""
    if live == UNSTAMPED:
        return Verdict(Signal.FAIL, "live is unstamped (dev): its release has no REVISION")
    status = compare["status"]
    if status == "identical":
        return Verdict(Signal.UP, f"live {live} is main")
    if status != "ahead":
        return Verdict(Signal.FAIL, f"live {live} is not on main (GitHub: {status})")
    if not diff_counts(compare):
        return Verdict(Signal.UP, f"{_ahead(live, compare)}, none that runs")
    return None


def pushes(compare: Mapping, runs: Mapping) -> list[Push]:
    """The pushes to ``main`` since live, oldest first, from CI's push runs.

    GitHub runs CI once per push, on its newest commit. A ``main`` with no run
    (``[skip ci]``) falls back to its commit date: the only clock left.

    ``main`` is the compare's last commit. That holds for this repo's linear
    history (it commits straight to ``main``) and up to the 250 commits a
    compare lists; past that the diff already counts (:func:`diff_counts`), but
    the tip named, its date and its CI may be an older commit's (CR 6).
    """
    undeployed = {c["sha"] for c in compare["commits"]}
    landed: dict[str, datetime] = {}
    for run in runs["workflow_runs"]:
        if run["event"] != "push" or run["head_branch"] != "main":
            continue
        if run["head_sha"] not in undeployed:
            continue
        at = _parse(run["created_at"])
        landed[run["head_sha"]] = min(at, landed.get(run["head_sha"], at))
    tip = compare["commits"][-1]
    if tip["sha"] not in landed:
        landed[tip["sha"]] = _parse(tip["commit"]["committer"]["date"])
    return sorted((Push(s, at) for s, at in landed.items()), key=lambda p: p.at)


def tip_ci(runs: Mapping, tip: str) -> str:
    """``main``'s CI result: its newest push run's conclusion, else its status."""
    mine = [
        r
        for r in runs["workflow_runs"]
        if r["head_sha"] == tip and r["event"] == "push" and r["head_branch"] == "main"
    ]
    if not mine:
        return "no run"
    newest = max(mine, key=lambda r: r["created_at"])
    if newest["status"] != "completed":
        return newest["status"]
    return newest["conclusion"] or "nothing"


def lagging(live: str, compare: Mapping, since: Push, *, ci: str, now: datetime) -> Verdict:
    """The verdict for live behind in code, the clock started at the push *since*.

    Within the grace nothing is walked, so *since* is the oldest push since live,
    which may be docs only: the body names the clock, never "the code since" (CR 4).
    """
    age = now - since.at
    body = (
        f"{_ahead(live, compare)}, the clock started at the push of {format_utc_iso(since.at)} "
        f"({age / timedelta(hours=1):.1f} h ago; grace {GRACE / timedelta(hours=1):.0f} h)"
    )
    if age <= GRACE:
        return Verdict(Signal.UP, body)
    remedy = "scripts/deploy.sh" if ci == "success" else "deploy.sh refuses it until CI passes"
    return Verdict(Signal.FAIL, f"{body}. main's CI: {ci} — {remedy}")


class GitHubSilent(Exception):
    """GitHub gave no answer this check can use."""


class GitHubNotFound(GitHubSilent):
    """GitHub answered 404."""


async def assess(live: str, *, now: datetime, api: str = GITHUB_API) -> Verdict:
    """Ask GitHub about *live*, the live release's build id. Never raises."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                base_url=api,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"Accept": "application/vnd.github+json"},
            ) as client:
                return await _assess(client, live, now)
    except GitHubSilent as exc:
        return Verdict(Signal.LOG, f"GitHub did not answer: {exc}")
    except TimeoutError:
        return Verdict(Signal.LOG, "GitHub did not answer: TimeoutError")
    except (KeyError, TypeError, IndexError, ValueError, AttributeError):
        # GitHub's shape changed, or this module has a bug: the traceback says which (CR 3).
        logger.warning("drift check: GitHub's answer is not the JSON expected", exc_info=True)
        return Verdict(Signal.LOG, "GitHub's answer is not the JSON expected")


async def _assess(client: httpx.AsyncClient, live: str, now: datetime) -> Verdict:
    if live == UNSTAMPED:
        return first_look(live, {})
    try:
        compare = await _get(client, f"compare/{live}...main")
    except GitHubNotFound as exc:
        # A base GitHub does not know is live off main, not GitHub silent (CR 2).
        return Verdict(
            Signal.FAIL,
            f"GitHub does not know live {live} ({exc}): main rewritten since the deploy, "
            "or the repo is no longer public",
        )
    verdict = first_look(live, compare)
    if verdict is not None:
        return verdict
    runs = await _get(client, RUNS_PATH)
    found = pushes(compare, runs)
    since = found[0]
    if now - since.at > GRACE:
        since = await _first_counting(client, live, found)
    return lagging(live, compare, since, ci=tip_ci(runs, compare["commits"][-1]["sha"]), now=now)


async def _first_counting(client: httpx.AsyncClient, live: str, found: list[Push]) -> Push:
    """The oldest push whose diff from *live* counts; ``main``'s (the last) is known to.

    Past :data:`WALK_LIMIT`, the first push not asked about: every one before it
    is known not to count, so the code came with it at the earliest.
    """
    for push in found[:-1][:WALK_LIMIT]:
        if diff_counts(await _get(client, f"compare/{live}...{push.sha}")):
            return push
    return found[min(WALK_LIMIT, len(found) - 1)]


async def _get(client: httpx.AsyncClient, path: str) -> Mapping:
    try:
        response = await client.get(path)
    except httpx.HTTPError as exc:
        raise GitHubSilent(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
    if not response.is_success:
        try:
            message = response.json().get("message", "")
        except (ValueError, AttributeError):
            message = ""
        error = GitHubNotFound if response.status_code == 404 else GitHubSilent
        raise error(f"{response.status_code} {message}".strip())
    answer = response.json()
    if not isinstance(answer, Mapping):
        raise TypeError("not an object")
    return answer


def _ahead(live: str, compare: Mapping) -> str:
    n = compare["total_commits"]
    main = compare["commits"][-1]["sha"][:12]
    return f"live {live}, main {main}: {n} commit{'' if n == 1 else 's'} ahead"


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
