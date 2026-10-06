---
title: Redeliver undelivered missing alerts through notifier (#10)
date: 2026-10-06
status: done
---

# Redeliver undelivered missing alerts through notifier

Issue: [#10](https://github.com/CannObserv/status/issues/10). Builds on #6 (`last_alert_status`, `undelivered`) and closes the gap #7 left. notifier side: CannObserv/notifier#96, released in notifier v0.3.2. The issue's [v0.3.2 comment](https://github.com/CannObserv/status/issues/10) outranks its body. Its § 2 correction, error table and pacing advice are what this plan follows.

## Problem

A missing alert that notifier accepted but didn't deliver (`failed`, `partial`) is reported (#6) and never resent (#7). `notifier-reachable` stays down until the monitor recovers or a renotify gets through. For the two production monitors on a 24 h renotify, that can be a day. notifier v0.3.2 can now retry just the failed channels of one dispatch (`redeliver()`), with no new key and no repeat to channels that already got it. Those were #7's two blockers.

## Approach

- **Pin** `notifier-client>=0.3.2,<0.4` and tag `v0.3.2`, then `uv lock`. Nothing co-status already imports changes.
- **Which dispatch to retry: `monitors.last_alert_dispatch_id`** (§ 2 of the comment). `monitor_events.dispatch_id` holds only the first crossing's dispatch. It is null when that alert was owed, and a renotify writes no event. `_alert` sets the new column next to `last_alert_status`, from the same `Delivery`, and clears it on a first crossing as it clears the status.
- **Pacing: `monitors.last_alert_redeliver_at`**, when the next redelivery is due, or null for none. It is computed from the returned `DispatchOut`: newest attempt's `started_at` + `REDELIVERY_DELAYS[attempt − 1]`. The delays are **1, 5, 15, then 60 min** (the last one repeats). The original send is attempt 1, so retries go out at about +1, +6, +21 and +81 min. Then one more call at +141 gets the 409. The SDK has no `GET /dispatch/{id}`, so the attempt log is unreadable between redeliveries. Storing the due time is the least state that keeps the spacing anchored on notifier's own `started_at`. It is set after the send and after every redelivery. A `succeeded` result sets it to null, and so does a 409.
- **The cap stays notifier's.** co-status doesn't count to 5. It keeps going until notifier answers 409, which costs one extra call 60 min after attempt 5. In exchange, nothing here has to change if notifier changes its cap.
- **`Alerter.redeliver(dispatch_id) -> Delivery`** in `src/core/alerting.py`, mapped as `send` maps errors:

  | notifier answers | raises / returns | the sweep then |
  |---|---|---|
  | 202 (any status, including unchanged) | `Delivery` | stores `status` and the next due time; on `succeeded` the monitor leaves `undelivered` |
  | 409 | `RedeliveryCapped` (an `AlertRejected`), with `channel_ids` | `redeliver_at = null`: **terminal for this dispatch**. Still reported `undelivered`, so a person is paged (#6) |
  | 404 | `AlertRejected(status_code=404)` | `redeliver_at = null`: the dispatch is gone, and asking again won't bring it back |
  | other 4xx | `AlertRejected` | logged as an error and left due, so it's tried next pass (as an owed `send` is) |
  | transport, 5xx, 429 | `NotifierUnavailable` | left due, so it's tried next pass. The SDK never auto-retries `redeliver()` |

  An empty `dispatch_id` never reaches the SDK. `segment()` raises `ValueError` on one, so the sweep only redelivers when the id is set.
- **Monitors with `renotify_seconds` set are included** (the issue's open question). The comment suggests leaving them out *if the #7 split still holds*. It doesn't: #7 excluded them because a retry needed a new key and re-alerted channels that had succeeded, and `redeliver()` does neither. Two of the four production monitors renotify at 24 h. Leaving them out would keep a failed alert unretried for a day. A renotify replaces `last_alert_dispatch_id` and `last_alert_redeliver_at`, so the old dispatch's chain just ends there. The cost is a possible duplicate on a channel that was retried shortly before a renotify went to every channel. With hours-long renotify periods that's rare, and it's at-least-once either way.
- **Sweep only, never the check-in path** (D6: a check-in is always answered within its 8 s budget, and a redelivery can take about 8 s per failed channel). It runs as a second loop **after** every overdue monitor has been alerted, so a new alert never waits behind a retry. It covers monitors that are `missing`, enabled, not `succeeded`, have a dispatch id, and have `redeliver_at <= now`, oldest due first. It's skipped when `/health` failed at the start of the pass, the same as sends. A monitor alerted in this pass isn't due yet (+1 min). `undelivered` is computed after this loop, so a `succeeded` redelivery clears `notifier-reachable` **in the same pass**.
- **Bounded per pass.** `TimeoutStartSec=120`. No new redelivery starts more than `REDELIVERY_WINDOW` (45 s) into the pass. It's counted from the pass's start, so slow sends use it up too (CR 8). With the heartbeat's 35 s, that leaves 30 s of the 120 s for the redelivery in flight, and a unit test holds that. The rest stay due for the next pass. A call that's already in flight is never cancelled: a cancelled redelivery could still land on notifier's side and use up an attempt with nobody to record it.
- **Report:** `SweepReport.redelivered` (monitor id → status after the redelivery, or `capped`) goes on the journald line, and its count goes in the `co-status-sweep` ping body (CR 1). `notifier-reachable` is unchanged: it still fails on `undelivered`, and a capped alert stays in it.
- **Migration: one revision, two nullable columns, no default** (expand-only). The running release ignores them. A monitor already undelivered at deploy has no dispatch id, so it isn't redelivered. It clears as before, on recovery or a delivered renotify.

## Tradeoffs / alternatives

- **Retry every pass.** Rejected: that uses up the cap in about 4 minutes, and channels stay down longer than that.
- **Store the attempt number and `started_at` instead of the due time** (two columns, schedule applied when read). The schedule could change without touching any row, but it needs a third column. The due time is derived from the same facts, and changing the schedule only affects due times computed after the change. Rejected as more state for no current need.
- **Count attempts locally and stop at 5 without the 409 call.** Rejected: it duplicates notifier's cap.
- **Exclude `renotify_seconds` monitors.** Rejected, above.
- **Redeliver recovery and report notices too.** Out of scope: #8 decided nothing resends them, and they have no pass of their own. That would be a separate issue.

## Steps

1. **Pin bump** + `uv lock`. Full suite green on v0.3.2.
2. **Model + migration** (test first): the two columns on `Monitor`, an Alembic revision, `alembic check` clean.
3. **`Alerter.redeliver`** (tests first, `tests/core/test_alerting.py`, through respx): 202 returns a `Delivery`; 409 raises `RedeliveryCapped` with the channel ids; 404 and other 4xx raise `AlertRejected`; 5xx, 429 and transport errors raise `NotifierUnavailable`; it is called once, with no auto-retry.
4. **Pure schedule** (tests first, `tests/core/test_monitors.py`): `REDELIVERY_DELAYS`, `redelivery_due(attempt, started_at)`, and `should_redeliver(monitor, now)`. `Delivery.latest_attempt`: the highest attempt number and the newest `started_at` across all attempts, which can come from different entries. It's `None` when `attempts` is empty, and the sweep then counts from its own `now` as attempt 1.
5. **Sweep** (tests first, `tests/core/test_sweep.py`; `FakeNotifier` gains a `redeliver` route): `_alert` stores the id and due time, and clears both on a first crossing. A due monitor is redelivered, and one that isn't due yet is not. A `succeeded` redelivery takes it out of `undelivered` in the same pass. A `failed` one sets the next due time from the returned attempts. A 409 stops redelivery and keeps it reported. A 404 stops redelivery. An unreachable notifier leaves it due. Renotify monitors are included, and a renotify replaces the chain. Monitors that are recovered, disabled, have no dispatch id, or are `succeeded` are skipped. Nothing is redelivered when `/health` failed. The window defers the rest. The check-in route never calls redeliver.
6. **Against dev notifier `:9001`**: a dev monitor on `dev-sink-failing`. The sweep sends attempt 1, then redelivers on schedule until the 409. Faster: call `Alerter.redeliver` directly against `:9001` with a fresh dispatch and expect `failed` ×4, then 409. Then the `succeeded` path through a scratch channel PATCHed from a closed port to a working sink (§ 6 of the comment).
   **Done 2026-10-06, against `:9001` through the real `Alerter`:** a dispatch to `dev-sink-failing` returned `failed` (attempt 1). Four redeliveries returned `failed` (attempts 2–5), and the fifth raised `RedeliveryCapped` naming `dev-sink-failing`. A scratch channel at `json://127.0.0.1:1/` returned `failed`. After a PATCH to `syslog://`, the redelivery returned `succeeded` (attempt 2), and a second one returned it unchanged. notifier won't delete a channel whose attempts are referenced, so two `co-status-10-scratch` channels stay in the dev tenant. The sweep's schedule was checked on dev **before merge** (`scripts/deploy.sh --dev origin/10-redeliver`, `74f8a1a`, migration `b3344354c124` applied), with a scratch monitor on `dev-sink-failing` in `status_dev`:
   - send (attempt 1) at 20:26:13Z;
   - redelivered at 20:27:13 (+1 min), 20:32:21 (+5 min, on the first pass after it was due) and 20:47:28 (+15 min); next due 21:47:28 (+60 min).

   Each due time is the returned attempt's `started_at` plus its delay, not the 60 s timer. Every pass's journal line showed `redelivered: {…: "failed"}`, and the monitor stayed in `undelivered`.
7. **Docs:** monitors.md § Accepted is not delivered (drop "Nothing is resent"), sweep/alerting docstrings, the model's column comments, and the AGENTS.md layout rows.
8. **Ship:** CI on the branch, merge on an explicit OK, then `scripts/deploy.sh` (migration on dev, then live).

   **Done 2026-10-06.**
   - Merged as `971e5b3..ee3f650` after three review rounds (CR 1–16; #22 filed from CR 16). CI passed on `main`. Live at 22:46Z through the CI gate, and migration `b3344354c124` ran there.
   - Live: `/health` names `ee3f6507a36f`, and `/ready` says `schema_state` `current`. The first pass logged `redelivered: {}` and `undelivered: {}` with no traceback, and pinged `co-status-sweep`, `notifier-reachable` and `co-status-api` successfully. No production monitor has a dispatch id yet, so nothing is due.
   - The dev chain ran to the end: attempt 5 at 21:47:29 (+60 min), then a 409 at 22:47:46. The journal line showed `redelivered: {…: "capped"}`, the due time was cleared, and the monitor stayed `undelivered`. The scratch tenant and monitor were then deleted from `status_dev`.

## Open questions / risks

- **Rollback window** (CR 2). A release from before #10, running on the new schema, starts outages and renotifies without touching the two columns. If co-status then rolls forward, a chain left from before the rollback can redeliver an older dispatch, possibly from an earlier outage. That dispatch's status then lands in `last_alert_status`. `succeeded`, returned unchanged, would take the current outage's undelivered alert out of `undelivered` and turn `notifier-reachable` green. That's a silent miss, not just a duplicate. Mitigation: a step in DEPLOYMENT.md § Rollback. Before rolling forward past `b3344354c124`, run `UPDATE monitors SET last_alert_redeliver_at = NULL`. No code guard: the only cheap one compares notifier's clock with co-status's, and clock skew would turn redelivery off silently.
- **A pass that dies after a redelivery and before its commit** calls again next pass and uses one more attempt. The cap still bounds it.
- **At-least-once** (§ 5 of the comment): a channel whose `False` result wasn't real can get the alert twice.
