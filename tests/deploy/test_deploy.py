"""End-to-end tests for scripts/deploy.sh against a throwaway root (#9).

Real git, a temporary origin and clone; stub ``uv``, ``sudo``, ``curl`` and
``logger`` on ``PATH`` that record every call. The stubs read ``FAKE_*``
variables to play a database that is behind or ahead, a failing migration, a
failing sweep pass, or an API that never reports the new build.

What the script must hold (spec R2–R4, R6, R9, R13):

- **Only pushed commits.** Live needs a commit on ``origin/main``. Dev accepts
  any ``origin/*`` branch.
- **Immutable releases.** One per commit, built once, read-only, with
  ``REVISION`` written last. A directory without ``REVISION`` is an
  interrupted build.
- **Root owns what the units run** (#14): the deploy root, ``releases/``, each
  finished release, and so the links. The stubs play root: a ledger of what
  it owns, kept unwritable but through ``sudo``, so a write under the root
  without ``sudo`` fails here as it would on the VM.
- **Order.** Dev before live. Migrate, then switch, then restart, then verify.
  A failed verify switches back.
- **A database ahead of the release is not migrated.** That is a rollback.
- **Live needs green CI** (#11): the commit's push run on main, every job
  ``success``, asked of GitHub before anything is built. Dev is never gated.
- **Units follow their target's release** (#18): each target installs its own
  units from the release it switches to, only those that differ, and a switch
  back puts the installed copies back. Host configs are compared, never
  installed.
"""

import contextlib
import fcntl
import grp
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

STUB_UV = r"""#!/usr/bin/env bash
echo "uv $PWD ALLOW=${STATUS_ALLOW_PROD_DB:-} URL=${DATABASE_URL:-} $*" >> "$FAKE_LOG"
case "$*" in
  sync*)
    [[ -n "${FAKE_SYNC_FAIL:-}" ]] && exit 1
    mkdir -p .venv ;;
  *schema_state*)
    [[ -n "${FAKE_SCHEMA_CRASH:-}" ]] && exit "$FAKE_SCHEMA_CRASH"
    state="${FAKE_SCHEMA:-current}"
    echo "$state"
    [[ "$state" == behind || "$state" == unmigrated ]] && exit 3 ;;
  *"alembic heads"*)
    for ((i = 0; i < ${FAKE_HEADS:-1}; i++)); do echo "head$i (head)"; done ;;
  *"alembic upgrade"*)
    exit "${FAKE_MIGRATE_RC:-0}" ;;
  *"python -c"*)
    [[ -n "${FAKE_BROKEN_VENV:-}" ]] && { echo "probe: no interpreter" >&2; exit 2; } ;;
esac
exit 0
"""

# Plays root (#14). FAKE_ROOT_OWNED is the ledger of what root owns, one
# "D <dir>" (that directory) or "R <path>" (that tree) per line; the stat stub
# answers from it. What root owns is kept unwritable, and only a command run
# through here opens it, so a write under the deploy root without sudo fails.
STUB_SUDO = r"""#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
# Files are real, under STATUS_DEPLOY_ETC; FAKE_INSTALL_FAIL fails installing
# that unit name, FAKE_TRY_RESTART_FAIL restarting it (#18).
[[ "$*" == "systemctl try-restart ${FAKE_TRY_RESTART_FAIL:-none}" ]] && exit 1
[[ "$1" == chown && -n "${FAKE_CHOWN_FAIL:-}" ]] && exit 1
as_root() {
  local kind path opened=() rc=0
  while read -r kind path; do
    [[ -d "$path" && ! -L "$path" ]] && chmod u+w "$path" && opened+=("$path")
  done <"$FAKE_ROOT_OWNED"
  # Root needs no permission to delete a read-only tree.
  if [[ "$1" == rm && "$2" == -rf ]]; then
    for path in "${@:3}"; do [[ -e "$path" ]] && chmod -R u+w "$path"; done
  fi
  "$@" || rc=$?
  for path in "${opened[@]}"; do [[ -d "$path" ]] && chmod a-w "$path"; done
  return $rc
}
ledger() { echo "$1 $2" >>"$FAKE_ROOT_OWNED"; }
case "$1" in
  install)
    [[ -n "${FAKE_INSTALL_FAIL:-}" && "${@: -1}" == */"$FAKE_INSTALL_FAIL" ]] && exit 1
    as_root "$@" || exit
    # A directory install -d makes is root's unless it names another owner,
    # and unwritable at once, like the rest of what root owns (CR 8).
    if [[ "$2" == -d* && " $* " != *" -o "* ]]; then
      ledger D "${@: -1}"
      chmod a-w "${@: -1}"
    fi
    exit 0 ;;
  chown)
    [[ "$2" == -R && "$3" == root:root ]] || { echo "stub sudo: chown $*?" >&2; exit 1; }
    ledger R "$4"
    exit 0 ;;
  rm)
    as_root "$@" || exit
    for path in "${@:2}"; do
      [[ -e "$path" ]] || sed -i "\\#^[DR] $path\\(/.*\\)\\{0,1\\}\$#d" "$FAKE_ROOT_OWNED"
    done
    exit 0 ;;
  ln | mv | chmod | touch | tee) as_root "$@"; exit ;;
esac
# FAKE_SWEEP_FAIL fails that unit's pass, but only while its target runs
# FAKE_SWEEP_FAIL_BUILD when that is set: a broken build, not a broken unit (CR 24).
if [[ "$1" == systemctl && "$2" == start && "$3" == "${FAKE_SWEEP_FAIL:-none}" ]]; then
  case "$3" in status-sweep.service) link=live ;; *) link=dev ;; esac
  running="$(basename "$(readlink "$STATUS_DEPLOY_ROOT/$link")")"
  [[ -z "${FAKE_SWEEP_FAIL_BUILD:-}" || "$running" == "$FAKE_SWEEP_FAIL_BUILD" ]] && exit 1
fi
exit 0
"""

# /health answers with whichever release the port's symlink names, as the
# restarted API would; FAKE_STALE_PORT plays an API still on the old build.
#
# GitHub's Actions API (#11) answers from <tmp>/ci: runs-<n>.json in turn, the
# last repeating (a run that finishes between polls), and jobs-<id>.json. With
# none written, one green push run on main for whatever commit was asked about.
# FAKE_CI_ERROR is GitHub refusing; as real curl does, only --fail-with-body
# passes its message on (CR 2).
STUB_CURL = r"""#!/usr/bin/env bash
url="${@: -1}"
echo "curl $url" >> "$FAKE_LOG"
if [[ "$url" == https://api.github.com/* ]]; then
  ci="$(dirname "$FAKE_LOG")/ci"
  if [[ -n "${FAKE_CI_ERROR:-}" ]]; then
    [[ " $* " == *" --fail-with-body "* ]] && echo "{\"message\":\"$FAKE_CI_ERROR\"}"
    exit 22
  fi
  case "$url" in
    */jobs*)
      id="${url#*/actions/runs/}"
      id="${id%%/*}"
      cat "$ci/jobs-$id.json" 2>/dev/null || echo '{"jobs":[
        {"name":"lint","status":"completed","conclusion":"success"},
        {"name":"test","status":"completed","conclusion":"success"},
        {"name":"migrations","status":"completed","conclusion":"success"}]}' ;;
    *)
      sha="${url#*head_sha=}"
      sha="${sha%%&*}"
      n="$(cat "$ci/polls")"
      echo $((n + 1)) > "$ci/polls"
      answers=("$ci"/runs-*.json)
      if [[ -e "${answers[0]}" ]]; then
        last=$((${#answers[@]} - 1))
        cat "$ci/runs-$((n < last ? n : last)).json"
      else
        echo "{\"workflow_runs\":[{\"id\":1,\"head_sha\":\"$sha\",\"event\":\"push\",
          \"head_branch\":\"main\",\"status\":\"completed\",\"conclusion\":\"success\",
          \"created_at\":\"2026-10-01T00:00:00Z\",\"html_url\":\"https://github.test/runs/1\"}]}"
      fi ;;
  esac
  exit 0
fi
case "$url" in *:9000/*) link=live ;; *:9001/*) link=dev ;; esac
# As src/core/build.py does: the served tree's REVISION, else "dev" (CR 36).
served="$(cd "$STATUS_DEPLOY_ROOT" && cd -P "$(readlink "$link")" 2>/dev/null && pwd)"
build="$(cat "$served/REVISION" 2>/dev/null || echo dev)"
# Stale only while the link names the newest main commit, unless always.
if [[ "$url" == *":${FAKE_STALE_PORT:-none}/"* ]]; then
  [[ -n "${FAKE_STALE_ALWAYS:-}" || "$build" == "${FAKE_NEW_BUILD:-$build}" ]] && build=stale
fi
# FAKE_STALE_UNLESS_UNIT_HAS: a live API that answers only while status.service
# holds that text, a hand fix the repo lacks (CR 10).
if [[ -n "${FAKE_STALE_UNLESS_UNIT_HAS:-}" && "$link" == live ]]; then
  grep -qF -- "$FAKE_STALE_UNLESS_UNIT_HAS" "$STATUS_DEPLOY_ETC/systemd/system/status.service" ||
    build=stale
fi
case "$url" in
  */ready) echo '{"status":"ready","schema_state":"current"}' ;;
  */health) echo "{\"status\":\"ok\",\"build\":\"$build\"}" ;;
esac
"""

# `systemctl show` needs no sudo. FAKE_SWEEP_BUSY is how many polls report a
# pass still running (a timer pass that started on the old release).
STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
busy_file="$(dirname "$FAKE_LOG")/busy"  # World.run writes it per run
left="$(cat "$busy_file")"
if [[ "$1" == show && "$left" -gt 0 ]]; then
  echo $((left - 1)) > "$busy_file"
  echo activating
elif [[ "$1" == show ]]; then
  echo inactive
