# Deploying co-status

How code reaches the units, and how to tell what is running. Design and reasons: [the deploy spec](specs/2026-09-30-deploy-releases-design.md) (R1–R13). Setup and routine ops: [RUNBOOK](RUNBOOK.md).

## Releases, not a checkout

```
/srv/status/
  releases/<build>/   git archive of one pushed commit + its own .venv; read-only; REVISION
  live -> releases/<build>   status.service, status-sweep.service   (:9000, database status)
  dev  -> releases/<build>   status-dev.service, status-sweep-dev.service (:9001, status_dev)
```

- **No unit runs `/home/exedev/status`.** On 2026-09-29 they all did, and an unmigrated model edit crashed the production sweep for 40 minutes ([#9](https://github.com/CannObserv/status/issues/9)). Saving, committing, switching branches or running `uv sync` in a checkout now changes nothing a unit runs.
- **A release is one commit, built once.** `<build>` is the commit's 12-character short SHA. `REVISION` is written last, so a directory without one is an interrupted build and gets rebuilt. `live` and `dev` share a release when they run the same commit.
- **Units never sync.** They `uv run --frozen --no-sync` the venv the deploy built.
- **Env comes from `/etc/status/` only.** Live units read `.env`; dev units read `dev.env` (`DEV_DATABASE_URL`). No unit reads a repo `.env`.

## `scripts/deploy.sh`

Run it as `exedev` from any checkout. It fetches `origin` itself.

```bash
scripts/deploy.sh                       # origin/main to dev, then live
scripts/deploy.sh <sha>                 # a specific main commit (a rollback, say)
scripts/deploy.sh --dev origin/<branch> # any pushed branch, :9001 only
scripts/deploy.sh --help
```

**What goes live** is a commit on `origin/main`. Dev takes any commit on an `origin/*` branch. Anything unpushed is refused.

**Order, per target, dev first:**

1. **Migrate** the target's database. This is skipped when the database is *ahead* of the release, which is what a rollback looks like.
2. **Switch** the symlink (an atomic rename).
3. **Restart** the API, then force one sweep pass. Any pass already running started on the old release, so the deploy first waits for it to end: `systemctl start` on a oneshot mid-pass joins that pass rather than starting another. `systemctl start` then waits for the new pass to finish.
4. **Verify.** The pass must exit 0, `/ready` must be 200, and `/health` must report `build` equal to `<build>`, all within 60 s.
5. **On failure,** switch back, restart, and exit non-zero naming the step. The migration stays applied.

A failure on dev stops the deploy before live is touched. The deploy keeps the 5 most recently deployed releases, plus whatever `live` and `dev` point at. Every switch and rollback is logged: `journalctl -t status-deploy`.

## Rules the design depends on

- **Migrations are expand-only.** The previous release must keep working against the new schema: it runs there between migrate and switch, and after any rollback. Add columns and tables, with defaults or nullable. A drop, a rename, or a new `NOT NULL` without a default ships as two deploys: first stop using it, then remove it.
- **One Alembic head.** A release with two heads is refused before anything switches.
- **Deploy is the only way in.** The shipping skill's "migrate, then `systemctl restart`" step does not apply here: `deploy.sh` does both, in order, and verifies.

## Rollback

```bash
journalctl -t status-deploy -n 20      # "live -> <build> (was releases/<old>)"
scripts/deploy.sh <old build>          # still on origin/main, so it may go live
```

The old release still exists (within the 5 kept), so nothing is rebuilt. Its Alembic does not know the newer revision, so the schema reads `ahead` and the migration is skipped. By the expand-only rule, the old code runs. A rollback never downgrades the schema.

## The schema check

`src/core/schema_state.py` compares `alembic_version` with the release's head:

| State | Sweep pass | `/ready` | `dev_server.sh` |
|---|---|---|---|
| `current` | runs | 200 | starts |
| `ahead` (older code, newer schema) | runs, logs a warning | 200 | starts |
| `behind` | fails: `SchemaBehind`, `/fail` ping | 503, names the database | refuses; prints the migration |
| `unmigrated` | fails, as `behind` | 503 | refuses |

Nothing refuses to start a unit: a refused API start would record no check-ins at all. `python -m src.core.schema_state` prints the state, and exits 0 for `current` or `ahead`, 3 for `behind` or `unmigrated`, and 2 for anything else (unreachable, refused by `db_safety`, crashed). `deploy.sh` migrates only on a state it printed.

## Development

Work in a worktree (the `using-git-worktrees` skill). To run your code on :9001:

- **Pushed:** `scripts/deploy.sh --dev origin/<branch>`, which gives it the full dev path, migration included.
- **Unpushed, fast loop:** `sudo systemctl stop status-dev`, then `scripts/dev_server.sh` from the worktree. It reloads on save and uses the repo `.env`'s `DEV_DATABASE_URL`. Run `uv sync` there yourself, because the launchers never sync. When you are done, redeploy dev or `sudo systemctl start status-dev`.

A later `deploy.sh` restarts `status-dev`, so stop the hand-run server first.

## Health checks

**On this VM, `curl http://127.0.0.1:9000/health` fails.** The units bind the tailnet address alone:

```bash
curl "http://$(tailscale ip -4):9000/health"     # this VM
curl http://status:9000/health                   # any other tailnet node
```

```json
{"status": "ok", "build": "0123456789ab", "database": "status",     "environment": "production"}
{"status": "ok", "build": "0123456789ab", "database": "status_dev", "environment": "development"}
```

- **`build`** is the release's `REVISION`, or `dev` for a hand-run server. Dev and live may run the same release, so a matching build says nothing about which port answered. Read `environment` for that (notifier#58).
- **The two probes cross-check the database.** `/health` reports it from the configured URL. `/ready` reports the one actually connected, via `current_database()`, plus `schema_state`. `/health` and `/ready` disagreeing means the running engine and `DATABASE_URL` have diverged; nothing else surfaces that.
- **Why these are unauthenticated.** The case they serve is a consumer wiring up before it has a working key. Neither the database names nor the `_dev`/`_test` suffix rule is a secret (both are published in this repo), and the ports are tailnet-only regardless.

The sweep logs `build` in every `monitor sweep complete` line.
