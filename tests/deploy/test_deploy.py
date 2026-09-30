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
- **Order.** Dev before live. Migrate, then switch, then restart, then verify.
  A failed verify switches back.
- **A database ahead of the release is not migrated.** That is a rollback.
"""

import fcntl
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy.sh"

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
esac
exit 0
"""

STUB_SUDO = r"""#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
[[ "$1" == systemctl && "$2" == start && "$3" == "${FAKE_SWEEP_FAIL:-none}" ]] && exit 1
exit 0
"""

# /health answers with whichever release the port's symlink names, as the
# restarted API would; FAKE_STALE_PORT plays an API still on the old build.
STUB_CURL = r"""#!/usr/bin/env bash
url="${@: -1}"
echo "curl $url" >> "$FAKE_LOG"
case "$url" in *:9000/*) link=live ;; *:9001/*) link=dev ;; esac
build="$(basename "$(readlink "$STATUS_DEPLOY_ROOT/$link")")"
# Stale only while the link names the newest main commit, unless always.
if [[ "$url" == *":${FAKE_STALE_PORT:-none}/"* ]]; then
  [[ -n "${FAKE_STALE_ALWAYS:-}" || "$build" == "${FAKE_NEW_BUILD:-$build}" ]] && build=stale
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
busy_file="$(dirname "$FAKE_LOG")/busy"
[[ -f "$busy_file" ]] || echo "${FAKE_SWEEP_BUSY:-0}" > "$busy_file"
left="$(cat "$busy_file")"
if [[ "$1" == show && "$left" -gt 0 ]]; then
  echo $((left - 1)) > "$busy_file"
  echo activating
elif [[ "$1" == show ]]; then
  echo inactive
fi
"""

STUB_LOGGER = r"""#!/usr/bin/env bash
echo "logger $*" >> "$FAKE_LOG"
"""


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
        for d in (self.root, self.etc, self.stubs):
            d.mkdir()
        self.log.touch()

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
            "STATUS_DEPLOY_VERIFY_SECONDS": "2",
            "STATUS_BIND_HOST": "127.0.0.1",
            "FAKE_LOG": str(self.log),
            # The build FAKE_STALE_PORT refuses to report: the one being deployed.
            "FAKE_NEW_BUILD": self.build(self.main[-1]),
            **fake,
        }
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

    def target(self, name: str) -> str | None:
        link = self.root / name
        return os.readlink(link) if link.is_symlink() else None

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def reset_log(self) -> None:
        self.log.write_text("")


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    # Releases are read-only; make them deletable for tmp_path's cleanup.
    releases = w.root / "releases"
    if releases.exists():
        for path in [releases, *releases.rglob("*")]:
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


class TestReleases:
    def test_a_built_release_is_reused(self, world):
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run(world.main[-1]))
        world.reset_log()
        assert_ok(world.run(world.main[0]))
        assert not [c for c in world.calls() if " sync " in c]

    def test_an_interrupted_build_is_rebuilt(self, world):
        """No REVISION means the build never finished: start it again."""
        partial = world.root / "releases" / world.build(world.main[-1])
        partial.mkdir(parents=True)
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

    def test_pruning_spares_what_dev_still_runs(self, world):
        assert_ok(world.run(world.main[0]))
        assert_ok(world.run("--dev", "origin/feature"))
        assert_ok(world.run("--dev", "origin/feature", STATUS_DEPLOY_KEEP="1"))
        kept = {p.name for p in (world.root / "releases").iterdir()}
        assert kept == {world.build(world.main[0]), world.build(world.feature)}


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
        assert not [c for c in world.calls() if c.startswith("sudo ")]


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
        reset = calls.index("sudo systemctl reset-failed status")
        restarts = [i for i, c in enumerate(calls) if c == "sudo systemctl restart status"]
        assert restarts[0] < reset < restarts[1]
        assert [c for c in calls[restarts[1] :] if c.endswith(":9000/health")]

    def test_a_rollback_that_does_not_come_back_says_so(self, world):
        """The old build not answering either is the loudest failure there is."""
        assert_ok(world.run(world.main[0]))
        result = world.run(FAKE_STALE_PORT="9000", FAKE_STALE_ALWAYS="1")
        assert result.returncode != 0
        assert "NOT answering" in result.stderr

    def test_a_failing_sweep_pass_is_switched_back(self, world):
        assert_ok(world.run(world.main[0]))
        previous = world.target("live")
        result = world.run(FAKE_SWEEP_FAIL="status-sweep.service")
        assert result.returncode != 0
        assert world.target("live") == previous
        assert "status-sweep" in result.stderr

    def test_a_failing_dev_stops_the_deploy_before_live(self, world):
        assert_ok(world.run(world.main[0]))
        live_before = world.target("live")
        result = world.run(FAKE_SWEEP_FAIL="status-sweep-dev.service")
        assert result.returncode != 0
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

    def test_a_first_deploy_that_fails_has_nothing_to_return_to(self, world):
        result = world.run(FAKE_STALE_PORT="9001")
        assert result.returncode != 0
        assert "no previous release" in result.stderr


class TestOperation:
    def test_no_restart_links_and_migrates_but_touches_no_unit(self, world):
        """The cutover's first step: the units still point at the checkout."""
        assert_ok(world.run("--no-restart"))
        assert world.target("live") == f"releases/{world.build(world.main[-1])}"
        assert not [c for c in world.calls() if c.startswith(("sudo ", "curl "))]
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

    def test_a_concurrent_deploy_is_refused(self, world):
        with open(world.root / ".deploy.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = world.run()
        assert result.returncode != 0
        assert "another deploy" in result.stderr

    def test_a_missing_root_says_how_to_create_it(self, world):
        world.root.rmdir()
        result = world.run()
        assert result.returncode != 0
        assert "mkdir" in result.stderr

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

    def test_an_unknown_flag_is_refused(self, world):
        assert world.run("--live-only").returncode != 0

    def test_root_is_refused(self):
        """Releases belong to exedev, the units' user (R13); root would own them."""
        assert '"$(id -u)" -eq 0' in DEPLOY.read_text()
