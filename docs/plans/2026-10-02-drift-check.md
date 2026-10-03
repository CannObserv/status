---
title: Drift check — alert when live lags origin/main (#12)
date: 2026-10-02
status: in progress
---

# Drift check

Issue: [#12](https://github.com/CannObserv/status/issues/12), deferred from #9 ([spec § Deferred](../specs/2026-09-30-deploy-releases-design.md)); broker#22 goal 4. Decisions: [#12 comment](https://github.com/CannObserv/status/issues/12#issuecomment-5963239051). Builds on #1 and #13 ([plan](2026-10-01-watch-the-api.md)).

## Problem

Since #9, production changes only when someone runs `scripts/deploy.sh`. Work pushed to `main` and never deployed goes unnoticed. `/health` and every sweep line name the running build, but nothing compares it with `origin/main`.

## Approach

An hourly oneshot, **`status-drift.timer`** → `status-drift.service` → `scripts/drift.sh` → `scripts/check_drift.py`. It runs from `/srv/status/live`, reads that release's `REVISION`, asks GitHub, and pings one healthchecks.io check, **`co-status-drift`** (period 1 h, grace 2 h). Production only, and no database or notifier. If the timer stops, the check goes silent, which alerts too.

**What counts.** A lag counts only if the undeployed diff touches a path outside the ignore-list: `docs/`, `tests/`, `.github/`, `.claude/`, `skills/`, `skills-vendor/`, `.skills/`, `*.md`. A diff whose file list is truncated or missing counts.

**When it alerts.** If live has lagged `main` in a path that counts for more than **8 h** since the push that brought that path in, the check gets `/fail`.

**The calls**, all unauthenticated (the repo is public, as #11's gate relies on):

1. `GET compare/<REVISION>...main` (one call per hour):
   - `identical` → up.
   - `diverged` or `behind` (live not on `main`) → `/fail` at once.
   - `ahead` with nothing that counts → up.
2. `GET actions/workflows/ci.yml/runs?event=push&branch=main` (one call per hour; read only when the diff counts). The pushes since live are the runs whose `head_sha` is undeployed, in order of `created_at`. That time is when the push landed. Committer dates can be days older. If `main`'s newest commit has no run (`[skip ci]`), its committer date stands in.
3. Only when the oldest of those pushes is past the grace: `GET compare/<REVISION>...<push>` for each push, oldest first, until one counts. That push's time starts the clock. At most `WALK_LIMIT` calls; if none of them counts, the first push not asked about starts the clock: the earliest the code can have come (alerts sooner, never later; CR 1).

That leaves deploy.sh most of the 60 requests an hour per address. The usual hour costs two calls.

**Ping per outcome:**

| Outcome | Ping | Body |
|---|---|---|
| In sync, nothing that counts, or within grace | success | live, `main`, commits behind, the age of the push that counts |
| Past grace | `/fail` | the same, plus the CI result of `main`'s newest commit: if it failed, #11's gate refuses the deploy, and the fix is CI rather than a deploy |
| Live not on `main`, or unstamped (`dev`) | `/fail` | which |
| GitHub unreachable, rate-limited, or answering unexpected JSON | `/log` | the status code or the exception type; a sustained outage goes silent, and the silence alerts |

The ping helper moves out of `Heartbeat` into `src/core/heartbeat.py::ping` with three signals (up, `/fail`, `/log`). Same key, same redaction, and the same rule that a ping never raises.

## Tradeoffs / alternatives

- **Inside the sweep** (the issue's option 1). Rejected: GitHub would join the sweep's failure modes.
- **`/health` behind-by** (option 3). Rejected: no alert, and `/health` would have to call GitHub.
- **`git ls-remote` in the checkout.** Rejected: a release has no `.git`, and the units never touch the checkout (#9).
- **A plain grace period, no path filter.** Rejected: live lags a docs-only commit after nearly every ship (`1625627` today).
- **Path filter on the whole diff only, with the clock at the oldest push.** Rejected: a docs push followed days later by a code push would alert on the code push at once.
- **Per-commit files (`GET commits/<sha>`).** Rejected: one call per commit; a push is the unit a deploy acts on.
- **A state file of first-seen times.** Rejected: CI run times already say when each push landed.
- **Silence past grace instead of `/fail`.** Rejected: the alert would not say what is wrong.

## Steps

1. **`ping()` and `Signal`** in `heartbeat.py`, with `Heartbeat._ping` delegating to them. Existing tests stay green, plus new ones for `/log` and for redaction outside `Heartbeat`. *Done when* green.
2. **`src/core/drift.py`, pure:** `counts(path)`, `pushes(...)`, `assess(...)` → `Verdict(signal, body)`. Table tests: identical, behind, diverged, unstamped, docs-only, truncated, within grace, past grace, docs push then code push, `[skip ci]` tip, CI result in the body. *Done when* green.
3. **`src/core/drift.py`, the GitHub side:** `check()` makes the calls above through respx and handles GitHub errors (→ `/log`) and the walk limit. *Done when* green.
4. **`scripts/check_drift.py` and `scripts/drift.sh`:** key from the credential; with no key it warns and exits 0, and the silence alerts. One journal line per run. *Done when* green.
5. **Units** `deploy/status-drift.{service,timer}`, with drift tests:
   - release root, ExecStart is the release's own launcher;
   - `hc-ping-key` with the `\n` fallback, and no notifier key;
   - no `EnvironmentFile=`, no `STATUS_ALLOW_PROD_DB`;
   - `OnUnitActiveSec=1h`, `TimeoutStartSec`;
   - no dev twin.

   *Done when* green.
6. **Docs:**
   - AGENTS.md: layout row, infrastructure line;
   - RUNBOOK: check setup, unit install, alert row;
   - DEPLOYMENT.md: drift after deploy;
   - monitors.md § Who watches co-status;
   - spec § Deferred.
7. **Deploy (operator + agent).**
   1. Create `co-status-drift` in healthchecks.io (period 1 h, grace 2 h, email + Slack) before deploying.
   2. Merge, then `scripts/deploy.sh`.
   3. Install and enable the timer by hand (#18).

   *Done when* the first run pings up with the correct body and a test `/fail` reaches the channels.

## Open questions / risks

- **Shared egress address.** GitHub's unauthenticated limit is per address. If the VM shares one, other tenants spend it too. A refusal goes to `/log`, never `/fail`, so it can't page anyone. A long refusal shows up as silence.
- **A push of commits pushed earlier to another branch** gets its CI run at the push to `main`, which is the correct clock.
- **After a deploy the check stays as it was until the next run**, up to an hour. `sudo systemctl start status-drift` clears it at once.
