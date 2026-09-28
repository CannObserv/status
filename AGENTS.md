# co-status — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Lead with the answer or action.

## Project Overview

The cohort's monitoring service. MVP (#2): **dead-man's timers** taken over from notifier (CannObserv/notifier#83). A consumer checks in on a cadence; a check-in that does not arrive is the alert.

co-status **originates** alerts and **delivers none itself**. Every alert goes through notifier's `POST /api/v1/dispatch` as an ordinary notifier tenant (`co-status`), using the `notifier-client` SDK. No Apprise, no Jinja, no templates, no channel secrets here.

Long-term home for cohort monitoring: public status pages, an admin UI and active probes are expected, each with its own spec. Until then they are constraints only (see the spec, § Decisions D2, D7).

Spec: [docs/plans/2026-09-26-co-status-mvp-design.md](docs/plans/2026-09-26-co-status-mvp-design.md). Current plan: [docs/plans/2026-09-26-co-status-scaffold-and-port.md](docs/plans/2026-09-26-co-status-scaffold-and-port.md).

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first. Code copied from notifier arrives with its tests, copied first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff, PostgreSQL 16, Alembic.

`pre-commit` runs ruff only, never pytest. A clean commit hook says nothing about correctness — only CI does (`.github/workflows/ci.yml`: `lint`, `test`, `migrations`). CI pins CPython 3.12 and installs with `uv sync --locked`, because `core = "sysmon"` coverage needs both; `tests/ci/` asserts it.

## Project Layout

`src/api/` is transport (routes, schemas, auth deps); `src/core/` is domain. Core never imports api.

| Module | Role |
|---|---|
| `src/core/monitors.py` | Pure: deadlines, `should_alert` (incl. the owed-alert rule), built-in wording as `Notice` |
| `src/core/alerting.py` | **The only module that talks to notifier.** Endpoint check, idempotency keys, `send()`, request `Budget` |
| `src/core/sweep.py` | The pass that marks missing monitors and sends/owes their alerts |
| `src/core/importer.py` | One monitor in from notifier's export, disabled |
| `src/api/routes/monitors.py` | CRUD and the check-in |
| `tests/fixtures/notifier-checkin-contract.json` | notifier's check-in contract, compared by `tests/api/test_contract.py` |

Tests reach notifier through the real `notifier-client` intercepted by `respx` — the `notifier` and `alerter` fixtures in `tests/conftest.py`.

## Infrastructure

**Planned** (Phase 3, not provisioned yet): exe.dev VM `co-status` (`pdx`), tailnet node `status`, `tag:status`.

| Service | Port | Database |
|---|---|---|
| API (live) | 9000, tailnet only | `status` |
| API (dev) | 9001, tailnet only | `status_dev` |
| Sweep (live / dev) | systemd timer, 60s | `status` / `status_dev` |
| Public status pages | **8000 — reserved; nothing binds it in the MVP** | — |

**Until then, development happens on `notifier.exe.xyz`.** `status_test` lives on notifier's Postgres cluster, owned by role `status`, which has no access to notifier's databases. Dropped once co-status runs its own Postgres.

**Which notifier is derived, never configured:** production → `http://notifier:9000`, development → `http://notifier:9001`, from co-status's own database name.

**The notifier API key is a systemd credential**, never an env var (D13): `LoadCredential=notifier-key:/etc/status/notifier-api{,-dev}.key` on the API and sweep units. Without it the API records check-ins and sends nothing; the sweep refuses to start.

**The sweeps are the only thing watching for consumer silence, and nothing watches them yet** (#1): `systemctl list-timers 'status-sweep*'` is the check.

Setup, routine ops and the cutover: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Environment Variables

Two env files, loaded in order by `scripts/load_env.sh` (later values override):

1. **`/etc/status/.env`** — production secrets (`DATABASE_URL`); on the co-status VM only.
2. **`.env`** (repo root, git-ignored) — `TEST_DATABASE_URL`, `DEV_DATABASE_URL`. Never commit it.

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

**Never hand-run uvicorn.** `scripts/serve.sh` (production unit) and `scripts/dev_server.sh` (dev unit, or by hand after `sudo systemctl stop status-dev`) are the only launchers; `src/core/db_safety.py` refuses a database not ending `_test`/`_dev` unless the unit opts in with `STATUS_ALLOW_PROD_DB=1`.

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
- **`variables` is opaque.** Stored and forwarded, never read. `status` is the consumer's own judgement.
- **Never deliver directly.** Alerts go through notifier's `/dispatch`; a new delivery path here is a design change, not a fix.
- **Nothing public on 9000/9001; nothing private on 8000.**
- **A check-in is always recorded and answered.** Nothing notifier does or fails to do may cost a consumer its heartbeat.

## Detail Docs

- [docs/reference/monitors.md](docs/reference/monitors.md) — the dead-man's timer: model, API, what gets sent, the owed alert, the gap
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — first-time setup (Phase 3), routine ops, moving a monitor from notifier
- [docs/plans/](docs/plans/) — the MVP spec and the Phases 1–2 plan