fi
"""

# FAKE_RM_FAIL fails the recursive delete, as a real prune failure would.
STUB_RM = r"""#!/usr/bin/env bash
[[ -n "${FAKE_RM_FAIL:-}" && "$1" == -rf ]] && exit 1
exec /bin/rm "$@"
"""

STUB_LOGGER = r"""#!/usr/bin/env bash
echo "logger $*" >> "$FAKE_LOG"
"""

# `stat -c %u -- <path>`: 0 for what the ledger says root owns (#14).
STUB_STAT = r"""#!/usr/bin/env bash
if [[ "$1" == -c && "$2" == %u ]]; then
  path="${@: -1}"
  while read -r kind owned; do
    if [[ "$path" == "$owned" || ("$kind" == R && "$path" == "$owned"/*) ]]; then
      echo 0
      exit 0
    fi
  done <"$FAKE_ROOT_OWNED"
fi
exec @STAT@ "$@"
"""


def ci_run(
    run_id: int,
    sha: str,
    *,
    event: str = "push",
    branch: str = "main",
    status: str = "completed",
    conclusion: str = "success",
    created: str = "2026-10-01T00:00:00Z",
) -> dict:
    """One workflow run as GitHub's Actions API lists it."""
    return {
        "id": run_id,
        "head_sha": sha,
        "event": event,
        "head_branch": branch,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "created_at": created,
        "html_url": f"https://github.test/runs/{run_id}",
    }


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(checkout: Path, label: str) -> str:
    (checkout / "version.txt").write_text(label)
    git(checkout, "add", "version.txt")
    git(checkout, "commit", "-q", "-m", label)
    return git(checkout, "rev-parse", "HEAD")


class World:
    """A temp origin, clone, deploy root, env dir and stubbed PATH."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.checkout = tmp / "checkout"
        self.root = tmp / "srv"
        self.etc = tmp / "etc"
        self.log = tmp / "calls.log"
        self.stubs = tmp / "stubs"
        self.ci = tmp / "ci"
        self.sysetc = tmp / "sysetc"  # /etc, for units and host configs (#18)
        self.units = self.sysetc / "systemd" / "system"
        self.owned = tmp / "root-owned"  # the sudo and stat stubs' ledger (#14)
        for d in (self.root, self.etc, self.stubs, self.ci):
            d.mkdir()
        self.units.mkdir(parents=True)
        self.log.touch()
        # As the RUNBOOK makes it: root's, so only sudo writes in it (#14).
        self.owned.write_text(f"D {self.root}\n")
        self.root.chmod(0o555)

        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp, "clone", "-q", str(self.origin), str(self.checkout))
        git(self.checkout, "config", "user.email", "t@example.com")
        git(self.checkout, "config", "user.name", "t")
        self.main = [commit(self.checkout, f"main-{i}") for i in range(3)]
        git(self.checkout, "push", "-q", "origin", "main")
        git(self.checkout, "switch", "-q", "-c", "feature")
        self.feature = commit(self.checkout, "feature")
        git(self.checkout, "push", "-q", "origin", "feature")
        git(self.checkout, "switch", "-q", "main")
        self.unpushed = commit(self.checkout, "unpushed")

        scripts = self.checkout / "scripts"
        scripts.mkdir()
        shutil.copy(DEPLOY, scripts / "deploy.sh")
        shutil.copy(REPO_ROOT / "scripts" / "tailnet_bind.sh", scripts / "tailnet_bind.sh")

        (self.etc / ".env").write_text("DATABASE_URL=postgresql+asyncpg://u@h/status\n")
        (self.etc / "dev.env").write_text("DEV_DATABASE_URL=postgresql+asyncpg://u@h/status_dev\n")

        for name, body in (
            ("uv", STUB_UV),
            ("sudo", STUB_SUDO),
            ("curl", STUB_CURL),
            ("logger", STUB_LOGGER),
            ("systemctl", STUB_SYSTEMCTL),
            ("rm", STUB_RM),
            ("stat", STUB_STAT.replace("@STAT@", shutil.which("stat") or "/usr/bin/stat")),
        ):
            stub = self.stubs / name
            stub.write_text(body)
            stub.chmod(0o755)

    def run(self, *args: str, **fake: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "STATUS_DEPLOY_ROOT": str(self.root),
            "STATUS_DEPLOY_ENV_DIR": str(self.etc),
            "STATUS_DEPLOY_ETC": str(self.sysetc),
            "STATUS_DEPLOY_VERIFY_SECONDS": "2",
            "STATUS_BIND_HOST": "127.0.0.1",
            "FAKE_LOG": str(self.log),
            "FAKE_ROOT_OWNED": str(self.owned),
            # The build FAKE_STALE_PORT refuses to report: the one being deployed.
            "FAKE_NEW_BUILD": self.build(self.main[-1]),
            **fake,
        }
        # Per run, not per world: FAKE_SWEEP_BUSY holds for any run (CR 19).
        (self.tmp / "busy").write_text(fake.get("FAKE_SWEEP_BUSY", "0"))
        (self.ci / "polls").write_text("0")
        return subprocess.run(
            [str(self.checkout / "scripts" / "deploy.sh"), *args],
            cwd=self.tmp,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def build(self, sha: str) -> str:
        return git(self.checkout, "rev-parse", "--short=12", sha)

    def push_deploy(self, files: dict[str, str | None], branch: str = "main") -> str:
        """Commit ``deploy/`` files (None deletes one) on top of origin's
        ``branch`` and push it. The checkout's main holds an unpushed commit,
        so this happens in a clone of its own. A main commit joins ``self.main``."""
        pusher = self.tmp / "pusher"
        if not pusher.exists():
            git(self.tmp, "clone", "-q", str(self.origin), str(pusher))
            git(pusher, "config", "user.email", "t@example.com")
            git(pusher, "config", "user.name", "t")
        git(pusher, "fetch", "-q", "origin")
        git(pusher, "switch", "-q", "-C", branch, f"origin/{branch}")
        for name, body in files.items():
            path = pusher / "deploy" / name
            if body is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        git(pusher, "add", "-A", "deploy")
        git(pusher, "commit", "-q", "--allow-empty", "-m", f"deploy/ on {branch}")
        git(pusher, "push", "-q", "origin", branch)
        git(self.checkout, "fetch", "-q", "origin")
        sha = git(pusher, "rev-parse", "HEAD")
        if branch == "main":
            self.main.append(sha)
        return sha

    def installed(self, name: str) -> str | None:
        """An installed unit's text, or None."""
        path = self.units / name
        return path.read_text() if path.exists() else None

    def target(self, name: str) -> str | None:
        link = self.root / name
        return os.readlink(link) if link.is_symlink() else None

    def ci_answers(self, *answers: list[dict] | str) -> None:
        """What GitHub lists as the commit's runs, poll by poll; the last repeats."""
        for n, runs in enumerate(answers):
            body = runs if isinstance(runs, str) else json.dumps({"workflow_runs": runs})
            (self.ci / f"runs-{n}.json").write_text(body)

    def ci_jobs(self, run_id: int, **conclusions: str) -> None:
        """A run's jobs by conclusion; any of lint, test, migrations left out is green."""
        jobs = {"lint": "success", "test": "success", "migrations": "success", **conclusions}
        body = [
            {"name": name, "status": "completed", "conclusion": conclusion}
            for name, conclusion in jobs.items()
            if conclusion != "absent"
        ]
        (self.ci / f"jobs-{run_id}.json").write_text(json.dumps({"jobs": body}))

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def root_owned(self, path: Path) -> bool:
        """Whether the ledger says root owns ``path``, as the stat stub answers."""
        for line in self.owned.read_text().splitlines():
            kind, owned = line.split(" ", 1)
            if str(path) == owned or (kind == "R" and str(path).startswith(owned + "/")):
                return True
        return False

    def disown(self, path: Path) -> None:
        """``path`` is exedev's again: built before #14, or handed back by hand."""
        lines = self.owned.read_text().splitlines()
        self.owned.write_text("".join(f"{x}\n" for x in lines if x.split(" ", 1)[1] != str(path)))

    @contextlib.contextmanager
    def as_root(self) -> Iterator[None]:
        """A hand edit under the deploy root, done with sudo as an operator would."""
        dirs = [Path(x.split(" ", 1)[1]) for x in self.owned.read_text().splitlines()]
        dirs = [d for d in dirs if d.is_dir() and not d.is_symlink()]
        for d in dirs:
            d.chmod(d.stat().st_mode | stat.S_IWUSR)
        try:
            yield
        finally:
            for d in dirs:
                if d.is_dir():
                    d.chmod(d.stat().st_mode & ~0o222)

    def github_calls(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("curl https://api.github.com/")]

    def reset_log(self) -> None:
        self.log.write_text("")


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    # The root and releases are read-only; make them deletable for tmp_path's cleanup.
    if w.root.exists():
        for path in [w.root, *w.root.rglob("*")]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)


def assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def index_of(calls: list[str], needle: str) -> int:
    return next(i for i, call in enumerate(calls) if needle in call)


def test_script_exists_and_is_executable():
    assert DEPLOY.is_file()
    assert DEPLOY.stat().st_mode & stat.S_IXUSR


class TestADeploy:
    def test_puts_origin_main_on_dev_and_live(self, world):
        assert_ok(world.run())
        build = world.build(world.main[-1])
        assert world.target("live") == world.target("dev") == f"releases/{build}"
        assert (world.root / "releases" / build / "REVISION").read_text().strip() == build

    def test_the_release_is_the_commit_and_nothing_else(self, world):
        """No .git, no untracked files: nothing in a release can drift."""
        (world.checkout / "untracked.txt").write_text("x")
        assert_ok(world.run())
        release = world.root / "releases" / world.build(world.main[-1])
        assert (release / "version.txt").read_text() == "main-2"
        assert not (release / ".git").exists()
        assert not (release / "untracked.txt").exists()

    def test_the_release_is_read_only(self, world):
        assert_ok(world.run())
        release = world.root / "releases" / world.build(world.main[-1])
        for path in [release, *release.rglob("*")]:
            if not path.is_symlink():
                assert not path.stat().st_mode & 0o222, path

    def test_dev_is_deployed_and_verified_before_live_is_touched(self, world):
        assert_ok(world.run())
        calls = world.calls()
        dev_sweep = index_of(calls, "systemctl start status-sweep-dev.service")
        live_migrate = next(
            i for i, c in enumerate(calls) if "alembic upgrade" in c and "ALLOW=1" in c
        )
        assert index_of(calls, "systemctl restart status-dev") < dev_sweep < live_migrate
        assert live_migrate < calls.index("sudo systemctl restart status")

    def test_each_target_migrates_its_own_database(self, world):
        """Only live carries the production opt-in (src/core/db_safety.py)."""
        assert_ok(world.run())
        upgrades = [c for c in world.calls() if "alembic upgrade head" in c]
        assert len(upgrades) == 2
        assert "URL=postgresql+asyncpg://u@h/status_dev" in upgrades[0]
        assert "ALLOW= " in upgrades[0]
        assert "URL=postgresql+asyncpg://u@h/status " in upgrades[1]
        assert "ALLOW=1" in upgrades[1]

    def test_the_build_is_synced_locked_without_dev_dependencies(self, world):
        assert_ok(world.run())
        (sync,) = [c for c in world.calls() if " sync " in c]
        assert "--locked" in sync and "--no-dev" in sync

    def test_every_run_in_the_release_neither_syncs_nor_relocks(self, world):
        assert_ok(world.run())
        runs = [c for c in world.calls() if c.startswith("uv ") and " run " in c]
        assert runs
        assert all(" run --frozen --no-sync " in c for c in runs), runs

    def test_the_deploy_is_recorded_in_the_journal(self, world):
        assert_ok(world.run())
        build = world.build(world.main[-1])
        logged = [c for c in world.calls() if c.startswith("logger ")]
        assert any("live" in c and build in c for c in logged)


class TestWhatMayBeDeployed:
    def test_an_unpushed_commit_is_refused(self, world):
        result = world.run(world.unpushed)
        assert result.returncode != 0
        assert "origin/main" in result.stderr
        assert not (world.root / "releases").exists() or not any(
            (world.root / "releases").iterdir()
        )

    def test_a_branch_commit_never_goes_live(self, world):
        result = world.run(world.feature)
        assert result.returncode != 0
        assert world.target("live") is None

    def test_a_pushed_branch_may_go_to_dev_alone(self, world):
        assert_ok(world.run("--dev", "origin/feature"))
        assert world.target("dev") == f"releases/{world.build(world.feature)}"
        assert world.target("live") is None
        assert not [c for c in world.calls() if "status-sweep.service" in c]

    def test_an_unpushed_commit_is_refused_for_dev_too(self, world):
        assert world.run("--dev", world.unpushed).returncode != 0

    def test_an_older_main_commit_may_be_deployed(self, world):
        """How a rollback is spelled: deploy the previous build again."""
        assert_ok(world.run(world.main[0]))
        assert world.target("live") == f"releases/{world.build(world.main[0])}"

    def test_an_unknown_ref_is_refused(self, world):
        result = world.run("no-such-ref")
        assert result.returncode != 0
        assert "no-such-ref" in result.stderr

    def test_two_alembic_heads_are_refused_before_anything_switches(self, world):
        result = world.run(FAKE_HEADS="2")
        assert result.returncode != 0
        assert "head" in result.stderr
        assert world.target("dev") is None
        release = world.root / "releases" / world.build(world.main[-1])
        assert not (release / "REVISION").exists()


# A wait no stubbed poll can outlast, polled at once: SECONDS ticks on the
# wall clock's second, so a 1 s wait can be spent before its first answer (CR 1).
PROMPT_POLLS = {"STATUS_DEPLOY_CI_WAIT_SECONDS": "60", "STATUS_DEPLOY_CI_POLL_SECONDS": "0"}


def assert_nothing_happened(world: World) -> None:
    """Refused before the build: no release, no migration, no unit touched."""
    assert not (world.root / "releases").exists()
    assert world.target("dev") is None and world.target("live") is None
    calls = world.calls()
    assert not [c for c in calls if " sync " in c or "alembic" in c or c.startswith("sudo ")]


class TestTheCIGate:
    """#11: a live deploy needs the commit's CI green; dev is never gated.

    The run that decides is the newest push run of ci.yml on main for exactly
    this commit. 291604b also has a workflow_dispatch run on its branch, and
    its check runs list every job twice.
    """

    @staticmethod
    def other_and_push(world: World, event: str = "workflow_dispatch", branch: str = "feature"):
        """291604b's shape: a newer run of another kind (8), and the push run on main (7)."""
        sha = world.main[-1]
        later, earlier = "2026-10-01T02:00:00Z", "2026-10-01T01:00:00Z"
        other = ci_run(8, sha, event=event, branch=branch, created=later)
        world.ci_answers([other, ci_run(7, sha, created=earlier)])

    # Each differs from the push run on main in one way only, so each half of
    # the filter is tested on its own (CR 7).
    OTHER_RUNS = pytest.mark.parametrize(
        ("event", "branch"),
        [("workflow_dispatch", "main"), ("pull_request", "main"), ("push", "feature")],
        ids=["dispatch-on-main", "fork-pr-from-main", "push-elsewhere"],
    )

    def test_a_live_deploy_asks_about_this_commit_as_pushed_to_main(self, world):
        assert_ok(world.run())
        runs, jobs = world.github_calls()
        assert "/repos/CannObserv/status/actions/workflows/ci.yml/runs?" in runs
        for param in (f"head_sha={world.main[-1]}", "event=push", "branch=main"):
            assert param in runs
        assert jobs.endswith("/repos/CannObserv/status/actions/runs/1/jobs?per_page=100")

    def test_a_pass_is_logged_with_its_run_before_anything_is_built(self, world):
        assert_ok(world.run())
        calls = world.calls()
        passed = index_of(calls, "CI passed")
        assert world.build(world.main[-1]) in calls[passed]
        assert "https://github.test/runs/1" in calls[passed]
        assert calls[passed].startswith("logger -t status-deploy ")
        assert passed < index_of(calls, " sync ")

    def test_a_failed_job_is_refused_by_name_before_anything_is_built(self, world):
        world.ci_answers([ci_run(7, world.main[-1])])
        world.ci_jobs(7, test="failure")
        result = world.run()
        assert result.returncode == 1
        assert "test (failure)" in result.stderr
        assert "lint (" not in result.stderr and "migrations (" not in result.stderr
        assert "https://github.test/runs/7" in result.stderr
        assert_nothing_happened(world)

    @pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "absent"])
    def test_a_job_that_did_not_succeed_is_not_a_pass(self, world, conclusion):
        """A skipped job leaves the run's own conclusion 'success'."""
        world.ci_answers([ci_run(7, world.main[-1])])
        world.ci_jobs(7, migrations=conclusion)
        result = world.run()
        assert result.returncode == 1
        assert "migrations (" in result.stderr
        assert_nothing_happened(world)

    def test_a_failed_job_the_checkout_does_not_know_is_refused(self, world):
        """CR 6: CI_JOBS comes from the checkout running deploy.sh, which may
        predate a job added to ci.yml since. Every job the run lists counts."""
        world.ci_answers([ci_run(7, world.main[-1], conclusion="failure")])
        world.ci_jobs(7, e2e="failure")
        result = world.run()
        assert result.returncode == 1
        assert "e2e (failure)" in result.stderr
        assert_nothing_happened(world)

    def test_a_cancelled_run_says_so(self, world):
        """CR 6: a run cancelled while queued (ci.yml's concurrency group) lists no jobs."""
        world.ci_answers([ci_run(7, world.main[-1], conclusion="cancelled")])
        world.ci_jobs(7, lint="absent", test="absent", migrations="absent")
        result = world.run()
        assert result.returncode == 1
        assert "run concluded cancelled" in result.stderr
        assert "migrations (not in the run)" in result.stderr
        # CR 12: a dispatch on main cancels it too, with no newer commit and
        # nothing to fix. Re-running it is the remedy.
        assert "re-run it" in result.stderr
        assert "fix it on main" not in result.stderr
        assert_nothing_happened(world)

    def test_a_run_that_did_not_conclude_success_is_refused_whatever_its_jobs(self, world):
        world.ci_answers([ci_run(7, world.main[-1], conclusion="failure")])
        world.ci_jobs(7)
        result = world.run()
        assert result.returncode == 1
        assert "run concluded failure" in result.stderr
        assert_nothing_happened(world)

    def test_a_run_from_before_a_job_was_added_still_passes(self, world):
        """CR 6: a rollback's run has the jobs ci.yml had then; CI_JOBS is the floor."""
        world.ci_answers([ci_run(7, world.main[-1])])
        world.ci_jobs(7)  # lint, test, migrations: no job added since
        assert_ok(world.run())

    def test_a_run_still_going_when_the_wait_ends_is_refused(self, world):
        world.ci_answers([ci_run(7, world.main[-1], status="in_progress")])
        result = world.run(STATUS_DEPLOY_CI_WAIT_SECONDS="0")
        assert result.returncode == 1
        assert "in_progress" in result.stderr
        assert "https://github.test/runs/7" in result.stderr
        assert not [c for c in world.github_calls() if "/jobs" in c]
        assert_nothing_happened(world)

    def test_a_run_that_finishes_within_the_wait_is_deployed(self, world):
        sha = world.main[-1]
        world.ci_answers([ci_run(7, sha, status="in_progress")], [ci_run(7, sha)])
        started = time.monotonic()
        result = world.run(**PROMPT_POLLS)
        assert_ok(result)
        assert time.monotonic() - started < 15, "polled every 30 s, not as told"
        # CR 3: the wait holds the lock for up to 10 minutes; say where to watch.
        assert "waiting for CI" in result.stderr
        waiting = next(line for line in result.stderr.splitlines() if "waiting for CI" in line)
        assert "https://github.test/runs/7" in waiting
        assert len([c for c in world.github_calls() if "/runs?" in c]) == 2

    def test_the_tip_just_pushed_waits_for_its_run_to_appear(self, world):
        """GitHub queues the push run a few seconds after the push."""
        world.ci_answers([], [ci_run(7, world.main[-1])])
        assert_ok(world.run(**PROMPT_POLLS))

    def test_the_tip_with_no_run_when_the_wait_ends_is_refused(self, world):
        world.ci_answers([])
        result = world.run(STATUS_DEPLOY_CI_WAIT_SECONDS="0")
        assert result.returncode == 1
        assert "no CI run" in result.stderr
        assert "--skip-ci" in result.stderr
        assert_nothing_happened(world)

    def test_a_commit_behind_the_tip_with_no_run_is_refused_at_once(self, world):
        """GitHub runs CI on the newest commit of a push only: it never will."""
        world.ci_answers([])
        result = world.run(world.main[0])  # the default wait, 600 s
        assert result.returncode == 1
        assert "no CI run" in result.stderr
        assert "newest commit of each push" in result.stderr
        # CR 8: pushed seconds ago, with another push after it, its run may be coming.
        assert "not be listed yet" in result.stderr
        assert len(world.github_calls()) == 1
        assert_nothing_happened(world)

    def test_a_run_for_another_commit_is_not_this_ones(self, world):
        world.ci_answers([ci_run(7, world.main[0])])
        result = world.run(STATUS_DEPLOY_CI_WAIT_SECONDS="0")
        assert result.returncode == 1
        assert "no CI run" in result.stderr

    @OTHER_RUNS
    def test_only_the_push_run_on_main_decides(self, world, event, branch):
        """A failed run of another kind, newer, changes nothing."""
        self.other_and_push(world, event, branch)
        world.ci_jobs(8, test="failure")
        world.ci_jobs(7)
        assert_ok(world.run())
        assert world.github_calls()[-1].endswith("/actions/runs/7/jobs?per_page=100")

    @OTHER_RUNS
    def test_a_green_run_of_another_kind_does_not_pass_a_failed_push_run(
        self, world, event, branch
    ):
        self.other_and_push(world, event, branch)
        world.ci_jobs(8)
        world.ci_jobs(7, lint="failure")
        result = world.run()
        assert result.returncode == 1
        assert "lint (failure)" in result.stderr
        assert "https://github.test/runs/7" in result.stderr

    def test_of_two_push_runs_on_main_the_newest_decides(self, world):
        """main moved back and forward again: the latest verdict counts."""
        sha = world.main[-1]
        world.ci_answers(
            [
                ci_run(7, sha, created="2026-10-01T01:00:00Z"),
                ci_run(9, sha, created="2026-10-01T03:00:00Z"),
                ci_run(8, sha, created="2026-10-01T02:00:00Z"),
            ]
        )
        world.ci_jobs(7, test="failure")
        world.ci_jobs(8, test="failure")
        world.ci_jobs(9)
        assert_ok(world.run())
        assert world.github_calls()[-1].endswith("/actions/runs/9/jobs?per_page=100")

    def test_github_refusing_refuses_the_deploy_with_its_message(self, world):
        """Unauthenticated: 60 requests an hour per IP address."""
        result = world.run(FAKE_CI_ERROR="API rate limit exceeded for 192.0.2.1.")
        assert result.returncode == 1
        assert "API rate limit exceeded for 192.0.2.1." in result.stderr
        assert "--skip-ci" in result.stderr
        assert_nothing_happened(world)

    @pytest.mark.parametrize("body", ["<html>unicorn</html>", '{"total_count":0}', "", " \n"])
    def test_a_runs_answer_that_is_not_what_was_expected_refuses_the_deploy(self, world, body):
        """#21: before github() refused it, an empty body read as no run and waited the
        default 600 s; no wait here keeps a regression from hanging the suite."""
        world.ci_answers(body)
        result = world.run(STATUS_DEPLOY_CI_WAIT_SECONDS="0")
        assert result.returncode == 1
        assert "not the JSON expected" in result.stderr
        assert "nothing was built" in result.stderr and "--skip-ci" in result.stderr
        assert_nothing_happened(world)

    @pytest.mark.parametrize("body", ["<html>unicorn</html>", '{"total_count":0}', "", " \n"])
    def test_a_jobs_answer_that_is_not_what_was_expected_refuses_the_deploy(self, world, body):
        """CR 10: jq alone exits 5 and says neither that nothing was built nor --skip-ci.
        #21: an empty body passed: jq given no input prints nothing and exits 0, so no
        job looked failed."""
        world.ci_answers([ci_run(7, world.main[-1])])
        (world.ci / "jobs-7.json").write_text(body)
        result = world.run()
        assert result.returncode == 1
        assert "not the JSON expected" in result.stderr
        assert "nothing was built" in result.stderr and "--skip-ci" in result.stderr
        assert_nothing_happened(world)

    def test_skip_ci_deploys_without_asking_and_logs_it_first(self, world):
        world.ci_answers([ci_run(7, world.main[-1])])
        world.ci_jobs(7, test="failure")
        assert_ok(world.run("--skip-ci"))
        assert not world.github_calls()
        calls = world.calls()
        skipped = index_of(calls, "--skip-ci")
        assert calls[skipped].startswith("logger -t status-deploy live: ")
        assert world.build(world.main[-1]) in calls[skipped]
        assert skipped < index_of(calls, " sync ")

    def test_dev_is_never_gated(self, world):
        world.ci_answers([ci_run(7, world.main[-1])])
        world.ci_jobs(7, test="failure")
        assert_ok(world.run("--dev"))
        assert not world.github_calls()

    def test_skip_ci_with_dev_is_refused(self, world):
        result = world.run("--dev", "--skip-ci")
        assert result.returncode == 1
        assert "dev is never gated" in result.stderr
        assert_nothing_happened(world)

    def test_help_names_skip_ci(self, world):
        result = world.run("--help")
        assert_ok(result)
        assert "--skip-ci" in result.stdout

    def test_the_jobs_the_gate_requires_are_ones_ci_yml_runs_on_a_push_to_main(self):
        """A job renamed in ci.yml must change the gate too. A job added need
        not: every job a run lists counts (CR 6), and tests/ci holds ci.yml to
        lint, test and migrations at least."""
        required = re.search(r"^CI_JOBS=\((.*)\)$", DEPLOY.read_text(), re.M)
        assert required, "deploy.sh names its jobs in CI_JOBS=(...)"
        workflow = yaml.safe_load(CI_WORKFLOW.read_text())
        jobs = {job.get("name", key): job for key, job in workflow["jobs"].items()}
        assert set(required.group(1).split()) <= set(jobs)
        # CR 14: GitHub names a matrix job "test (3.12)" and a reusable workflow's
        # "test / ...": the gate would find no "test" and refuse every live deploy.
        for name in required.group(1).split():
            assert "strategy" not in jobs[name] and "uses" not in jobs[name], name
        # PyYAML reads a bare `on:` as True; a quoted one stays "on" (CR 4).
        assert "main" in workflow.get(True, workflow.get("on"))["push"]["branches"]


class TestReleases:
    def test_a_built_release_is_reused(self, world):
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run(world.main[-1]))
        world.reset_log()
        assert_ok(world.run(world.main[0]))
        assert not [c for c in world.calls() if " sync " in c]

    def test_an_unlinked_release_whose_interpreter_is_gone_is_rebuilt(self, world):
        """CR 9: REVISION says the build finished, not that its venv still runs."""
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run(world.main[1]))
        world.reset_log()
        result = world.run(world.main[0], FAKE_BROKEN_VENV="1")
        assert_ok(result)
        assert [c for c in world.calls() if " sync " in c]
        assert "probe: no interpreter" in result.stderr, "why it rebuilt"

    def test_a_linked_release_whose_interpreter_is_gone_is_never_deleted(self, world):
        """CR 15: live runs from it. Rebuilding it in place would pull the code
        out from under the running sweep and API, unverified, and a failed sync
        would leave live with no release at all."""
        assert_ok(world.run(world.main[0]))
        release = world.root / "releases" / world.build(world.main[0])
        world.reset_log()
        result = world.run("--dev", world.main[0], FAKE_BROKEN_VENV="1")
        assert result.returncode != 0
        assert (release / "REVISION").exists()
        assert (release / ".venv").exists()
        assert not [c for c in world.calls() if " sync " in c]
        assert "probe: no interpreter" in result.stderr
        assert "live" in result.stderr

    def test_an_interrupted_build_is_rebuilt(self, world):
        """No REVISION means the build never finished: start it again."""
        partial = world.root / "releases" / world.build(world.main[-1])
        with world.as_root():
            partial.parent.mkdir()
        world.owned.write_text(world.owned.read_text() + f"D {partial.parent}\n")
        with world.as_root():
            partial.mkdir()
        (partial / "half-written").write_text("x")
        assert_ok(world.run())
        assert not (partial / "half-written").exists()
        assert (partial / "REVISION").exists()

    def test_a_failed_sync_switches_nothing(self, world):
        result = world.run(FAKE_SYNC_FAIL="1")
        assert result.returncode != 0
        assert world.target("dev") is None and world.target("live") is None

    def test_old_releases_are_pruned_but_never_a_linked_one(self, world):
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run("--dev", "origin/feature"))
        assert_ok(world.run(world.main[1]))
        assert_ok(world.run(world.main[2], STATUS_DEPLOY_KEEP="1"))
        kept = {p.name for p in (world.root / "releases").iterdir()}
        assert kept == {world.build(world.main[2])}

    def test_a_failed_prune_does_not_fail_a_deploy_that_succeeded(self, world):
        """CR 10: both targets are verified by then; say so, and exit 0."""
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run(world.main[1]))
        result = world.run(world.main[2], STATUS_DEPLOY_KEEP="1", FAKE_RM_FAIL="1")
        assert_ok(result)
        assert "prune" in result.stderr
        # CR 26: a half-pruned release reads as interrupted, never as complete.
        half = world.root / "releases" / world.build(world.main[0])
        assert half.exists() and not (half / "REVISION").exists()

    def test_a_linked_release_without_revision_is_never_deleted(self, world):
        """CR 27: every rebuild path checks the links, not only the broken-venv one."""
        assert_ok(world.run(world.main[0]))
        release = world.root / "releases" / world.build(world.main[0])
        release.chmod(0o755)
        (release / "REVISION").chmod(0o644)
        (release / "REVISION").unlink()
        world.reset_log()
        result = world.run("--dev", world.main[0])
        assert result.returncode != 0
        assert (release / "version.txt").exists()
        assert not [c for c in world.calls() if " sync " in c]

    def test_a_linked_release_deleted_by_hand_is_built_again(self, world):
        """CR 32: nothing runs from a directory that is gone; building it is the fix."""
        assert_ok(world.run(world.main[0]))
        release = world.root / "releases" / world.build(world.main[0])
        for path in [release, *release.rglob("*")]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)
        with world.as_root():
            shutil.rmtree(release)
        world.disown(release)
        assert_ok(world.run("--dev", world.main[0]))
        assert (release / "REVISION").exists()

    def test_a_release_root_does_not_own_is_rebuilt(self, world):
        """#14: built before root owned releases, or handed back by hand, so
        exedev may have changed it. It replaces CR 26's writable check: REVISION
        is now written after the chmod and the chown, so a release cut short
        between them has none."""
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run(world.main[1]))
        release = world.root / "releases" / world.build(world.main[0])
        world.disown(release)
        world.reset_log()
        result = world.run(world.main[0])
        assert_ok(result)
        assert [c for c in world.calls() if " sync " in c]
        assert "not root's" in result.stderr, "why it rebuilt"
        assert world.root_owned(release)

    def test_a_linked_release_root_does_not_own_is_never_rebuilt(self, world):
        """CR 15 holds for it too: live runs from it."""
        assert_ok(world.run(world.main[0]))
        release = world.root / "releases" / world.build(world.main[0])
        world.disown(release)
        world.reset_log()
        result = world.run("--dev", world.main[0])
        assert result.returncode != 0
        assert (release / "REVISION").exists()
        assert not [c for c in world.calls() if " sync " in c]
        assert "not root's" in result.stderr and "live" in result.stderr

    def test_the_reuse_probe_imports_the_dependencies_not_just_python(self, world):
        """CR 26: `import sys` passes on an empty venv."""
        assert_ok(world.run(world.main[0]))
        world.reset_log()
        assert_ok(world.run(world.main[0]))
        (probe,) = [c for c in world.calls() if "python -c" in c]
        assert "import fastapi" in probe

    def test_an_absolute_link_made_by_hand_still_protects_its_release(self, world):
        """CR 28: every check compared the relative spelling deploy.sh writes."""
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run("--dev", world.main[1]))
        live = world.root / "live"
        target = world.root / "releases" / world.build(world.main[0])
        with world.as_root():
            live.unlink()
            live.symlink_to(target)
        assert_ok(world.run("--dev", world.main[2], STATUS_DEPLOY_KEEP="1"))
        assert target.exists(), "prune deleted the release live runs"

    def test_pruning_spares_what_dev_still_runs(self, world):
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run("--dev", "origin/feature"))
        assert_ok(world.run("--dev", "origin/feature", STATUS_DEPLOY_KEEP="1"))
        kept = {p.name for p in (world.root / "releases").iterdir()}
        assert kept == {world.build(world.main[0]), world.build(world.feature)}


class TestOwnership:
    """#14: root owns the deploy root, releases/ and every finished release.

    exedev has passwordless sudo, so this is no boundary against exedev. What it
    does: changing what a unit runs takes sudo, which journals the command, where a
    `chmod u+w` was silent. Every test in this file runs against a root exedev
    cannot write, so the switch, the switch back and the prune are covered too.
    """

    def test_a_built_release_is_roots_throughout(self, world):
        assert_ok(world.run())
        release = world.root / "releases" / world.build(world.main[-1])
        assert world.root_owned(world.root / "releases")
        assert world.root_owned(release)
        assert world.root_owned(release / "REVISION")
        assert world.root_owned(release / ".venv")

    def test_root_takes_the_release_before_revision_marks_it_finished(self, world):
        """R4: REVISION last, so a build cut short in the chown is rebuilt."""
        assert_ok(world.run())
        release = world.root / "releases" / world.build(world.main[-1])
        calls = world.calls()
        chown = calls.index(f"sudo chown -R root:root {release}")
        assert chown < calls.index(f"sudo tee {release}/REVISION")
        # By sudo: the ledger cannot catch a plain chmod by the file's owner.
        assert f"sudo chmod 444 {release}/REVISION" in calls
        assert (release / "REVISION").stat().st_mode & 0o777 == 0o444

    def test_the_release_is_built_where_it_runs_by_exedev(self, world):
        """R4: a uv venv embeds its path, so the directory is made for exedev
        at its final path, and only the finished tree goes to root."""
        assert_ok(world.run())
        release = world.root / "releases" / world.build(world.main[-1])
        me, group = pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name
        calls = world.calls()
        made = calls.index(f"sudo install -d -m 755 -o {me} -g {group} {release}")
        assert made < index_of(calls, " sync ") < calls.index(f"sudo chown -R root:root {release}")

    def test_the_venv_is_its_own_copy_not_the_uv_caches(self, world):
        """Hardlinked, a chown would reach the cache, and an edit in a release
        would edit the cache and every venv sharing the file."""
        assert_ok(world.run())
        (sync,) = [c for c in world.calls() if " sync " in c]
        assert "--link-mode copy" in sync

    def test_a_release_directory_root_will_not_make_says_so(self, world):
        """CR 4: set -e ended the deploy here with no word of why."""
        build = world.build(world.main[-1])
        result = world.run(FAKE_INSTALL_FAIL=build)
        assert result.returncode == 1
        assert f"cannot make releases/{build}" in result.stderr
        assert "nothing switched" in result.stderr
        assert world.target("dev") is None

    def test_a_failed_chown_switches_nothing_and_leaves_no_revision(self, world):
        result = world.run(FAKE_CHOWN_FAIL="1")
        assert result.returncode == 1
        assert world.target("dev") is None and world.target("live") is None
        release = world.root / "releases" / world.build(world.main[-1])
        assert not (release / "REVISION").exists()
        assert f"cannot hand {world.build(world.main[-1])} to root" in result.stderr
        assert "nothing switched" in result.stderr

    def test_a_deploy_root_root_does_not_own_is_refused_with_the_fix(self, world):
        """As on co-status before #14: exedev's, so exedev could repoint live."""
        world.disown(world.root)
        result = world.run()
        assert result.returncode == 1
        assert f"sudo chown root:root {world.root}" in result.stderr
        assert not [c for c in world.calls() if " sync " in c or c.startswith("sudo ")]

    def test_a_releases_directory_root_does_not_own_is_refused_with_the_fix(self, world):
        assert_ok(world.run(world.main[0]))
        world.disown(world.root / "releases")
        world.reset_log()
        result = world.run()
        assert result.returncode == 1
        assert f"sudo chown root:root {world.root}/releases" in result.stderr
        assert not [c for c in world.calls() if " sync " in c or c.startswith("sudo ")]

    def test_a_group_writable_deploy_root_is_refused(self, world):
        world.root.chmod(0o575)
        result = world.run()
        assert result.returncode == 1
        assert f"sudo chmod 755 {world.root}" in result.stderr
        assert not [c for c in world.calls() if c.startswith("sudo ")]

    def test_a_deploy_root_that_is_a_link_is_refused_as_one(self, world):
        """CR 3: find -perm reads a link's own mode, 777, so this was refused as
        "writable by more than root", which sends the operator to chmod."""
        real = world.tmp / "real-srv"
        world.root.rename(real)
        world.root.symlink_to(real)
        result = world.run()
        assert result.returncode == 1
        assert "a link" in result.stderr and "writable" not in result.stderr
        assert not [c for c in world.calls() if c.startswith("sudo ")]

    def test_a_directory_the_sudo_stub_makes_as_root_is_unwritable_at_once(self, world):
        """CR 8: the harness relocked only what was root's before the command,
        so a new root directory stayed writable until the next sudo."""
        made = world.root / "releases"
        env = {**os.environ, "FAKE_LOG": str(world.log), "FAKE_ROOT_OWNED": str(world.owned)}
        subprocess.run(
            [str(world.stubs / "sudo"), "install", "-d", "-m", "755", str(made)],
            env=env,
            check=True,
        )
        assert world.root_owned(made)
        assert not made.stat().st_mode & 0o222

    def test_a_releases_link_to_nowhere_is_refused_as_a_link(self, world):
        """CR 12: -e is false for a dangling link, so the link check never ran
        and install -d failed on it with no word of why."""
        with world.as_root():
            (world.root / "releases").symlink_to(world.tmp / "nowhere")
        result = world.run()
        assert result.returncode == 1
        assert f"{world.root}/releases is a link" in result.stderr
        assert not [c for c in world.calls() if " sync " in c or c.startswith("sudo ")]

    def test_a_missing_releases_directory_is_made_by_root(self, world):
        assert_ok(world.run())
        assert f"sudo install -d -m 755 {world.root}/releases" in world.calls()

    def test_the_links_are_replaced_by_root(self, world):
        assert_ok(world.run())
        calls = world.calls()
        for target in ("dev", "live"):
            assert f"sudo mv -Tf {world.root}/{target}.new {world.root}/{target}" in calls

    def test_a_switch_back_is_done_by_root(self, world):
        assert_ok(world.run(world.main[0]))
        world.reset_log()
        result = world.run(FAKE_STALE_PORT="9000")
        assert result.returncode == 1, result.stderr
        assert world.target("live") == f"releases/{world.build(world.main[0])}"
        move = f"sudo mv -Tf {world.root}/live.new {world.root}/live"
        assert world.calls().count(move) == 2, "there and back"

    def test_a_reused_release_is_touched_by_root(self, world):
        """Prune order is last deploy (R13). The ledger cannot catch a plain
        touch: the test's user owns the directory, and an owner may set its
        times. exedev, on the VM, is not the owner."""
        assert_ok(world.run(world.main[0]))
        world.reset_log()
        assert_ok(world.run(world.main[0]))
        release = world.root / "releases" / world.build(world.main[0])
        assert f"sudo touch {release}" in world.calls()

    def test_the_prune_is_done_by_root_revision_first(self, world):
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run(world.main[1]))
        world.reset_log()
        assert_ok(world.run(world.main[2], STATUS_DEPLOY_KEEP="1"))
        old = world.root / "releases" / world.build(world.main[0])
        assert not old.exists()
        calls = world.calls()
        assert calls.index(f"sudo rm -f {old}/REVISION") < calls.index(f"sudo rm -rf {old}")
        assert not world.root_owned(old), "the ledger forgets what was removed"


class TestMigrations:
    def test_a_database_ahead_of_the_release_is_not_migrated(self, world):
        """A rollback: the older Alembic cannot resolve the newer revision."""
        result = world.run(FAKE_SCHEMA="ahead")
        assert_ok(result)
        assert not [c for c in world.calls() if "alembic upgrade" in c]
        assert "ahead" in result.stderr
        # CR 7: or a branch migration nobody merged, which would stop dev
        # rehearsing migrations for good.
        assert "branch" in result.stderr

    @pytest.mark.parametrize("state", ["behind", "unmigrated"])
    def test_a_database_behind_is_migrated(self, world, state):
        assert_ok(world.run(FAKE_SCHEMA=state))
        assert len([c for c in world.calls() if "alembic upgrade" in c]) == 2

    @pytest.mark.parametrize("rc", ["1", "2"], ids=["crash", "unreadable"])
    def test_an_unreadable_schema_state_is_never_migrated(self, world, rc):
        """CR 1: db_safety refusing dev.env's URL must not become an upgrade."""
        result = world.run(FAKE_SCHEMA_CRASH=rc)
        assert result.returncode != 0
        assert not [c for c in world.calls() if "alembic upgrade" in c]
        assert world.target("dev") is None

    def test_a_failed_migration_switches_nothing(self, world):
        result = world.run(FAKE_MIGRATE_RC="1")
        assert result.returncode != 0
        assert world.target("dev") is None
        switched = ("sudo ln ", "sudo mv ", "sudo systemctl ", "sudo install -m 644 ")
        assert not [c for c in world.calls() if c.startswith(switched)]


class TestVerification:
    def test_a_live_api_on_the_wrong_build_is_switched_back(self, world):
        assert_ok(world.run(world.main[0]))
        previous = world.target("live")
        world.reset_log()

        result = world.run(FAKE_STALE_PORT="9000")

        assert result.returncode != 0
        assert world.target("live") == previous
        restarts = [c for c in world.calls() if c == "sudo systemctl restart status"]
        assert len(restarts) == 2, "restarted onto the new build, then back"

    def test_a_rollback_clears_the_start_limit_and_proves_the_old_build(self, world):
        """CR 3: a crash-looping release can exhaust StartLimitBurst.

        systemd then refuses the rollback's restart too, and the deploy would
        report "switched back" over a dead API. reset-failed comes first, and
        the old build must answer before the message claims it does.
        """
        assert_ok(world.run(world.main[0]))
        world.reset_log()
        result = world.run(FAKE_STALE_PORT="9000")
        assert result.returncode != 0
        calls = world.calls()
        resets = [i for i, c in enumerate(calls) if c == "sudo systemctl reset-failed status"]
        restarts = [i for i, c in enumerate(calls) if c == "sudo systemctl restart status"]
        # One before each restart: forward (CR 23) and rollback (CR 3).
        assert resets[0] < restarts[0] < resets[1] < restarts[1]
        assert [c for c in calls[restarts[1] :] if c.endswith(":9000/health")]
        # CR 20: the old build itself answered, and the message says which.
        old = world.build(world.main[0])
        assert f"switched back to {old}, which is answering" in result.stderr
        assert "NOT" not in result.stderr

    def test_a_rollback_that_does_not_come_back_says_so(self, world):
        """The old build not answering either is the loudest failure there is."""
        assert_ok(world.run(world.main[0]))
        result = world.run(FAKE_STALE_PORT="9000", FAKE_STALE_ALWAYS="1")
        assert result.returncode == 4, "distinct from a clean rollback's 1 (CR 16)"
        assert "NOT answering" in result.stderr
        logged = [c for c in world.calls() if c.startswith("logger ")]
        assert not [c for c in logged if "rolled back to" in c], "never claim it"
        assert [c for c in logged if "NOT answering" in c]

    def test_a_rollback_proves_the_old_sweep_too(self, world):
        """CR 16: a migration that was not really expand-only breaks the old
        sweep, not the old API; the rollback must find out now, not at the
        next timer pass."""
        assert_ok(world.run(world.main[0]))
        world.reset_log()
        assert world.run(FAKE_STALE_PORT="9000").returncode == 1
        calls = world.calls()
        restarts = [i for i, c in enumerate(calls) if c == "sudo systemctl restart status"]
        passes = [
            i for i, c in enumerate(calls) if c == "sudo systemctl start status-sweep.service"
        ]
        assert len(passes) == 2 and passes[1] > restarts[1]
        logged = [c for c in calls if c.startswith("logger ")]
        assert [c for c in logged if "rolled back to" in c]

    def test_redeploying_the_running_build_has_nothing_to_switch_back_to(self, world):
        """CR 16: previous == new; "switched back" would be a lie."""
        assert_ok(world.run())
        world.reset_log()
        result = world.run(FAKE_STALE_PORT="9000")
        assert result.returncode == 4, "live is left on a build that did not answer (CR 25)"
        assert "already" in result.stderr
        assert "switched back" not in result.stderr

    def test_redeploying_the_running_build_puts_back_a_hand_fixed_unit(self, world):
        """CR 10: only the units changed, so they are what switches back; the
        hand fix is the known-good state, and the copy kept aside its only copy."""
        world.push_deploy(V1)
        assert_ok(world.run())
        (world.units / "status.service").write_text("hand fix\n")
        world.reset_log()

        result = world.run(FAKE_STALE_UNLESS_UNIT_HAS="hand fix")

        assert result.returncode == 1, result.stderr
        assert world.installed("status.service") == "hand fix\n"
        assert "put back" in result.stderr and "which is answering" in result.stderr
        assert "switched back" not in result.stderr, "the link never moved"
        assert world.target("live") == f"releases/{world.build(world.main[-1])}"

    def test_redeploying_the_running_build_on_units_that_fail_too_is_dead(self, world):
        world.push_deploy(V1)
        assert_ok(world.run())
        (world.units / "status.service").write_text("hand edit\n")
        result = world.run(FAKE_STALE_UNLESS_UNIT_HAS="never there")
        assert result.returncode == 4, result.stderr
        assert world.installed("status.service") == "hand edit\n"
        assert "NOT answering" in result.stderr

    def test_a_rollback_restores_a_hand_made_link_exactly(self, world):
        """CR 31: live pointed by hand at somewhere outside releases/ during a
        recovery. Switching back must restore that link, not releases/<name>."""
        assert_ok(world.run(world.main[0]))
        elsewhere = world.tmp / "elsewhere"
        elsewhere.mkdir()
        live = world.root / "live"
        with world.as_root():
            live.unlink()
            live.symlink_to(elsewhere)
        result = world.run(FAKE_STALE_PORT="9000")
        assert result.returncode == 1, "a checkout answers as 'dev', and it did (CR 36)"
        assert "which is answering" in result.stderr
        assert os.readlink(live) == str(elsewhere)
        logged = [c for c in world.calls() if c.startswith("logger ") and " -> " in c]
        assert any(str(elsewhere) in c for c in logged), "the journal names the real old link"

    def test_a_failing_sweep_pass_is_switched_back(self, world):
        assert_ok(world.run(world.main[0]))
        previous = world.target("live")
        result = world.run(
            FAKE_SWEEP_FAIL="status-sweep.service",
            FAKE_SWEEP_FAIL_BUILD=world.build(world.main[-1]),
        )
        assert result.returncode == 1, "the old build's own pass succeeds (CR 24)"
        assert world.target("live") == previous
        assert "status-sweep" in result.stderr
        assert "which is answering" in result.stderr

    def test_an_old_build_whose_sweep_fails_too_is_reported_dead(self, world):
        """CR 24: CR 16's own case. The new API fails; switching back finds the
        old sweep broken as well (a migration that was not expand-only)."""
        assert_ok(world.run(world.main[0]))
        result = world.run(
            FAKE_STALE_PORT="9000",
            FAKE_SWEEP_FAIL="status-sweep.service",
            FAKE_SWEEP_FAIL_BUILD=world.build(world.main[0]),
        )
        assert result.returncode == 4
        assert "NOT answering" in result.stderr

    def test_a_failing_dev_stops_the_deploy_before_live(self, world):
        assert_ok(world.run(world.main[0]))
        live_before = world.target("live")
        result = world.run(
            FAKE_SWEEP_FAIL="status-sweep-dev.service",
            FAKE_SWEEP_FAIL_BUILD=world.build(world.main[-1]),
        )
        assert result.returncode == 1
        assert world.target("live") == live_before
        assert world.target("dev") == live_before

    def test_the_forced_pass_waits_out_one_already_running(self, world):
        """CR 2: `systemctl start` on a oneshot mid-pass merges into that pass.

        A timer pass that started before the switch runs the old release, so
        merging into it would verify the old code. Verified on this host's
        systemd; a pass that starts after the switch runs the new release.
        """
        assert_ok(world.run("--dev", FAKE_SWEEP_BUSY="2"))
        calls = world.calls()
        start = calls.index("sudo systemctl start status-sweep-dev.service")
        polls = [i for i, c in enumerate(calls) if c.startswith("systemctl show")]
        assert len(polls) == 3, "two busy answers, then the idle one"
        assert max(polls) < start

    def test_a_pass_that_never_ends_fails_the_target_and_switches_back(self, world):
        """CR 21: the wait is bounded, and running out of it is a failed verify.

        A wait of 0 gives up at its first poll, however slow the poll. Bash's
        SECONDS counts whole seconds, so a wait of 1 lasted 0–1 s: one busy
        answer used or two, and the switch-back's own wait left to chance (#23).
        """
        assert_ok(world.run(world.main[0]))
        previous = world.target("dev")
        world.reset_log()
        result = world.run(FAKE_SWEEP_BUSY="1", STATUS_DEPLOY_SWEEP_WAIT_SECONDS="0")
        assert result.returncode == 1, result.stderr
        assert result.stderr.count("has been running") == 1
        assert "switched back" in result.stderr
        assert "which is answering" in result.stderr
        assert world.target("dev") == previous
        polls = [c for c in world.calls() if c.startswith("systemctl show")]
        assert len(polls) == 2, "one busy answer forward, then the idle one switching back"

    def test_a_switch_back_that_outlasts_its_own_wait_is_reported_dead(self, world):
        """#23: the switch-back's wait is as bounded as the forward one."""
        assert_ok(world.run(world.main[0]))
        previous = world.target("dev")
        result = world.run(FAKE_SWEEP_BUSY="99", STATUS_DEPLOY_SWEEP_WAIT_SECONDS="0")
        assert result.returncode == 4, result.stderr
        assert result.stderr.count("has been running") == 2
        assert "switched back" in result.stderr
        assert "NOT answering" in result.stderr
        assert world.target("dev") == previous

    def test_the_busy_stub_holds_for_a_later_run_in_the_same_world(self, world):
        """CR 19: the counter was set by a world's first run and kept after."""
        assert_ok(world.run("--dev"))
        world.reset_log()
        assert_ok(world.run("--dev", FAKE_SWEEP_BUSY="2"))
        assert len([c for c in world.calls() if c.startswith("systemctl show")]) == 3

    def test_a_deploy_clears_the_start_limit_before_starting_the_fix(self, world):
        """CR 23: the deploy that fixes a crash loop is the one that hits the limit.

        systemd refuses a start past StartLimitBurst, manual ones included, so
        the forward restart needs reset-failed as much as the rollback does.
        """
        assert_ok(world.run("--dev"))
        calls = world.calls()
        reset = calls.index("sudo systemctl reset-failed status-dev")
        assert reset < calls.index("sudo systemctl restart status-dev")

    def test_a_first_deploy_that_fails_has_nothing_to_return_to(self, world):
        result = world.run(FAKE_STALE_PORT="9001")
        assert result.returncode == 4, "dev is left on a build that did not answer (CR 25)"
        assert "no previous release" in result.stderr


class TestOperation:
    def test_no_restart_links_and_migrates_but_restarts_no_unit(self, world):
        """First-time setup: nothing is enabled yet. A release with units
        installs them (#18): TestUnits, test_no_restart_installs_and_reloads_*."""
        assert_ok(world.run("--no-restart"))
        assert world.target("live") == f"releases/{world.build(world.main[-1])}"
        assert not [c for c in world.calls() if c.startswith(("sudo systemctl", "curl http://"))]
        assert [c for c in world.calls() if "alembic upgrade" in c]

    def test_no_restart_is_refused_once_a_target_runs_a_release(self, world):
        """CR 4: the sweeps resolve the link every pass, so a relink goes live
        within 60 s whatever the flag says, and unverified."""
        assert_ok(world.run(world.main[0]))
        before = world.target("live")
        result = world.run("--no-restart")
        assert result.returncode != 0
        assert "first deploy" in result.stderr
        assert world.target("live") == before
        # CR 17: a first deploy that failed half way retries by removing the link.
        assert f"sudo rm {world.root}/dev" in result.stderr

    def test_a_concurrent_deploy_is_refused(self, world):
        """The lock is the deploy root itself (#14): exedev cannot create a
        file in a directory root owns."""
        held = os.open(world.root, os.O_RDONLY)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = world.run()
        finally:
            os.close(held)
        assert result.returncode != 0
        assert "another deploy" in result.stderr
        # CR 11: the lock is taken before the CI gate, which may wait 10 minutes.
        assert not world.github_calls()

    def test_the_lock_leaves_nothing_behind(self, world):
        assert_ok(world.run())
        assert sorted(p.name for p in world.root.iterdir()) == ["dev", "live", "releases"]

    def test_a_missing_root_says_how_to_create_it(self, world):
        world.root.rmdir()
        result = world.run()
        assert result.returncode != 0
        assert f"sudo install -d -m 755 {world.root}" in result.stderr

    def test_a_missing_env_file_is_refused(self, world):
        (world.etc / "dev.env").unlink()
        result = world.run()
        assert result.returncode != 0
        assert "dev.env" in result.stderr

    def test_a_url_missing_from_its_env_file_is_not_taken_from_the_shell(self, world):
        """CR 5: after `. scripts/load_env.sh` the shell exports both URLs.

        The unit reads only the file, so the deploy must too: falling back on
        the caller's environment migrates a database the unit never opens.
        """
        (world.etc / "dev.env").write_text("# DEV_DATABASE_URL forgotten\n")
        result = world.run(
            DEV_DATABASE_URL="postgresql+asyncpg://u@h/from_the_shell_dev",
            DATABASE_URL="postgresql+asyncpg://u@h/status",
        )
        assert result.returncode != 0
        assert not [c for c in world.calls() if "from_the_shell" in c]
        assert "no database URL for dev" in result.stderr

    def test_run_from_outside_a_checkout_says_where_to_run_it(self, world):
        """CR 11: /srv/status/live/scripts/deploy.sh exists too, with no .git."""
        release_scripts = world.tmp / "release" / "scripts"
        release_scripts.mkdir(parents=True)
        shutil.copy(DEPLOY, release_scripts / "deploy.sh")
        result = subprocess.run(
            [str(release_scripts / "deploy.sh")],
            env={
                "PATH": f"{world.stubs}:{os.environ['PATH']}",
                "STATUS_DEPLOY_ROOT": str(world.root),
                "STATUS_DEPLOY_ENV_DIR": str(world.etc),
                "FAKE_LOG": str(world.log),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert "from a checkout" in result.stderr

    def test_no_tailnet_address_stops_the_deploy_before_anything_switches(self, world):
        """CR 34: verification could never pass, so the rollback would report a
        serving old build as NOT answering. Refuse up front instead."""
        assert_ok(world.run(world.main[0]))
        before = world.target("live")
        bind = world.checkout / "scripts" / "tailnet_bind.sh"
        bind.write_text("#!/usr/bin/env bash\nexit 1\n")
        world.reset_log()
        result = world.run()
        assert result.returncode == 1
        assert "tailnet" in result.stderr
        assert world.target("live") == before
        assert not [c for c in world.calls() if "alembic upgrade" in c or c.startswith("sudo ")]

    def test_an_unknown_flag_is_refused(self, world):
        assert world.run("--live-only").returncode != 0

    def test_root_is_refused(self):
        """exedev builds (R13): as root, uv would build with root's cache and
        interpreters, which the units cannot read. Root only takes the result (#14)."""
        assert '"$(id -u)" -eq 0' in DEPLOY.read_text()


V1 = {
    "status.service": "[Service]\n# live API v1\n",
    "status-sweep.service": "[Service]\n# live sweep v1\n",
    "status-sweep.timer": "[Timer]\n# live timer v1\n",
    "status-dev.service": "[Service]\n# dev API v1\n",
    "status-sweep-dev.timer": "[Timer]\n# dev timer v1\n",
}


def installs(calls: list[str]) -> list[str]:
    """The unit names `sudo install` wrote, in order; not the release directories (#14)."""
    return [Path(c.split()[-1]).name for c in calls if c.startswith("sudo install -m 644 ")]


class TestUnits:
    """#18: units reach /etc/systemd/system on deploy, with the release they run."""

    def test_each_target_installs_its_units_from_its_release(self, world):
        world.push_deploy(V1)
        assert_ok(world.run())
        for name, body in V1.items():
            assert world.installed(name) == body, name
            assert (world.units / name).stat().st_mode & 0o777 == 0o644

    def test_units_switch_with_the_link_before_the_restart(self, world):
        """Verification then covers the units too: the restart and the forced
        pass run what was just installed."""
        world.push_deploy(V1)
        assert_ok(world.run())
        calls = world.calls()
        for target, unit, api in (
            ("dev", "status-dev.service", "status-dev"),
            ("live", "status.service", "status"),
        ):
            switched = index_of(calls, f"logger -t status-deploy {target} -> ")
            installed = index_of(calls, f"/{unit}")
            reload = next(i for i, c in enumerate(calls) if i > installed and "daemon-reload" in c)
            assert switched < installed < reload < calls.index(f"sudo systemctl restart {api}")

    def test_dev_alone_installs_dev_units_alone(self, world):
        world.push_deploy(V1)
        assert_ok(world.run("--dev"))
        assert sorted(installs(world.calls())) == ["status-dev.service", "status-sweep-dev.timer"]
        assert world.installed("status.service") is None

    def test_a_branch_on_dev_rehearses_its_unit_edit_and_live_keeps_its_own(self, world):
        world.push_deploy(V1)
        assert_ok(world.run())
        world.push_deploy(
            {"status-dev.service": "dev v2\n", "status.service": "live v2\n"}, "feature"
        )
        assert_ok(world.run("--dev", "origin/feature"))
        assert world.installed("status-dev.service") == "dev v2\n"
        assert world.installed("status.service") == V1["status.service"]

    def test_unchanged_units_need_neither_sudo_nor_a_reload(self, world):
        world.push_deploy(V1)
        assert_ok(world.run())
        world.push_deploy({"status.service": V1["status.service"]})  # a commit, units as they were
        world.push_deploy({})
        world.reset_log()
        assert_ok(world.run())
        assert not installs(world.calls())
        assert not [c for c in world.calls() if "daemon-reload" in c]

    def test_only_the_units_that_differ_are_installed_with_one_reload_per_target(self, world):
        world.push_deploy(V1)
        assert_ok(world.run())
        world.push_deploy({"status.service": "live API v2\n"})
        world.reset_log()
        assert_ok(world.run())
        assert installs(world.calls()) == ["status.service"]
        assert len([c for c in world.calls() if "daemon-reload" in c]) == 1
        assert world.installed("status.service") == "live API v2\n"

    def test_a_changed_timer_is_restarted_so_it_rearms(self, world):
        world.push_deploy(V1)
        assert_ok(world.run())
        world.push_deploy({"status-sweep.timer": "[Timer]\n# v2\n"})
        world.reset_log()
        assert_ok(world.run())
        try_restarts = [c for c in world.calls() if "try-restart" in c]
        assert try_restarts == ["sudo systemctl try-restart status-sweep.timer"]

    def test_a_timer_that_will_not_restart_fails_the_target_and_switches_back(self, world):
        """CR 1: a broken status-sweep.timer is nothing watching for silence;
        the deploy must not call that verified."""
        world.push_deploy(V1)
        assert_ok(world.run())
        previous = world.target("live")
        world.push_deploy({"status-sweep.timer": "[Timer]\n# broken\n"})

        result = world.run(FAKE_TRY_RESTART_FAIL="status-sweep.timer")

        assert result.returncode == 1, result.stderr
        assert "try-restart status-sweep.timer" in result.stderr
        assert world.target("live") == previous
        assert world.installed("status-sweep.timer") == V1["status-sweep.timer"]

    def test_a_new_unit_is_installed_and_named_with_how_to_enable_it(self, world):
        """Enabling is a decision (#12: its healthchecks.io check comes first)."""
        world.push_deploy(V1)
        assert_ok(world.run())
        world.push_deploy({"status-drift.timer": "[Timer]\n"})
        world.reset_log()
        result = world.run()
        assert_ok(result)
        assert world.installed("status-drift.timer") == "[Timer]\n"
        assert "sudo systemctl enable --now status-drift.timer" in result.stderr
        assert not [c for c in world.calls() if " enable " in c]
        logged = [c for c in world.calls() if c.startswith("logger ") and "units" in c]
        assert any("status-drift.timer (new)" in c for c in logged)

    def test_each_new_unit_is_marked_new_and_only_those(self, world):
        """CR 5: the journal line is the record of what a deploy installed."""
        world.push_deploy(V1)
        assert_ok(world.run())
        world.push_deploy(
            {"status.service": "v2\n", "status-drift.service": "x\n", "status-drift.timer": "y\n"}
        )
        world.reset_log()
        assert_ok(world.run())
        (line,) = [c for c in world.calls() if c.startswith("logger ") and " units from " in c]
        assert line.endswith(
            ": status.service status-drift.service (new) status-drift.timer (new)"
        ), line

    def test_a_hand_edited_unit_is_replaced_and_named(self, world):
        world.push_deploy(V1)
        (world.units / "status.service").write_text("edited by hand\n")
        result = world.run()
        assert_ok(result)
        assert world.installed("status.service") == V1["status.service"]
        assert "status.service" in result.stderr

    def test_a_unit_the_release_lacks_is_left_alone(self, world):
        """A rollback to a build from before a unit existed (#12's 203/EXEC)."""
        (world.units / "status-drift.service").write_text("installed\n")
        world.push_deploy(V1)
        assert_ok(world.run())
        assert world.installed("status-drift.service") == "installed\n"

    def test_a_rollback_installs_the_older_units(self, world):
        old = world.push_deploy(V1)
        world.push_deploy({"status.service": "v2\n", "status-drift.timer": "[Timer]\n"})
        assert_ok(world.run())
        assert world.installed("status.service") == "v2\n"
        assert_ok(world.run(old))
        assert world.installed("status.service") == V1["status.service"]
        assert world.installed("status-drift.timer") == "[Timer]\n", "the release lacks it"

    def test_no_restart_installs_and_reloads_but_starts_nothing(self, world):
        """First-time setup: no `sudo cp` list, only the `enable --now` lines."""
        world.push_deploy(V1)
        assert_ok(world.run("--no-restart"))
        assert all(world.installed(name) == body for name, body in V1.items())
        assert [c for c in world.calls() if "daemon-reload" in c]
        assert not [c for c in world.calls() if re.search(r"systemctl (re)?start ", c)]

    def test_no_restart_puts_back_what_it_replaced_when_an_install_fails(self, world):
        """CR 3: the copies kept aside go with the deploy; restore before dying.
        Glob order under the C locale: status-sweep.service before status.service."""
        world.push_deploy(V1)
        (world.units / "status-sweep.service").write_text("installed\n")
        result = world.run("--no-restart", FAKE_INSTALL_FAIL="status.service")
        assert result.returncode == 1, result.stderr
        assert "did not install" in result.stderr
        assert world.installed("status-sweep.service") == "installed\n"

    def test_a_failed_verify_puts_back_exactly_the_units_it_replaced(self, world):
        """Switch back means the units too: what was installed, hand edits
        included, and nothing the failed build added."""
        world.push_deploy(V1)
        assert_ok(world.run())
        (world.units / "status.service").write_text("edited by hand\n")
        world.push_deploy({"status.service": "v2\n", "status-drift.timer": "[Timer]\n"})
        world.reset_log()

        result = world.run(FAKE_STALE_PORT="9000")

        assert result.returncode == 1, result.stderr
        assert world.installed("status.service") == "edited by hand\n"
        assert world.installed("status-drift.timer") is None
        assert world.installed("status-dev.service") == V1["status-dev.service"]
        calls = world.calls()
        restored = index_of(calls, "logger -t status-deploy live units restored")
        reloads = [i for i, c in enumerate(calls) if "daemon-reload" in c]
        assert reloads[-1] < restored
        assert reloads[-1] < len(calls) - 1 - calls[::-1].index("sudo systemctl restart status")

    def test_a_failed_live_leaves_dev_on_its_new_units(self, world):
        """As the link: a failure on live leaves dev on the new build."""
        world.push_deploy(V1)
        assert_ok(world.run())
        world.push_deploy({"status-dev.service": "dev v2\n"})
        assert world.run(FAKE_STALE_PORT="9000").returncode == 1
        assert world.installed("status-dev.service") == "dev v2\n"

    def test_the_copies_kept_for_a_switch_back_are_removed(self, world):
        tmp = world.tmp / "tmpdir"
        tmp.mkdir()
        world.push_deploy(V1)
        (world.units / "status.service").write_text("edited by hand\n")
        assert_ok(world.run(TMPDIR=str(tmp)))
        assert not list(tmp.iterdir())

    def test_a_unit_that_cannot_be_kept_aside_is_not_replaced(self, world):
        """No copy, no switch back: the target fails before overwriting it."""
        world.push_deploy(V1)
        assert_ok(world.run())
        previous = world.target("live")
        world.push_deploy({"status.service": "v2\n"})
        (world.units / "status.service").chmod(0o000)
        try:
            result = world.run()
        finally:
            (world.units / "status.service").chmod(0o644)
        assert result.returncode == 1, result.stderr
        assert "aside" in result.stderr
        assert world.target("live") == previous
        assert world.installed("status.service") == V1["status.service"]

    def test_nowhere_to_keep_the_units_still_proves_the_old_build(self, world):
        """CR 4: a switch back with nothing kept aside restores nothing, and
        still restarts and proves the old build."""
        world.push_deploy(V1)
        assert_ok(world.run())
        world.push_deploy({"status.service": "v2\n"})
        stub = world.stubs / "mkdir"
        stub.write_text(
            '#!/usr/bin/env bash\n[[ "${@: -1}" == */status-deploy.*/live ]] && exit 1\n'
            'exec /bin/mkdir "$@"\n'
        )
        stub.chmod(0o755)
        result = world.run()
        assert result.returncode == 1, result.stderr
        assert "which is answering" in result.stderr
        assert world.installed("status.service") == V1["status.service"]

    def test_a_failed_install_switches_back(self, world):
        world.push_deploy(V1)
        assert_ok(world.run())
        previous = world.target("live")
        world.push_deploy({"status.service": "v2\n", "status-sweep.service": "v2\n"})

        result = world.run(FAKE_INSTALL_FAIL="status-sweep.service")

        assert result.returncode == 1, result.stderr
        assert "status-sweep.service" in result.stderr
        assert world.target("live") == previous
        assert world.installed("status.service") == V1["status.service"]
        assert world.installed("status-sweep.service") == V1["status-sweep.service"]


