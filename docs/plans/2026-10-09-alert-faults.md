---
title: One report per alert fault, a reminder per renotify, a cleared notice (#28)
date: 2026-10-09
status: draft
---

# One report per alert fault, a reminder per renotify, a cleared notice

Issue: [#28](https://github.com/CannObserv/status/issues/28). Spec: [2026-10-09-alert-faults-design](../specs/2026-10-09-alert-faults-design.md) (F1–F11). Asked for on [#24](https://github.com/CannObserv/status/issues/24) and [#27](https://github.com/CannObserv/status/issues/27).

## Problem

Every `alert` check-in dispatches its own report, so a probe that checks in every tick floods its channels for as long as a fault lasts (#27: 12 an hour). A fault that ends with an `ok` sends nothing at all, so its end looks like a lost report. The spec decides both. This plan builds it.

## Approach

The spec's design, in the order the code depends on it:

1. **Schema.** `monitors.fault_since` and `fault_key`, and the `cleared` event kind.
2. **The pure decision.** `report_due`, the cleared wording and `cleared_key`.
3. **The check-in route**, which uses them.
4. **The surfaces** that read the result: `MonitorOut` and the sweep's `undelivered_notices`.

Each layer is tested before the next is built. The work goes on branch `28-alert-faults` and merges on explicit OK. Consumers are told *before* the deploy (F11), because the deploy is what changes what they receive.

## Tradeoffs / alternatives

- **One commit for the route and the schema.** Rejected. The migration and the pure functions are reviewable on their own, and the route diff is the risky part, so it should read alone.
- **Tell consumers after the deploy.** Rejected (F11). Processor's and observo's docs, and processor#39's reasoning, go wrong the moment it lands.
- **Ship behind a flag.** Rejected. No consumer reads `dispatches` (spec § Audit), no fault is open in production today, and a rollback is `deploy.sh` switching back. The columns are expand-only, and the old release ignores them.

## Steps

1. **Schema** (tests first, `tests/core/test_monitor_models.py`):
   - `Monitor.fault_since` (`timestamptz NULL`) and `fault_key` (`JSONB NULL`);
   - `EventKind.CLEARED`, with `ck_monitor_events_kind` widened to allow it;
   - one Alembic revision: add both columns, then drop and recreate the check with `cleared`. The downgrade restores the old check and refuses while any `cleared` row exists.

   Done when `alembic upgrade head`, `downgrade -1` and `alembic check` are clean on `status_test`, and the model tests pass.
2. **Pure core** (tests first, `tests/core/test_monitors.py`, `tests/core/test_alerting.py`):
   - `report_due(monitor, now, fault, last_report)` in `src/core/monitors.py`, covering every branch the spec's Testing section lists. That includes 23h50m due and 11h59m not due on a 24 h / 24 h monitor, `interval > 2 × renotify`, renotify null, no channels, an absent key against a present one, and `partial` as heard.
   - `CLEARED_TITLE`, `CLEARED_BODY` and `cleared_notification` beside the recovery notice, with a name containing `{{` passed through as data.
   - `cleared_key(monitor)` in `src/core/alerting.py`: `{id}:cleared:{fault_since}`.
3. **The check-in** (tests first, `tests/api/test_monitors_route.py`, the real SDK through respx). Rework `checkin()` to the spec's eight steps:
   - load the fault's latest report through one indexed query;
   - decide whether a report or a cleared notice is owed;
   - run the endpoint and preview checks only when something is to be sent;
   - fault bookkeeping, then the cleared notice and its event.

   Tests:
   - crossing;
   - a suppressed repeat: respx records **zero** notifier calls, the event's status is null, and `dispatches` is `[]`;
   - key change;
   - resend after `not_accepted` and after `failed`;
   - reminder at the tolerance;
   - `ok` → cleared, with its idempotency key, and the columns reset;
   - the three outage cases (F7);
   - a disabled monitor; a monitor with no channels;
   - a preview failure at the crossing, resent the next tick;
   - a 422 leaves the fault columns untouched.

   The `CheckinResponse.dispatches` comment is updated. `tests/api/test_contract.py` passes unchanged.
4. **`MonitorOut`** (tests first, same file):
   - `fault_since`, `fault_key`, `last_cleared_at`, `last_cleared_status`;
   - `_with_notices` gains a third lateral for `cleared`.

   Done when get, list, create and patch serve them, and they stay null with no events.
5. **Sweep** (tests first, `tests/core/test_sweep.py`): `CHECKIN_NOTICES[EventKind.CLEARED] = "cleared"`. An unheard cleared notice shows in `undelivered_notices` and in the `notifier-reachable` body (`1 cleared failed`), and clears on a later delivered one.
6. **Docs:**
   - `docs/reference/monitors.md`:
     - § The model: the fault columns;
     - § The API: `metadata.fault`, and what an empty `dispatches` means;
     - § What gets sent: the report row rewritten, plus a *has cleared* row;
     - § The sweep: `cleared`.
   - MVP spec: "amended by #28" pointers in § The check-in and on the key table's *report* row.
   - AGENTS.md: the `monitors.py` and `alerting.py` table rows.
7. **Branch CI and review.** Push `28-alert-faults`. CI's `lint`, `test` and `migrations` must pass. Then a code review (`reviewing-code-python-fastapi`), with its findings addressed.
8. **Tell consumers** (F11), once the branch is reviewed and before the merge:
   - **Comments on #24 (Processor) and #27 (Observo)**, as the spec's § Audit rows put it:
     - what they will receive;
     - the `metadata.fault` suggestion (`kind`; `outcome`);
     - the docs that become wrong;
     - `--test-alert` and rehearsals need an `ok` first, or a fault key of their own.
   - **Suggested issues in CannObserv/broker, index, watcher and usa-wa** (usa-wa's referencing usa-wa#458), through their own tokens.
9. **Ship:** merge on explicit OK, then `scripts/deploy.sh` (dev, then live through the CI gate) on explicit authorization. Verify:
   - `/ready` says `schema_state current`, and `/openapi.json` serves the new fields;
   - the next production pass logs `undelivered_notices: {}` with no traceback;
   - every monitor's `fault_since` is null, as no fault is open (read-only, authorized).

   Then post the deploy time on #24, #27 and #28, and close #28.

## Open questions / risks

- **Concurrent `alert` check-ins on one monitor** could both open the fault and both report. Accepted: each consumer checks in once per tick. A row lock (`SELECT … FOR UPDATE`) in `_load_owned` for check-ins would close it, at the cost of serialising them. Decide in step 3 if a test shows it is cheap.
- **A template that only a repeat's `variables` would break** answers 202 until the next report that is due (spec F5). Accepted; documented in monitors.md.
- **A fault open at deploy re-reports once** (F10). None is open as of the 2026-10-09 audit. If one opens before the deploy, say so on its consumer's issue.
