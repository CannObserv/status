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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.core.heartbeat import Signal
from src.core.utils import format_utc_iso

DRIFT_CHECK = "co-status-drift"
#: How long code may sit on ``main`` undeployed. Deploys are by hand; CI takes
#: about two minutes.
GRACE = timedelta(hours=8)
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
    if live == "dev":
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
    """The verdict for live behind in code since the push *since*."""
    age = now - since.at
    body = (
        f"{_ahead(live, compare)}, in code since the push of {format_utc_iso(since.at)} "
        f"({age / timedelta(hours=1):.1f} h ago; grace {GRACE / timedelta(hours=1):.0f} h)"
    )
    if age <= GRACE:
        return Verdict(Signal.UP, body)
    remedy = "scripts/deploy.sh" if ci == "success" else "deploy.sh refuses it until CI passes"
    return Verdict(Signal.FAIL, f"{body}. main's CI: {ci} — {remedy}")


def _ahead(live: str, compare: Mapping) -> str:
    n = compare["total_commits"]
    main = compare["commits"][-1]["sha"][:12]
    return f"live {live}, main {main}: {n} commit{'' if n == 1 else 's'} ahead"


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