HOST_V1 = {
    "earlyoom.default": "earlyoom v1\n",
    "system.slice.d/10-memory-protection.conf": "slice v1\n",
}


class TestHostConfigs:
    """#18: compared after a live deploy, never installed, never a failure."""

    def install_host(self, world, rel: str, body: str) -> Path:
        path = world.sysetc / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_a_differing_host_config_is_a_warning_naming_both_paths(self, world):
        earlyoom = self.install_host(world, "default/earlyoom", "older\n")
        self.install_host(
            world, "systemd/system/system.slice.d/10-memory-protection.conf", "slice v1\n"
        )
        world.push_deploy(HOST_V1)
        result = world.run()
        assert_ok(result)
        assert "deploy/earlyoom.default" in result.stderr
        assert str(earlyoom) in result.stderr
        assert "10-memory-protection.conf" not in result.stderr, "that one is the same"
        assert earlyoom.read_text() == "older\n", "never installed"

    def test_a_missing_host_config_is_a_warning(self, world):
        world.push_deploy(HOST_V1)
        result = world.run()
        assert_ok(result)
        assert "deploy/earlyoom.default" in result.stderr
        assert "not installed" in result.stderr

    def test_dev_alone_compares_nothing(self, world):
        world.push_deploy(HOST_V1)
        result = world.run("--dev")
        assert_ok(result)
        assert "earlyoom" not in result.stderr

    def test_a_host_config_is_never_installed_as_a_unit(self, world):
        world.push_deploy(HOST_V1)
        assert_ok(world.run())
        assert not installs(world.calls())


