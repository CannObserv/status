# co-status deploys: production runs a release, never the working tree

**Status:** accepted (2026-09-30)
**Issue:** [#9](https://github.com/CannObserv/status/issues/9) ([review](https://github.com/CannObserv/status/issues/9#issuecomment-5916059572))
**Cohort design:** [CannObserv/broker#22](https://github.com/CannObserv/broker/issues/22). This spec is its first pilot for a service with a database: § [broker#22's questions](#broker22s-questions) answers each one for co-status.
**Amends:** [MVP spec D12](2026-09-26-co-status-mvp-design.md). co-status is still developed on its own VM; development no longer reaches production except by a deploy.
**Plan:** [2026-09-30-deploy-releases](../plans/2026-09-30-deploy-releases.md)

## Problem

All four units have `WorkingDirectory=/home/exedev/status`, the checkout agents edit. The sweeps `uv run` that tree every 60 seconds, so saving a file deploys it. That includes uncommitted edits, and, through `uv run`'s implicit sync, edits to `pyproject.toml` or `uv.lock`.

On 2026-09-29, #6's model change reached both sweeps before its migration had run. The production sweep then crashed on 38 consecutive passes (18:06:29–18:46:31 UTC), while it was the only thing watching the cohort for silence. The first failure came nine minutes before #6's first commit.

Two further problems share this cause:

- **Secrets from the repo `.env` reach production.** `status.service` and `status-sweep.service` load the repo `.env` as an `EnvironmentFile=`, so every PAT and API key in it is in production's environment.
- **`build` misreports.** `/health`'s `build` is `git rev-parse HEAD` at start, not the code the sweep ran.

## Decisions

| # | Decision | Why |
|---|---|---|
| **R1** | **Every unit runs an immutable release directory, never a git checkout.** Live and dev alike. | It removes the premise rather than guarding it (broker#22). A guard on a dev tree trades silent drift for a refused restart (replicator#94). |
| **R2** | **Layout:** `/srv/status/releases/<build>/`, plus two symlinks, `/srv/status/live` and `/srv/status/dev`. Owned by `exedev`; a finished release is read-only (`a-w`). One commit is one release, shared by both symlinks. | A symlink swap by `rename(2)` is atomic, and rollback is repointing it. Owned by `exedev` because the units run as `exedev` and `exedev` has sudo anyway: root ownership adds a deploy-time `sudo` for no real boundary here. Read-only is what stops the accidental edit, which is the actual failure. |
| **R3** | **Source:** `git archive <sha>` from the development checkout, after `git fetch origin`. Live requires `<sha>` to be on `origin/main`; dev requires it on some `origin/*` branch. | The repo is public, so fetching needs no credential, and there is no store-helper trap. The ancestry check is what makes "pushed" true. An archive has no `.git`, no submodules and no untracked files, so there is nothing in a release to drift. |
| **R4** | **Build in place:** `uv sync --locked --no-dev --compile-bytecode` inside the release, then write `REVISION`, then `chmod -R a-w`. **`REVISION` is written last**, so a directory without one is an interrupted build: deleted and rebuilt. | A uv venv embeds its absolute path in its scripts' shebangs, so a venv built elsewhere and moved into place breaks. Building at the final path, with `REVISION` as the commit marker, gives the same all-or-nothing result. |
| **R5** | **Units run `uv run --frozen --no-sync`.** Syncing is a build step, never a start or pass side effect. | The cohort template (`init-project-fastapi`) and replicator. It also covers the hand-run inner loop in a worktree: the developer syncs. |
| **R6** | **Deploy order:** build; dev (migrate `status_dev`, switch `dev`, restart `status-dev`, force a dev sweep pass, verify); then live (the same against `status`). **A failed verification after a switch switches back**, restarts, and exits non-zero. Migrations are not reverted. | Dev is the rehearsal for every migration. A forced pass (`systemctl start` on the oneshot waits for it) is the only check that the sweep's code works against the database; `/ready` covers the API. |
| **R7** | **Migrations are expand-only.** The previous release must run against the new schema. A contracting change (drop, rename, `NOT NULL` without a default) ships as two deploys: stop using it, then remove it. | Between migrate and switch the old code runs against the new schema. That also has to hold for a rollback. |
| **R8** | **Schema check (`src/core/schema_state.py`).** It compares `alembic_version` with the code's single Alembic head: `current`, `behind` (a revision the code knows, not head), `ahead` (a revision it does not know), or `unmigrated`. **`behind` and `unmigrated` fail a sweep pass** (`SchemaBehind`, which also sends `/fail`), and make `/ready` 503. **`ahead` logs a warning and proceeds.** Nothing refuses to start, with one exception: `dev_server.sh` refuses a database that is `behind` or `unmigrated`. It already refused an unmigrated one (notifier#23), and its start is where a developer learns they need to migrate. | Fails loudly on the 2026-09-29 case. Allows R6's migrate-then-switch window and a rollback, which observo#192's exact match would break. It never blocks a start (broker#22 goal 3); a refused API start would record no check-ins at all. |
| **R9** | **Deploy skips the migration when the database is `ahead`.** | A rollback to an older release: its Alembic cannot resolve the newer revision, and `upgrade head` would fail. By R7, it runs anyway. |
| **R10** | **Build id is the release's `REVISION`, read by the app** (`src/core/build.py`). There is no `ExecStartPre` git stamp. It is `dev` when absent. The sweep logs `build` on every pass. | The code that ran reports itself. It is correct for the sweep, which never had a stamp, and it needs no git in a release. |
| **R11** | **Env:** live units read `/etc/status/.env` only. Dev units read `/etc/status/dev.env` (`DEV_DATABASE_URL`, `root:exedev 0640`). **No unit reads a repo `.env`.** | Removes the PATs and API keys from production's environment (broker#22 Q6). A release has no `.env` anyway. |
| **R12** | **Dev is deployed, not an exception.** `scripts/deploy.sh --dev <ref>` puts any pushed ref on `:9001`. The inner loop is `scripts/dev_server.sh` run by hand from a worktree, after `sudo systemctl stop status-dev`, as before. | broker#22 Q11: no named loophole. The dev endpoint carries watcher's non-production traffic, so it deserves a deploy too. |
| **R13** | **`scripts/deploy.sh` runs as `exedev`,** with `sudo` for `systemctl` only, serialized by `flock`. It keeps the 5 most recently deployed releases plus whatever `live` and `dev` point at. | A deploy should be runnable by an agent or an operator alike. The retention covers several rollbacks and costs about 100 MB. |

**Deferred** (none blocks this):

- A CI-green check before a live deploy. CI is the only correctness signal (AGENTS.md), but reading check runs needs a token in the deploy path.
- A drift signal when `live` lags `origin/main` (broker#22 goal 4).
- `OnFailure=` (broker#22 Q10).
- A root-owned deploy root (R2).

## Design

### `scripts/deploy.sh [--dev] [<ref>]`

`<ref>` defaults to `origin/main`. Without `--dev` it deploys dev, then live; with `--dev`, dev only.

1. **Refuse** if running as root, or if another deploy holds the lock.
2. **Resolve.** `git fetch --prune origin` in the checkout the script sits in, then resolve `<ref>` to a commit and check it (R3).
3. **Build** `releases/<build>` unless a complete one exists whose venv still runs (R4). One that no longer runs is rebuilt only if no target links it; otherwise the deploy stops. `<build>` is `git rev-parse --short=12`. After `uv sync`, and before `REVISION` is written, check that the release has exactly one Alembic head.
4. **For each target** (`dev`, then `live`), with its environment file:
   1. Read the schema state from the new release. For `current`, `behind` or `unmigrated`, run `alembic upgrade head`; for `ahead`, skip it (R9). Any other result (refused, unreachable, crashed) aborts before anything switches.
   2. Record the old target, then swap the symlink (`ln -s` to a temporary name, then `mv -T`).
   3. `sudo systemctl restart` the API unit. Wait out any sweep pass already running, because it started on the old release and `start` would join it. Then `sudo systemctl start` the sweep service, which waits for the pass.
   4. Verify: the sweep exited 0; `/ready` is 200 (so `schema_state` is `current` or `ahead`); `/health` `build` equals `<build>`. Poll up to 60 s, on the tailnet address.
   5. If verification fails, swap back, `reset-failed` (a crash loop may have hit the start limit), restart, prove the old build with a sweep pass and `/ready`/`/health`, and exit 1 (old build answering) or 4 (not), naming the step. A failure on live leaves dev on the new build.
5. **Prune** (R13).

The root, env directory, retention and time bounds come from `STATUS_DEPLOY_ROOT`, `STATUS_DEPLOY_ENV_DIR`, `STATUS_DEPLOY_KEEP`, `STATUS_DEPLOY_VERIFY_SECONDS` and `STATUS_DEPLOY_SWEEP_WAIT_SECONDS` (DEPLOYMENT.md lists the defaults). The tests run the script against a throwaway root and a temporary origin, with stub `uv`, `sudo`, `systemctl`, `curl`, `logger` and `rm` on `PATH`.

### Units

- **Paths.** `WorkingDirectory=/srv/status/{live,dev}`. `ExecStart=` is the same path plus `/scripts/…`.
- **Physical paths in the launch scripts.** They `cd -P`, so a process resolves its release once and a later swap cannot hand a running API a module from a different release.
- **No build stamp.** The `ExecStartPre` git stamps go; `/run/status` goes with them.
- **Env files.** No repo `.env`. Dev units add `EnvironmentFile=/etc/status/dev.env`.
- **Tests** hold that no unit mentions `/home/exedev`.

### Schema check

`code_head()` loads the release's `alembic/` through `ScriptDirectory`, and refuses more than one head. `database_revision(session)` returns `None` when `alembic_version` does not exist; it uses `to_regclass`, so the probe never aborts the transaction. `classify()` is pure.

The check is used in three places:

- **Sweep:** inside `run_sweep`'s `try`, before the pass, so a behind schema sends `/fail` with body `SchemaBehind`.
- **API:** `/ready`, with `schema_state` on the 200 payload, and on the 503 whenever the database was reached (one `NotReadyResponse` model).
- **Deploy and `dev_server.sh`:** the CLI `python -m src.core.schema_state`, which prints the state. Exit 0 means `current` or `ahead`, 3 means `behind` or `unmigrated`, 2 means anything else (unreachable, refused, crashed); never 1, which is any uncaught exception. This replaces `dev_server.sh`'s bash `alembic current` test.

## broker#22's questions

| Q | co-status |
|---|---|
| 1 Location, ownership | `/srv/status`, `exedev`, releases read-only (R2) |
| 2 Atomicity, rollback | release per commit plus a symlink swap; rollback is `deploy.sh <old sha>` (R2, R4, R9) |
| 3 Git source | local object store after `git fetch`, ancestry against `origin/*` (R3) |
| 4 Who deploys | `scripts/deploy.sh`, as `exedev`, sudo for systemctl (R13) |
| 5 Dependencies | `uv sync --locked --no-dev` at build (R4, R5). No private wheels |
| 6 Env and state | `/etc/status/` only (R11). The units write nothing under their working directory |
| 7 Restart | the deploy restarts the API and forces a sweep pass (R6). No restart window: check-ins retry |
| 8 Guards | at deploy time: ancestry, a single head, verification. At run time: only the schema check, which never blocks a start (R8) |
| 9 Build id, drift | `REVISION` in `/health` and in every sweep line (R10). Drift signal deferred |
| 10 `OnFailure=` | deferred; healthchecks.io already watches the sweep (#1) |
| 11 Dev services | deployed, no exception (R12) |
| 12 Tests | units hold no `/home/exedev`; `deploy.sh` runs end to end against a temp root with stubs |
| 13 Packaging | lift R1–R13 into `init-project-fastapi` and `shipping-work-python-fastapi` via broker#22 |
| 14 Rollout | co-status now, as broker#22's database pilot |

## Cutover

1. **`/etc/status/dev.env`.** Copy `DEV_DATABASE_URL` from the repo `.env` into it, `root:exedev 0640`.
2. **First deploy, units not yet switched.** `sudo mkdir /srv/status && sudo chown exedev: /srv/status`. Then build and link without restarting: `deploy.sh --no-restart`, a flag that exists for this step only.
3. **Switch the units.** Copy them to `/etc/systemd/system/`, `daemon-reload`, restart the APIs, and start both sweeps by hand. Check `/health` `build`, `/ready`, `list-timers`, and the healthchecks.io checks.
4. **Clean up.** Delete `.skills/worktree_venv` in the development checkout, because its `.venv` is no longer production's.

Soak (#2): this is a production change during Phase 6, recorded there. The monitors, cutover state and alert paths do not change.
