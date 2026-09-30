---
title: Deploy releases — production stops running the working tree (#9)
date: 2026-09-30
status: in progress
---

# Deploy releases

Issue: [#9](https://github.com/CannObserv/status/issues/9). Spec: [2026-09-30-deploy-releases-design](../specs/2026-09-30-deploy-releases-design.md), decisions R1–R13. Cohort parent: [broker#22](https://github.com/CannObserv/broker/issues/22).

## Problem

Units run `/home/exedev/status`, working tree included. On 2026-09-29, an unmigrated model edit crashed the production sweep for 40 minutes. The production units also load the repo `.env`, which carries PATs and API keys.

## Approach

- **Releases, not a checkout.** Immutable release directories under `/srv/status`, built from pushed commits by `scripts/deploy.sh`.
- **Units:** they point at the `live` and `dev` symlinks, run with `uv run --frozen --no-sync`, and read env from `/etc/status/` only.
- **A schema check** that fails a sweep pass and `/ready` when the database is behind the code, and never blocks a start.
- **Build id:** the release's `REVISION`.

## Tradeoffs / alternatives

- **In-place checkout at `/srv/status`.** Rejected: it races the 60-second sweep, and a failed sync leaves production half-installed (R4).
- **observo's exact-match boot check.** Rejected: it breaks migrate-then-switch and rollback (R8).
- **A start-time checkout guard (replicator).** Rejected: it trades drift for refused restarts (replicator#94).
- **Worktree discipline alone.** Rejected: it is today's convention, and it failed on 2026-09-29.

## Steps

Each is TDD, in the worktree `9-deploy-releases`. The development checkout is still production until step 8.

1. **Build id** (`src/core/build.py`, R10). `REVISION` is read, else `dev`. `/health` uses it. *Done when* the health tests are rewritten against a temp `REVISION`, and the sweep's line carries `build`.
2. **Schema check** (`src/core/schema_state.py`, R8). Covers `classify`, `code_head` (one head), `database_revision` (a missing table gives `None` without aborting the transaction) and the CLI's exit codes. The test engine stamps `alembic_version` at head, as a migrated database would have. *Done when* it is unit tested against the repo's real `alembic/`.
3. **Wire the check.**
   - `run_sweep` raises `SchemaBehind` inside its `try`, which also sends `/fail`.
   - `/ready` returns 503 with `schema` when behind, and carries `schema` when ready.
   - `dev_server.sh` calls the CLI instead of `alembic current`.

   *Done when* each path has a test.
4. **Launchers** (R5). `serve.sh`, `sweep.sh` and `dev_server.sh` `cd -P`, and use `uv run --frozen --no-sync`. *Done when* the deploy tests assert both.
5. **Units** (R2, R10, R11).
   - `WorkingDirectory` and `ExecStart` under `/srv/status/{live,dev}`.
   - No git stamp and no `/run/status`.
   - No repo `.env`; the dev units get `/etc/status/dev.env`.

   *Done when* the drift tests hold: no `/home/exedev`, no `rev-parse`, and live env files limited to `/etc/status/.env`.
6. **`scripts/deploy.sh`** (R3, R4, R6, R9, R13). *Done when* `tests/deploy/test_deploy.py` runs it against a temp root, a temp origin and stubs for `uv`, `systemctl` and `curl`, and covers:
   - refusing an unpushed or off-main sha;
   - building once and reusing the release;
   - rebuilding an interrupted build;
   - a read-only release;
   - dev before live;
   - skipping the migration when `ahead`;
   - rollback on a failed verify, and on a failed sweep;
   - `--dev` alone;
   - `--no-restart`;
   - pruning;
   - refusing root;
   - the lock.
7. **Docs.**
   - AGENTS.md: Infrastructure, Server Lifecycle and the shipping override. The "this checkout is the deployment" warning goes.
   - RUNBOOK: setup and Routine ops.
   - New `docs/DEPLOYMENT.md`.
   - monitors.md, and the `/health` docstrings.
8. **Cutover** (spec § Cutover). Merge, push, CI green, then deploy and switch the units. Verify `build`, `/ready`, the timers, and healthchecks.io. Comment on #2 and #9.

## Open questions / risks

- **The cutover restarts both APIs.** A check-in during the few seconds of restart gets a connection error, which consumers retry (broker, watcher and index all retry on their next cadence).
- **A forced sweep pass during a deploy sends a real heartbeat.** That is intended.
- **`uv sync` at build needs GitHub** for `notifier-client`'s git source. A build fails before anything switches, so an outage there blocks a deploy and never breaks production.