def host_configs() -> dict[str, str]:
    """deploy.sh's HOST_CONFIGS: each deploy/ file and where it is installed."""
    body = re.search(r"^HOST_CONFIGS=\((.*?)^\)", DEPLOY.read_text(), re.M | re.S)
    assert body, "HOST_CONFIGS=( ... ) in deploy.sh"
    pairs = re.findall(r'"([^"=]+)=([^"]+)"', body.group(1))
    return dict(pairs)


class TestWhichUnitsAreWhose:
    """#18: deploy.sh tells a target's units by name; the repo keeps to it."""

    REPO_DEPLOY = REPO_ROOT / "deploy"

    def root_of(self, unit: Path) -> str:
        (line,) = [ln for ln in unit.read_text().splitlines() if ln.startswith("WorkingDirectory=")]
        return line.split("=", 1)[1]

    def test_a_service_runs_dev_exactly_when_its_name_ends_dev(self):
        services = sorted(self.REPO_DEPLOY.glob("*.service"))
        assert services
        for unit in services:
            want = "/srv/status/dev" if unit.stem.endswith("-dev") else "/srv/status/live"
            assert self.root_of(unit) == want, unit.name

    def test_a_timer_belongs_to_its_services_target(self):
        for timer in sorted(self.REPO_DEPLOY.glob("*.timer")):
            (line,) = [ln for ln in timer.read_text().splitlines() if ln.startswith("Unit=")]
            service = self.REPO_DEPLOY / line.split("=", 1)[1]
            assert service.is_file(), timer.name
            assert service.stem.endswith("-dev") == timer.stem.endswith("-dev"), timer.name

    def test_every_file_under_deploy_is_a_unit_or_a_host_config(self):
        """A new file there is installed or compared, never forgotten."""
        configs = host_configs()
        for path in sorted(p for p in self.REPO_DEPLOY.rglob("*") if p.is_file()):
            rel = path.relative_to(self.REPO_DEPLOY).as_posix()
            is_unit = "/" not in rel and path.suffix in (".service", ".timer")
            assert is_unit != (rel in configs), rel
        for rel in configs:
            assert (self.REPO_DEPLOY / rel).is_file(), f"HOST_CONFIGS names {rel}, not in deploy/"

    def test_the_runbook_installs_each_host_config_where_deploy_compares_it(self):
        """The warning points at the RUNBOOK, so the two must agree."""
        runbook = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()
        # The drop-ins go in by one loop, each at its own path under the unit dir.
        drop_in_loop = '"/etc/systemd/system/$f"' in runbook
        for rel, dest in host_configs().items():
            assert rel in runbook, rel
            if dest == f"systemd/system/{rel}":
                assert drop_in_loop, dest
            else:
                assert f"/etc/{dest}" in runbook or f"/etc/{Path(dest).parent}/" in runbook, dest
