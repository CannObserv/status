# Deploying co-status

How code reaches the units, and how to tell what is running. Design and reasons: [the deploy spec](specs/2026-09-30-deploy-releases-design.md) (R1–R13). Setup and routine ops: [RUNBOOK](RUNBOOK.md).

## Releases, not a checkout

```
/srv/status/                 root:root 0755
  releases/                  root:root 0755
    <build>/                 git archive of one pushed commit + its own .venv; root's, read-only; REVISION
  live -> releases/<build>   status.service, status-sweep.service   (:9000, database status)
                             status-drift.service (#12; GitHub and healthchecks.io only)
  dev  -> releases/<build>   status-dev.service, status-sweep-dev.service (:9001, status_dev)
```

- **No unit runs `/home/exedev/status`.** On 2026-09-29 they all did, and an unmigrated model edit crashed the production sweep for 40 minutes ([#9](https://github.com/CannObserv/status/issues/9)). Saving, committing, switching branches or running `uv sync` in a checkout now changes nothing a unit runs.
- **A release is one commit, built once.** `<build>` is the commit's 12-character short SHA. `REVISION` is written last, by root, once the tree is root's, so a directory without one is an interrupted build and gets rebuilt. `live` and `dev` share a release when they run the same commit.
- **Root owns what the units run** ([§ Who owns a release](#who-owns-a-release)). The units run as `exedev` and only read it.
- **Units never sync.** They `uv run --frozen --no-sync` the venv the deploy built.
- **Env comes from `/etc/status/` only.** Live units read `.env`; dev units read `dev.env` (`DEV_DATABASE_URL`). No unit reads a repo `.env`.

## Who owns a release

**Root owns `/srv/status`, `releases/` and every finished release** ([#14](https://github.com/CannObserv/status/issues/14), [spec § Amendment](specs/2026-09-30-deploy-releases-design.md#amendment-r2-root-owns-the-releases-14)). The units run as `exedev` and read. `exedev` cannot edit a release, `chmod` it, or repoint `live` or `dev`: a link can only be replaced by someone who can write its directory.

**This is not a security boundary against `exedev`.** `exedev` has passwordless sudo, and so does every agent session. What root ownership buys:

- **Changing production takes `sudo`.** A plain `chmod u+w` and edit no longer works. sudo journals each command it runs with its user and directory (`sudo journalctl _COMM=sudo`). **A root shell is journaled only as a shell:** what is done in `sudo -i` or `sudo bash` leaves no record beyond `COMMAND=/bin/bash`.
- **An accident fails loudly**, with `Permission denied` where it used to succeed.

**How `deploy.sh` builds:**

- `exedev` builds the release in place, at its final path, since a uv venv embeds that path. Then it removes write permission, and `sudo chown -R root:root` hands the tree to root. `REVISION` is written last, by root.
- Venvs are built with `--link-mode copy`. Hardlinked to the uv cache, as releases were before #14, a chown would reach the cache, and an edit in one venv would reach every venv sharing the file. Each release is about 70 MB.
- **A release not owned by root is not reused.** It is rebuilt, or refused while a target runs it, as an interrupted build is.
- The deploy refuses a root, or a `releases/`, that root does not own, that group or others can write, or that is a link, and names the fix.
- **`/srv` must be root's and writable by root alone too** (it is, `root:root 755`). Otherwise `exedev` could rename `/srv/status` away and put its own in its place. The deploy does not check it: the tests' roots sit in directories the test user owns.

**A hand fix in an emergency** is `sudo`. Name the path on sudo's command line (`sudo vim /srv/status/…`, not `sudo -i`), so the journal records it. Prefer `deploy.sh --skip-ci` of a pushed fix, which leaves a release that is exactly a commit. A hand-edited release is still marked finished, so the next deploy of that build reuses it as it stands. Deploy a different build.

**Moving to root ownership** (once per VM; on `co-status` with #14's first deploy), from a checkout on `main` with #14 in it. `deploy.sh` runs the checkout's own copy, and an older one fails at its lock file with a bare `Permission denied`:

```bash
cd /home/exedev/status && git switch main && git pull --ff-only
sudo chown root:root /srv/status /srv/status/releases
sudo chmod 755 /srv/status /srv/status/releases
sudo rm -f /srv/status/.deploy.lock        # the lock is the directory now
scripts/deploy.sh                          # a fresh root-owned release for both targets
stat -c '%U %a %n' /srv/status /srv/status/releases "$(readlink -f /srv/status/live)"
```

The releases built before #14 stay `exedev`'s. `chown -R` would reach the uv cache's inodes through their hardlinks. The prune removes them over the next deploys, and a rollback to one rebuilds it.

## `scripts/deploy.sh`

Run it as `exedev` from any checkout. It fetches `origin` itself, builds as `exedev`, and uses `sudo` for `systemctl`, unit files and every write under `/srv/status`.

```bash
scripts/deploy.sh                       # origin/main to dev, then live, once its CI passed
scripts/deploy.sh <sha>                 # a specific main commit (a rollback, say)
scripts/deploy.sh --dev origin/<branch> # any pushed branch, :9001 only, never gated
scripts/deploy.sh --skip-ci [<sha>]     # live without asking CI: an emergency, logged
scripts/deploy.sh --help
```

The defaults are what production uses. The variables exist for the tests and for an unusual recovery:

| Variable | Default | What |
|---|---|---|
| `STATUS_DEPLOY_ROOT` | `/srv/status` | releases and the `live`/`dev` links |
| `STATUS_DEPLOY_ENV_DIR` | `/etc/status` | `.env` (live) and `dev.env` (dev) |
| `STATUS_DEPLOY_ETC` | `/etc` | units in `systemd/system/`; host configs compared where the RUNBOOK installs them |
| `STATUS_DEPLOY_KEEP` | `5` | releases kept besides the linked ones |
| `STATUS_DEPLOY_VERIFY_SECONDS` | `60` | how long `/ready` and `/health` have to answer |
| `STATUS_DEPLOY_SWEEP_WAIT_SECONDS` | `150` | how long to wait out a pass already running |
| `STATUS_DEPLOY_CI_WAIT_SECONDS` | `600` | how long a live deploy waits for the commit's CI |
| `STATUS_DEPLOY_CI_POLL_SECONDS` | `30` | how often it asks GitHub meanwhile |

**What goes live** is a commit on `origin/main` whose CI passed ([§ The CI gate](#the-ci-gate)). Dev takes any commit on an `origin/*` branch. Anything unpushed is refused.

**Order, per target, dev first**, once [the CI gate](#the-ci-gate) has passed for a live deploy:

1. **Migrate** the target's database. This is skipped when the database is *ahead* of the release, which is what a rollback looks like.
2. **Switch** the symlink (an atomic rename), then **install the target's units** from the release, those that differ from their installed copies ([§ Units](#units)).
3. **Restart** the API, then force one sweep pass. Any pass already running started on the old release, so the deploy first waits for it to end (up to 150 s): `systemctl start` on a oneshot mid-pass joins that pass rather than starting another. `systemctl start` then waits for the new pass to finish.
4. **Verify.** The pass must exit 0, bounded by the unit's `TimeoutStartSec=120`. Then, within 60 s, `/ready` must be 200 and `/health` must report `build` equal to `<build>`.
5. **On failure,** switch back, units included, clear the unit's start limit, restart, and prove the old build the same way: its sweep pass, then its API. Exit 1 when the old build answers. Exit 4 when the target is left on a build that does not answer: the old build failed too, or there was nothing to switch back to. Either way the message names the step. The journal records the outcome only after that check. The migration stays applied. A first deploy has nothing to switch back to. Nor does a deploy of the build a target already ran, unless it replaced units: then those go back, and the build is proved on them ([§ Units](#units)).

A failure on dev stops the deploy before live is touched. A failure on live leaves dev on the new build and its units, so redeploy dev from the old build if that matters. The deploy keeps the 5 most recently deployed releases, plus whatever `live` and `dev` point at. Every switch and rollback is logged: `journalctl -t status-deploy`. One deploy at a time: the lock is `flock` on `/srv/status` itself.

## The CI gate

**A live deploy needs the commit's CI to have passed** ([#11](https://github.com/CannObserv/status/issues/11)). Before anything is built or migrated, `deploy.sh` asks GitHub's Actions API about the commit and refuses unless `lint`, `test` and `migrations`, and any other job the run has, all succeeded. The refusal names each job that did not, with its conclusion, and links the run. A refused deploy changes nothing, so dev is not touched either. **Dev alone is never gated**: `--dev` exists to try what CI has not passed.

- **Which run.** The newest `push` run of `ci.yml` on `main` for exactly that commit. A commit can have several runs: 291604b has a `workflow_dispatch` run on its branch and the push run on `main`, so its check runs list each job twice. Only the push run counts. A branch run tests the same tree, but with several, the verdict would depend on which one someone ran last. A pull request's run tests a merge commit, not this one. A re-run counts, since GitHub reports a run's latest attempt.
- **The run and every job in it, `success` only.** A `skipped` job leaves the run's own conclusion `success`, so the gate reads the jobs too. `lint`, `test` and `migrations` must be among them: `CI_JOBS` in `deploy.sh`, the floor `tests/ci` holds `ci.yml` to. Any other job counts without being named there, so a checkout older than `ci.yml` cannot pass a job it has never heard of, and a rollback's older run, from before a job was added, still passes. A job renamed in `ci.yml` must be renamed in `CI_JOBS`; `test_deploy.py` checks it. Builds from before the rename then lack the new name, so a rollback to one is refused (`pytest (not in the run)`, say) and needs `--skip-ci`. A run cancelled while queued lists no jobs, and the refusal says `run concluded cancelled`. Cancelled is not a verdict: a newer push or a dispatch on `main` (the same concurrency group) cancels a queued run, so re-run it from its page, or deploy the newer commit.
- **Pending: it waits**, up to 10 minutes, asking every 30 s. `git push origin main && scripts/deploy.sh` works as one step; CI takes about 2 minutes. A run still going after that is refused with its link: deploy again when it finishes. The wait holds the deploy lock.
- **No run.** GitHub runs CI on the newest commit of each push only, and never on a `[skip ci]` commit, so many `main` commits have no run (e6c8d09, in the middle of #13's push). A commit behind `origin/main`'s tip with no run is refused at once: deploy the newest commit of its push, or pass `--skip-ci`. If it was pushed seconds ago, with another push after it, its run may not be listed yet: deploy again shortly first. The tip may have been pushed seconds ago, so it waits, as for a pending run. A tip that never gets one (`[skip ci]`) is refused when the wait ends, after 10 minutes; `--skip-ci` it rather than wait.
- **No token.** The repo is public, so the API answers without one, at 60 requests an hour per IP address. A deploy costs 2, or up to 22 when it waits the full 10 minutes. With no credential in the deploy path, there is none to scope, store or rotate. If GitHub refuses or cannot be reached (rate limit, outage), or answers with anything but a JSON object (an empty body included), the deploy says so, with GitHub's message when there is one, and builds nothing: wait, or pass `--skip-ci`. If the repo goes private, the API answers 404, and the gate needs a read-only token.
- **`--skip-ci`** deploys to live without asking GitHub at all: for a fix that cannot wait for CI, or when GitHub cannot answer. Before anything is built it is logged, `live: CI not checked for <build> (--skip-ci)`. With `--dev` it is refused, since dev is never gated. A gate that passes is logged too, with the run's link:

```bash
journalctl -t status-deploy -n 20      # "live: CI passed for <build> (<run>)", or "... CI not checked ..."
```

## Rules the design depends on

- **Migrations are expand-only.** The previous release must keep working against the new schema: it runs there between migrate and switch, and after any rollback. Add columns and tables, with defaults or nullable. A drop, a rename, or a new `NOT NULL` without a default ships as two deploys: first stop using it, then remove it.
- **One Alembic head.** A release with two heads is refused before anything switches.
- **Deploy is the only way in.** The shipping skill's "migrate, then `systemctl restart`" step does not apply here: `deploy.sh` does both, in order, and verifies. Any hand-run alembic command that connects to `status` is refused with `ProductionDatabaseError`, `upgrade` and `downgrade` as much as `check`, `current` or `revision --autogenerate`: `alembic/env.py` crosses `db_safety`, and of what runs alembic, only `deploy.sh` passes `STATUS_ALLOW_PROD_DB=1`, for live ([#15](https://github.com/CannObserv/status/issues/15)). Run them against dev with `DATABASE_URL="$DEV_DATABASE_URL"` after `. scripts/load_env.sh`. `--sql` runs connect to nothing and are not checked.

## Rollback

```bash
journalctl -t status-deploy -n 20      # "live -> <build> (was releases/<old>)"
scripts/deploy.sh <old build>          # still on origin/main, so it may go live
```

A rollback goes through [the CI gate](#the-ci-gate) like any live deploy. A build that went live through the gate has a green push run, and passes again unless a job in `CI_JOBS` has been renamed since (see [the CI gate](#the-ci-gate)). One that went live before #11, from the middle of a push, may have no run at all. Either way, `--skip-ci` it, once. The old release still exists (within the 5 kept), so nothing is rebuilt, unless its venv no longer runs, or it was built before #14 and is not root's: the first rollback after #14 shipped rebuilds the release it returns to ([§ Who owns a release](#who-owns-a-release)). A release that `live` or `dev` still runs is never rebuilt in place: the deploy stops, names the target, and asks for another build there first. Its Alembic does not know the newer revision, so the schema reads `ahead` and the migration is skipped. By the expand-only rule, the old code runs. A rollback never downgrades the schema.

**Rolling back past #10, then forward again.** A build from before migration `b3344354c124` starts outages and renotifies without touching `last_alert_dispatch_id` or `last_alert_redeliver_at`. Rolled forward, the sweep could redeliver a dispatch from before the rollback, and that dispatch's status (`succeeded`, say) would hide the current outage's undelivered alert. Between the rollback and the roll-forward, clear the schedule, so only alerts sent after the roll-forward are redelivered: `UPDATE monitors SET last_alert_redeliver_at = NULL` (`sudo -u postgres psql status`, and `status_dev` for dev).

## Drift

**Pushed is not deployed, and an hourly check says so** ([#12](https://github.com/CannObserv/status/issues/12), [monitors.md § Who watches co-status](reference/monitors.md#who-watches-co-status)). `status-drift.timer` compares live's `REVISION` with `origin/main` on GitHub and fails healthchecks.io's `co-status-drift` once code (not docs or tests) has waited 8 h since its push. After a deploy the check clears on its next run, within the hour; `sudo systemctl start status-drift` clears it now.

Units are not part of this check: every deploy installs them, so they drift only between deploys, by hand edits ([§ Units](#units)).

## Units

**A unit edit ships like code** ([#18](https://github.com/CannObserv/status/issues/18)): merge it, `scripts/deploy.sh`. Until #18 units went in by a hand-run `sudo cp`, and on 2026-10-02 four installed copies differed from the repo with nothing saying so.

- **Each target installs its own units from the release it switches to**, right after the switch and before the restart. The restart and the forced sweep pass then run what was installed, so verification covers the units too. Between the switch and the install, a sweep tick would run the new code under the old unit; the window is the time between two commands, and a `/fail` from it clears on the forced pass that follows. **Whose is by name:** `<name>-dev.service` and `<name>-dev.timer` are dev's (`status-dev`, `status-sweep-dev`); every other `deploy/*.service` and `*.timer` is live's (`status`, `status-sweep`, `status-drift`). `test_deploy.py` holds the repo to it: a unit runs `/srv/status/dev` exactly when its name ends `-dev`.
- **Only what differs.** On most deploys nothing does: no `sudo`, no `daemon-reload`. Otherwise `sudo install -m 644`, one `daemon-reload`, and `systemctl try-restart` for a changed timer so it re-arms. A timer that will not restart fails the target, which switches back: `status-sweep.timer` is the only thing watching for silence. The deploy names each unit it installed, and the journal has it: `live units from <build>: …`.
- **A new unit is installed, never enabled.** Enabling is a decision: #12's timer needed its healthchecks.io check first. The deploy prints the `sudo systemctl enable --now <unit>` it needs.
- **`--dev origin/<branch>` rehearses a unit edit** as it rehearses a migration: dev's units come from the branch. Live's stay on live's release.
- **On a failed verify, the units switch back with the link**: the installed copies the deploy replaced go back, hand edits included, and units it added are removed, then `daemon-reload` (`<target> units restored` in the journal). A unit that fails to install fails the target the same way. Redeploying the build a target already runs, say after a hand fix to a unit, puts the replaced units back on failure, so the hand fix survives a deploy that breaks it.
- **A rollback installs the old release's units.** Units always match the build their target runs. Units the old release lacks stay installed: a build from before #12 leaves `status-drift.*` in place, failing `203/EXEC` until live is past it ([RUNBOOK](RUNBOOK.md)).
- **A hand edit in `/etc/systemd/system/` lasts until the next deploy of its target, whatever that deploy changes.** The deploy compares each unit with its installed copy, not with the previous release, so it replaces the edit and names the unit. An emergency edit belongs in `deploy/` too.
- **By hand still:** enabling a new unit; `sudo systemctl reenable <unit>` after its `[Install]` section changes; and retiring one (`disable --now`, then remove the file and `daemon-reload`), since a unit gone from `deploy/` stays installed.
- **Host configs are compared, never installed.** Sysctl, earlyoom, the slice and Postgres drop-ins and needrestart change rarely, and installing one means `sysctl -p`, an earlyoom restart or a slice reload. After a live deploy, each one under `deploy/` that differs from its installed copy, or is missing, is a warning naming both paths; install it as the [RUNBOOK](RUNBOOK.md) First-time setup does. `HOST_CONFIGS` in `deploy.sh` maps each to its path, and `test_deploy.py` holds every file under `deploy/` to being a unit or one of them.

## The schema check

`src/core/schema_state.py` compares `alembic_version` with the release's head:

| State | Sweep pass | `/ready` | `dev_server.sh` |
|---|---|---|---|
| `current` | runs | 200 | starts |
| `ahead` (older code, newer schema) | runs, logs a warning | 200 | starts |
| `behind` | fails: `SchemaBehind`, `/fail` ping | 503, names the database | refuses; prints the migration |
| `unmigrated` | fails, as `behind` | 503 | refuses |

Nothing refuses to start a unit, because a refused API start would record no check-ins at all. The one exception is `dev_server.sh`, which refuses `behind` or `unmigrated` (it already refused an unmigrated database, notifier#23). `python -m src.core.schema_state` prints the state, and exits 0 for `current` or `ahead`, 3 for `behind` or `unmigrated`, and 2 for anything else (unreachable, refused by `db_safety`, crashed). `deploy.sh` migrates only on a state it printed.

## Development

Work in a worktree (the `using-git-worktrees` skill). To run your code on :9001:

- **Pushed:** `scripts/deploy.sh --dev origin/<branch>`, which gives it the full dev path, migration included.
- **Unpushed, fast loop:** `sudo systemctl stop status-dev`, then `scripts/dev_server.sh` from the worktree. It reloads on save and uses the repo `.env`'s `DEV_DATABASE_URL`. Run `uv sync` there yourself, because the launchers never sync. When you are done, redeploy dev or `sudo systemctl start status-dev`.

A later `deploy.sh` restarts `status-dev`, so stop the hand-run server first.

### Branch migrations

`--dev origin/<branch>` migrates `status_dev` to the branch's head. If that migration is later rewritten or abandoned, `status_dev` is left at a revision `main` never knows. Every later deploy then reads the schema as `ahead`, skips migrating as if it were a rollback, and dev stops rehearsing migrations. It says so: `database at <rev>, unknown to this code`. Before abandoning or rewriting a branch migration, downgrade `status_dev` from the branch's checkout:

```bash
. scripts/load_env.sh
DATABASE_URL="$DEV_DATABASE_URL" uv run alembic downgrade <revision main knows>
```

## Health checks

**On this VM, `curl http://127.0.0.1:9000/health` fails.** The units bind the tailnet address alone:

```bash
curl "http://$(tailscale ip -4):9000/health"     # this VM
curl http://status:9000/health                   # any tailnet node, this VM too
```

```json
{"status": "ok", "build": "0123456789ab", "database": "status",     "environment": "production"}
{"status": "ok", "build": "0123456789ab", "database": "status_dev", "environment": "development"}
```

- **`build`** is the release's `REVISION`, or `dev` for a hand-run server. Dev and live may run the same release, so a matching build says nothing about which port answered. Read `environment` for that (notifier#58).
- **The two probes cross-check the database.** `/health` reports it from the configured URL. `/ready` reports the one actually connected, via `current_database()`, plus `schema_state`. `/health` and `/ready` disagreeing means the running engine and `DATABASE_URL` have diverged; nothing else surfaces that.
- **Why these are unauthenticated.** The case they serve is a consumer wiring up before it has a working key. Neither the database names nor the `_dev`/`_test` suffix rule is a secret (both are published in this repo), and the ports are tailnet-only regardless.

The sweep logs `build` in every `monitor sweep complete` line.

**The production sweep asks `http://status:9000/ready` after every pass** and pings healthchecks.io's `co-status-api` with the answer (#13, [monitors.md § Who watches co-status](reference/monitors.md#who-watches-co-status)). So `journalctl -u status` has a `/ready` line every minute.
