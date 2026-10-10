# co-status — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Lead with the answer or action.

## Project Overview

The cohort's monitoring service. MVP (#2): **dead-man's timers** taken over from notifier (CannObserv/notifier#83). A consumer checks in on a cadence; a check-in that does not arrive is the alert.

co-status **originates** alerts and **delivers none itself**. Every alert goes through notifier's `POST /api/v1/dispatch` as an ordinary notifier tenant (`co-status`), using the `notifier-client` SDK. No Apprise, no Jinja, no templates, no channel secrets here.

Long-term home for cohort monitoring: public status pages, an admin UI and active probes are expected, each with its own spec. Until then they are constraints only (see the spec, § Decisions D2, D7).

Spec: [docs/specs/2026-09-26-co-status-mvp-design.md](docs/specs/2026-09-26-co-status-mvp-design.md). Current plan: [docs/plans/2026-09-26-co-status-scaffold-and-port.md](docs/plans/2026-09-26-co-status-scaffold-and-port.md).

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first. Code copied from notifier arrives with its tests, copied first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff, PostgreSQL 16, Alembic.

`pre-commit` runs ruff only, never pytest. A clean commit hook says nothing about correctness — only CI does (`.github/workflows/ci.yml`: `lint`, `test`, `migrations`). CI pins CPython 3.12 and installs with `uv sync --locked`, because `core = "sysmon"` coverage needs both; `tests/ci/` asserts it.

## Project Layout

`src/api/` is transport (routes, schemas, auth deps); `src/core/` is domain. Core never imports api.

| Module | Role |
|---|---|
| `src/core/monitors.py` | Pure: deadlines, `should_alert` (incl. the owed-alert rule), `should_redeliver` and its schedule, `report_due` (one report per fault, #28), built-in wording as `Notice` (missing, recovered, cleared) |
| `src/core/alerting.py` | **The only module that talks to notifier.** Endpoint check, idempotency keys (missing, recovered, cleared), `send()`, `redeliver()`, request `Budget` |
| `src/core/sweep.py` | The pass that marks missing monitors and sends/owes their alerts; redelivers undelivered ones, spaced, until delivered or capped (#10); reports undelivered ones, the check-in's notices too, never-accepted ones included (#6, #8, #19) |
| `src/core/heartbeat.py` | healthchecks.io pings after each production pass (#1), and the API's `/ready` (#13); never fails the sweep. `ping()` for any check |
| `src/core/drift.py` | Does live lag `origin/main` in code that runs? GitHub, unauthenticated; hourly `status-drift.timer` → `co-status-drift` (#12) |
| `src/core/importer.py` | One monitor in from notifier's export, disabled |
| `src/core/schema_state.py` | Database vs the code's Alembic head; `behind` fails a pass and `/ready`, never a start (#9) |
| `src/core/build.py` | Build id = the release's `REVISION`, else `dev` |
| `src/api/routes/monitors.py` | CRUD and the check-in: records every one; opens, reports, suppresses and clears faults (#28) |
| `tests/fixtures/notifier-checkin-contract.json` | notifier's check-in contract, compared by `tests/api/test_contract.py` |

Tests reach notifier through the real `notifier-client` intercepted by `respx` — the `notifier` and `alerter` fixtures in `tests/conftest.py`.

## Infrastructure

**Provisioned 2026-09-28** (Phase 3): exe.dev VM `co-status` (`pdx`), tailnet node `status` (`100.88.216.92`), `tag:status`. Postgres 16 on localhost holds `status`, `status_dev` and `status_test`.

| Service | Port | Database |
|---|---|---|
| API (live) | 9000, tailnet only | `status` |
| API (dev) | 9001, tailnet only | `status_dev` |
| Sweep (live / dev) | systemd timer, 60s | `status` / `status_dev` |
| Public status pages | **8000 — reserved; nothing binds it in the MVP** | — |

**Units run releases, never a checkout** (#9): `/srv/status/{live,dev}` → `releases/<build>`, a read-only `git archive` of one pushed commit with its own venv. Root owns the root, `releases/` and each release (#14): changing what a unit runs takes `sudo`, which journals the command (a root shell, only as a shell). Not a boundary against `exedev`, which can sudo. Nothing done in a checkout reaches a unit until deployed. **Shipping is `scripts/deploy.sh`** (dev, then live: migrate, switch, restart, verify, switch back on failure). That replaces `shipping-work-python-fastapi`'s migrate-then-restart step. Live only once the commit's push run on `main` and every job in it passed, `lint`, `test` and `migrations` among them (#11); `--skip-ci` for an emergency, logged. **Migrations are expand-only**: the previous release must run on the new schema. Hand-run alembic against production is refused, every command that connects (#15); point it at dev. [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). `/health` is on the tailnet address, not localhost.

**Which notifier is derived, never configured:** production → `http://notifier:9000`, development → `http://notifier:9001`, from co-status's own database name.

**The notifier API key is a systemd credential**, never an env var (D13): `LoadCredential=notifier-key:/etc/status/notifier-api{,-dev}.key` on the API and sweep units. Without it the API records check-ins and sends nothing; the sweep refuses to start.

**The sweeps are the only thing watching for consumer silence; healthchecks.io watches the production one** (#1): checks `co-status-sweep` and `notifier-reachable`, key `LoadCredential=hc-ping-key:/etc/status/hc-ping.key` on `status-sweep.service` and `status-drift.service` only, with a `SetCredential=` fallback so a missing key never stops the sweep. Local check: `systemctl list-timers 'status-sweep*' status-drift.timer`.

**The production API is watched by that sweep, not `OnFailure=`** (#13): each pass asks `http://status:9000/ready` and pings `co-status-api`. Dev units are deliberately unwatched.

**What is deployed is checked hourly** (#12): `status-drift.timer` pings `co-status-drift`, `/fail` once live has lagged `origin/main` in code (not docs or tests) for 8 h since the push. **Units ship with their release** (#18): each target's step installs that target's units that differ (`*-dev` are dev's), and a switch back restores them; new units are never enabled. Host configs under `deploy/` are compared after a live deploy, never installed.

Setup, routine ops and the cutover: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Environment Variables

Two env files, loaded in order by `scripts/load_env.sh` (later values override):

1. **`/etc/status/.env`** — production secrets (`DATABASE_URL`); on the co-status VM only.
2. **`.env`** (repo root, git-ignored) — `TEST_DATABASE_URL`, `DEV_DATABASE_URL`. Never commit it. For pytest and hand-run servers; no unit reads it.

Units read `/etc/status/` only: live `.env`, dev `dev.env` (`DEV_DATABASE_URL`).

Source them with `. scripts/load_env.sh`; never word-split through `xargs`.

## Common Commands

```bash
uv sync
. scripts/load_env.sh
uv run pytest
uv run pytest --no-cov tests/path/test_x.py   # a subset; skips the coverage gate
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files
uv run pre-commit install                     # once per clone
```

## Server Lifecycle

**Never hand-run uvicorn.** `scripts/serve.sh` (production unit) and `scripts/dev_server.sh` (dev unit, or by hand from a worktree after `sudo systemctl stop status-dev`) are the only launchers; `src/core/db_safety.py` refuses a database not ending `_test`/`_dev` unless the unit opts in with `STATUS_ALLOW_PROD_DB=1`. Launchers `uv run --frozen --no-sync`: in a checkout, `uv sync` yourself.

## Agent Skills

Vendored as submodules under `skills-vendor/` (read-only; change upstream). Each skill is a symlink in `skills/` (agentskills.io), re-linked from `.claude/skills/` (Claude Code); a committed directory in `skills/` overrides the vendored copy in both. None overridden yet. Procedure: the `managing-skills` skill.

- **`gregoryfoster/skills`** (`skills-vendor/gregoryfoster-skills/`): `curating-context`, `enforcing-architecture`, `init-socraticode`, `managing-skills`, `orchestrating-issue-backlog`, `reviewing-architecture`, `reviewing-code-python-fastapi`, `shipping-work-python-fastapi`, `using-git-worktrees`, `using-mayfly-chat`, `writing-plans`.
- **`obra/superpowers`** (`skills-vendor/obra-superpowers/`): `brainstorming`, `dispatching-parallel-agents`, `subagent-driven-development`, `systematic-debugging`, `test-driven-development`, `verification-before-completion`, `writing-skills`. Its `writing-plans` and `using-git-worktrees` are not linked; gregoryfoster's hold those names.

**Specs in `docs/specs/`, plans in `docs/plans/`**, never `docs/superpowers/`. A spec (`YYYY-MM-DD-<topic>-design.md`, from `brainstorming`) is the design: what, why, the decisions. A plan (`YYYY-MM-DD-<topic>.md`, from `writing-plans`) is how one piece of a spec gets built: phases, steps, tests; it links its spec. This is the stated preference `brainstorming` defers to, which is why it is not overridden.

Dangling symlinks (fresh clone, new worktree): `bash .skills/doctor.sh`.

## Conventions

**Commit messages:**
```
#<number> [type]: <description>      # with issue (this repo's numbers)
[type]: <description>                # without issue
```
Types: feat, fix, refactor, docs, test, chore. Notifier issues are written `notifier#N` in code and docs (`CannObserv/notifier#N` in GitHub text, where it links).

**Date & time:** all UTC. ISO 8601: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (timestamps), `YYYY-MM-DD` (dates).

**Dependencies:** every specifier carries an upper bound; a `0.x` dependency caps at the next minor above the locked version; every runtime dependency has an importer under `src/`.

**General:**
- No inline module imports; all at file top
- Docstrings for public modules, classes, functions
- Test structure mirrors source (`src/foo.py` → `tests/test_foo.py`)
- Small, focused functions

## API Boundary Principles

- **The check-in contract is frozen** (D6): `POST /api/v1/monitors/{id}/checkin`, request and response identical to notifier's at CannObserv/notifier@2c02dbf. Consumers switch by base URL and key alone.
- **`variables` is opaque.** Stored and forwarded, never read; only refused when not JSON (`NaN`, `Infinity`: a 422, #31). `status` is the consumer's own judgement. The one key read from a check-in is the opt-in `metadata.fault`, compared, never interpreted (#28).
- **Never deliver directly.** Alerts go through notifier's `/dispatch`; a new delivery path here is a design change, not a fix.
- **Nothing public on 9000/9001; nothing private on 8000.**
- **A check-in is always recorded and answered.** Nothing notifier does or fails to do may cost a consumer its heartbeat.

## Detail Docs

- [docs/reference/monitors.md](docs/reference/monitors.md) — the dead-man's timer: model, API, what gets sent, the owed alert, who watches the sweep
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — first-time setup (Phase 3), routine ops, moving a monitor from notifier
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — releases, `deploy.sh`, rollback, the schema check, health checks
- [docs/specs/](docs/specs/) — the MVP spec; the deploy spec (#9)
- [docs/plans/](docs/plans/) — the Phases 1–2 plan and later ones
