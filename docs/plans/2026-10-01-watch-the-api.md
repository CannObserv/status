---
title: Watch the API — the production sweep probes /ready (#13)
date: 2026-10-01
status: done
---

# Watch the API

Issue: [#13](https://github.com/CannObserv/status/issues/13), deferred from #9 ([spec § Deferred](../specs/2026-09-30-deploy-releases-design.md)) and broker#22 Q10. Builds on #1 ([plan](2026-09-29-watch-the-watchdog.md)).

## Problem

A failed production API is invisible. `status.service` retries five times and then stays `failed`, and nothing pings or alerts. Consumers' check-ins fail. After their grace, the sweep reports the *consumers* `missing` through notifier, a false alarm, when the fault is co-status.

## Approach

The issue's option 2. After each production pass, the sweep sends `GET http://status:9000/ready`, the API by the name consumers use, over the tailnet. It then pings a third healthchecks.io check, **`co-status-api`**:

- **Success** when `/ready` answers 200 with `environment` `production`. Body: `/ready`'s JSON.
- **`/fail`** otherwise. Body: the status code and response, or the error and its message (the URL carries no key, and the message tells nothing listening from a name that won't resolve). It is also a journal warning.
- **Retried every 2 s for up to 20 s** before the API counts as down. The last deploy's forced pass ended at 2026-10-01 14:54:10.73, 0.2 s before uvicorn listened, so a single try would alert on every deploy.
- **After the pass and its own pings, whatever the pass did** (`finally`). A slow API never delays an alert. With Postgres down, both checks fail and both tell the truth.
- Same key, module (`src/core/heartbeat.py`) and rules as #1: production only, never raises, bounded.

**Dev is deliberately not alerted.** The dev sweep pings nothing (#1), so it probes nothing, and no dev unit gets `OnFailure=`. `deploy.sh` verifies `:9001` on every deploy, and dev's callers see their own failures.

## Tradeoffs / alternatives

- **Option 1, `OnFailure=status-failed@%n.service` pinging `/fail`.** Rejected. Under `Restart=` it fires on every crash, even one the restart fixes, because systemd 255's default `RestartMode=normal` passes through `failed`. With `RestartMode=direct` it fires only once the unit gives up, about 5 minutes into the crash loop. It never fires for an API that is up but not ready. A check fed only `/fail` never comes back up by itself, so it needs a success source anyway, which would be the sweep. It would also put the ping key in a second unit. The probe already covers a failed unit, because a failed unit cannot answer `/ready`.
- **Both.** Rejected: once the API has been down for a pass, the probe has already alerted. If the sweep is dead too, `co-status-sweep` is already alerting.
- **Silence instead of `/fail`**: skip the success ping, `/log` the reason, and let the grace alert. Rejected: it alerts 6 minutes later for the same false-alarm protection the 20 s retry gives, and it breaks the existing checks' rule that a pass which sees a fault says so.
- **The address from `tailscale ip -4`** instead of the MagicDNS name. Rejected: consumers resolve `status`, so the probe resolves it too.
- **An external check of `:9000/ready`.** Ruled out in the issue: nothing outside can reach the tailnet.

## Steps

1. **`Heartbeat.api_checked()`** in `src/core/heartbeat.py`: probe, retry window, ping `co-status-api`. Tests go through respx on `status:9000` and `hc-ping.com`: ready, 503, the wrong environment, a body that is not JSON, unreachable throughout, recovering within the window, a stall cut off at the window, and a ping that never raises. *Done when* green.
2. **Wire `scripts/sweep_monitors.py::main`.** It calls the check in a `finally`, after the pass's own pings. Tests cover a completed pass, a raised pass, no notifier key, and the order. *Done when* green.
3. **Docs.** monitors.md § Who watches the sweep becomes Who watches co-status. RUNBOOK gets the third check's setup, its alert row and what to do, and the dev policy. AGENTS.md and the unit comments: why `status.service` has no `OnFailure=`, and that the dev units are unwatched by design.
4. **Deploy (operator + agent).** Create `co-status-api` in healthchecks.io (period 1 min, grace 5 min, email + Slack) **before** deploying. Until it exists, every ping answers 404 and the sweep warns on each pass. Then run `scripts/deploy.sh`. *Done when* the check goes green and one `/fail` test ping reaches the channels. **Done 2026-10-01 18:12Z**: `b4e6809f402e` live and dev. The first `co-status-api` ping was answered 200 at 18:12:50. A test `/fail` at 18:14:50 was cleared by the next pass at 18:14:55.

## Open questions / risks

- **Pass length.** `TimeoutStartSec=120` now also covers up to 20 s of probing and one more ping (5 s). A normal pass takes about 1 s. A deploy whose new API never comes up spends those 20 s in its forced pass, which still fits inside `SWEEP_WAIT_SECONDS=150`.
- **`journalctl -u status` gets a `/ready` access line every pass**, and more while retrying.
- **A deploy that fails verification alerts** (down, then up once `deploy.sh` switches back). That is a real event, and it is reported as one.
