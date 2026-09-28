---
title: co-status Phases 1–2 — scaffold the repo and port the monitors
date: 2026-09-26
status: done — Phase 1 CI run 36447672609 (e96cc5f), Phase 2 CI run 36450624269 (99339fe)
---

# co-status Phases 1–2: scaffold and port

Spec: [`2026-09-26-co-status-mvp-design.md`](2026-09-26-co-status-mvp-design.md). Decision numbers (D1–D15) and section names below refer to it.

## Problem

`CannObserv/status` holds a LICENSE and the spec. Phases 3–7 (provision, drills, cutover, soak, removal) all need a service that already does everything notifier's monitors do, sending alerts through notifier. Phases 1–2 build that service. None of it needs the co-status VM.

## Approach

Build in `~/status` on the notifier VM, test-first, in small commits pushed to `main`, with CI running on every push. **Phase 1** copies notifier's infrastructure, together with its tests, until CI is green on an app serving only `/health` and `/ready`. **Phase 2** ports the monitor code with its tests, then adds what is new: `monitor_events`, `alerting.py`, the changed `should_alert` rule, the import script, and the contract snapshot. The environment-variable prefix is `STATUS_`, and the key-audit journald tag is `status-keys`.

**Test-first, and what that means for copied code.** When a module is copied, its tests are copied first, so they fail on a missing import before the code arrives. Real red-to-green applies wherever behaviour is new or changed. Every notifier issue reference in copied code or docs is rewritten as `CannObserv/notifier#N`.

**Pins:**
- `notifier-client` is `>=0.3.1,<0.4`, from git tag `v0.3.1`. That tag already has `health`, `dispatch`, `preview` and `channels.list()`, and the dispatch response has not changed since.
- The contract snapshot is taken at notifier commit `2c02dbf`. No release contains monitors.

## Tradeoffs / alternatives

- **Wait for the co-status VM.** Rejected: provisioning waits on operator prerequisites, and no code needs the VM.
- **Two plans, one per phase.** Rejected: Phase 2 builds directly on Phase 1, and the spec already fixes the design.
- **Agent tooling now** (vendored skills, SocratiCode, context hooks and the context-budget checks). Deferred to Phase 3: the index and the hooks are per-host, and they belong on the VM where sessions will run. `AGENTS.md` and `CLAUDE.md` start in step 1 and grow as the code does.
- **A branch and PR per step.** Rejected, to match notifier's practice of small green commits straight to `main`. Nothing is deployed until Phase 3.

## Steps

**Phase 1: scaffold**

1. **Skeleton.** `pyproject.toml` (dependencies meeting notifier's four-rule policy; ruff; coverage with `core = "sysmon"`, `fail_under = 80`; pytest), `uv.lock`, `.pre-commit-config.yaml`, `.gitignore`, `AGENTS.md`, `CLAUDE.md`, `README.md`. *Done when* `uv sync --locked` and `uv run pre-commit run --all-files` are clean.
2. **Core infrastructure.** Copy `db_safety` (`STATUS_ALLOW_PROD_DB`; `status` → production, `status_dev` → development), `database`, `logging` and `log_config.json` (audit tag `status-keys`), `utils`, and models `base`, `tenant` and `api_key`, plus `api_keys` and `tenants`. Set up Alembic with an initial migration for `tenants` and `api_keys`. *Done when* their ported tests pass against `status_test`.
3. **API shell.** Copy `main.py`, `deps.py` (`require_api_key`, including the 403 for a development key on production), `/health`, `/ready`, and their schemas. *Done when* the ported `test_health`, `test_deps` and `test_key_environment` pass.
4. **Scripts and units.** Copy `seed_tenant`, `rotate_key`, `delete_tenant`, `dump_openapi`, `load_env.sh`, `dev_server.sh`, `serve.sh` and `tailnet_bind.sh`. Add `status.service` and `status-dev.service` with `LoadCredential=` for the notifier key (D13), and the memory-reservation drop-ins. *Done when* the ported `tests/deploy/` and script tests pass.
5. **CI.** `ci.yml` (`lint`; `test` against a Postgres service with `fetch-depth: 0`; `migrations`), `dependabot.yml`, and the `tests/ci/` checks: lint selectors, dependencies, dependabot, workflows, and `no_channel_urls`. *Done when* a push to `main` is green. **This is the Phase 1 gate.**

**Phase 2: port and build**

6. **Domain.** The `monitor` model without `template_id`, and `monitor_event` with its kinds (see *Data model*), plus a migration. Port `monitors.py`, applying the changed `should_alert` rule and making the built-in wording a fixed template that receives its values as `variables`. *Done when* the ported `test_monitors` passes and new tests pass for the changed rule and for a name containing `{{`.
7. **`alerting.py`.** Through `respx`, test:
   - the endpoint check;
   - the deterministic idempotency keys;
   - reading `status` from the 202;
   - behaviour when notifier is unreachable;
   - the one retry after an unknown-channel 404;
   - the 8-second budget on the request path.

   *Done when* every rule in the spec's *`alerting.py`* section has a test that passes.
8. **Routes.** `/api/v1/monitors` CRUD, where `template_id` is a 422 and channels are checked against `channels.list()`, plus the five-step check-in that writes events. Add the contract fixture taken from notifier `2c02dbf` (check-in path, `CheckinRequest`, `CheckinResponse`, `DispatchOut`, `DispatchAttemptOut`) and its test. *Done when* the ported route and tenant-isolation tests, the new disabled-monitor test and the contract test all pass.
9. **Sweep.** `sweep.sh`, `sweep_monitors.py`, and the `status-sweep{,-dev}` units with `TimeoutStartSec`. *Done when* the ported `test_sweep_monitors_script` and `test_sweep_units` pass.
10. **Import and docs.** `import_monitors.py`:
    - dry run by default;
    - refuses a row with a `template_id` and an ID that already exists;
    - renames `watcher` to `co-watcher` and `watcher-backup` to `co-watcher-backup`;
    - applies the channel mapping;
    - writes `imported` and `paused`.

    Docs: `docs/reference/monitors.md`, adapted from notifier's and including *The gap* and status#1; `docs/RUNBOOK.md` with the read-only export query and the cutover steps; `AGENTS.md` brought up to date. *Done when* the import tests pass, and CI is green with coverage at 80% or more. **This is the Phase 2 gate.**

## Open questions / risks

- **A test database on this host. Needs your OK.** The fast test loop needs `status_test` on this VM's Postgres cluster, which also serves notifier's production database. My proposal: a dedicated `status` role that owns `status_test` only and has no grants on notifier's databases, dropped once the co-status VM runs Phase 3. The alternative is running database tests in CI only, which makes every red-green cycle a push.
- **Copied code drifts from notifier's copies.** This is D4's accepted cost. Each copy's commit names the notifier commit it came from, so a later diff has a base.
- **The coverage gate applies from step 1.** It measures whatever exists at the time, so thinly tested early code shows up immediately rather than at the Phase 2 gate.
