---
title: Watch the watchdog — healthchecks.io pings from the production sweep (#1)
date: 2026-09-29
status: approved
---

# Watch the watchdog

Issue: [#1](https://github.com/CannObserv/status/issues/1); decision recorded in [its review comment](https://github.com/CannObserv/status/issues/1#issuecomment-5894477297). Spec: [D10, The gap](../specs/2026-09-26-co-status-mvp-design.md).

## Problem

Nothing watches co-status. If the production sweep stops (timer, VM, Postgres, OOM killer), every dead-man's timer stops with it and nothing says so. And while notifier is down, no alert reaches anyone. The soak's daily manual check is the only watcher, and it ends 2026-10-05. After Phase 7, notifier no longer keeps fallback copies of the monitors.

## Approach

Every production sweep pass pings healthchecks.io, free plan, org account, with alerts on its own email and Slack channels and never through notifier.

- **`co-status-sweep`**: a success ping after a pass completes and commits, and `/fail` when a pass raises. The body is the pass's counts only.
- **`notifier-reachable`**: a success ping when the pass's existing `/health` endpoint check passed and nothing was left `owed`. Otherwise `/fail`. No extra request to notifier.
- **URL:** `https://hc-ping.com/<ping-key>/<slug>`, one secret for both checks.
- **Module:** `src/core/heartbeat.py`, not `alerting.py`. Short timeout; every error is logged and swallowed, so a ping never fails a sweep.
- **Production only.** The ping key is a systemd credential on `status-sweep.service` alone (D13). The dev sweep has no key and pings nothing.

**A missing key must not stop the sweep.** `LoadCredential=` on a missing file fails the unit (243/CREDENTIALS; tested on this host's systemd 255). An empty `SetCredential=` is no fallback either. The unit therefore declares `SetCredential=hc-ping-key:\n` alongside `LoadCredential=hc-ping-key:/etc/status/hc-ping.key`: the file wins when present, otherwise the credential is a lone newline, which reads as "no key" (both cases verified). A missing key then shows as healthchecks alerting, which is loud, and never as a stopped sweep, which is silent.

## Tradeoffs / alternatives

- **B, a watcher inside the cohort** (e.g. broker probing co-status). Rejected: its alerts route through notifier or co-status, so the loop never closes.
- **C, leave the gap.** Rejected: Phase 7 leaves co-status as the only watcher.
- **The ping URL in `/etc/status/.env`**, as the issue first suggested. Rejected: an env file is inherited by every shell that sources it (D13), and a leaked key can report a dead sweep as alive.
- **`ImportCredential=` from `/etc/credstore`**, which tolerates a missing file. Rejected: every co-status secret lives in `/etc/status/`, and the `SetCredential=` fallback keeps it there.
- **A second `/health` request** for `notifier-reachable`. Rejected: the sweep already makes one, and "owed is empty" is the stronger signal because it also means notifier *accepted* the alerts.

## Steps

1. **Credential reader.** Factor `read_notifier_key`'s body into `src/core/credentials.py::read_credential(name, environ)`; `read_notifier_key` delegates to it. *Done when* the existing alerting tests plus new credential tests pass, including "whitespace-only reads as absent".
2. **`SweepReport.notifier_ok`.** `sweep_monitors` records whether the endpoint check passed. *Done when* `tests/core/test_sweep.py` asserts it both ways.
3. **`src/core/heartbeat.py`.** Add `Heartbeat` (`sweep_completed(report)`, `sweep_failed(reason)`) and `heartbeat_from_environment()`, which returns `None` without a key or outside production. Tests go through respx on `hc-ping.com`: the URLs, `/fail`, the counts body, and errors and non-2xx answers swallowed. *Done when* green.
4. **Wire the entrypoint.** `run_sweep` takes an optional heartbeat, pings after commit, and sends `/fail` and re-raises on an exception. `main()` builds the heartbeat and sends `/fail` when there is no alerter. *Done when* `tests/test_sweep_monitors_script.py` covers success, failure and no heartbeat.
5. **Unit.** Add `SetCredential=` and `LoadCredential=` for `hc-ping-key` to `status-sweep.service` only. Drift tests: production has both, the fallback comes first, the file is under `/etc/status/`, the dev units have neither, and no `Environment*=` line mentions it.
6. **Docs.** monitors.md § The gap becomes § Who watches the sweep. RUNBOOK gets the setup (key file, checks, channels) and routine ops. AGENTS.md gets the module table row and the infra line. Spec D10 gets a closing note.
7. **Deploy (operator + agent).** In the healthchecks UI: the two checks, period 1 min, grace 5 min, email and Slack. Write the key to `/etc/status/hc-ping.key` (root, 0400). Then `cp` the unit, `daemon-reload`, and confirm both checks go green. *Done when* both checks go green, and one `/fail` test ping reaches the channels.

## Open questions / risks

- **The ping key never passes through chat or the agent.** The operator writes the file (RUNBOOK); the agent only verifies that pings land.
- **healthchecks.io is a third-party dependency outside the cohort.** An outage there sends false alarms, never silence, which is the acceptable direction.
- **notifier#56 (notifier's own gap)** is covered only as far as `notifier-reachable` reaches. A suggested notifier issue, not work here.
