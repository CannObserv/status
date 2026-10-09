# Alert faults: one report per fault, a reminder per renotify, a notice when it clears

**Status:** draft (2026-10-09), for review
**Issue:** [#28](https://github.com/CannObserv/status/issues/28). Asked for on [#24](https://github.com/CannObserv/status/issues/24) (Processor: "alert once at the crossing") and [#27](https://github.com/CannObserv/status/issues/27) (Observo: 12 reports an hour).
**Amends:** [MVP spec](2026-09-26-co-status-mvp-design.md) § The check-in (step 4) and § `alerting.py` (the key table's *report* row). D6 holds: the check-in request and response keep their shape.
**Plan:** to follow, in `docs/plans/`.

## Problem

Every `alert` check-in dispatches its own report (`src/api/routes/monitors.py`, the `if is_alert:` branch), with no look at what came before. A consumer that checks in every tick sends one report per tick for as long as a fault lasts. co-observo-live checks in every 5 minutes, so that is 12 reports an hour to Mailgun and Slack. `renotify_seconds` does not help: it spaces only the sweep's *missing* alerts.

The other half is the end of a fault. *Has recovered* is sent only when a monitor comes back from `missing` (silence). A check-in going from `alert` back to `ok` sends nothing, so today a fault ends in silence (#27's rehearsal, cases 1–3). Once repeats stop, that silence can't be told apart from a report that got lost. The two are decided together.

**Constraints:**
- `variables` is opaque (AGENTS.md § API Boundary Principles). Whatever tells one fault from the next keys on `status` and time, or on something the consumer opts into.
- The check-in contract is frozen in shape (D6).
- A check-in is always recorded and answered.
- A suppressed report still records its `alert` event.

## Decisions

| # | Decision | Why |
|---|---|---|
| **F1** | **A fault is a run of `alert` check-ins.** It opens on an `alert` check-in when none is open, and closes on the next `ok` check-in. Two nullable columns on `monitors` hold it: `fault_since` (when the fault's first `alert` check-in arrived; null means no fault is open) and `fault_key` (the `metadata.fault` value the fault opened with, or null). | The state that makes "a repeat" decidable. Events alone can't give it: D9 never records `ok` check-ins, so an event query can't see where a run ended. |
| **F2** | **A fault is identified by `status`, plus an optional `metadata.fault` the consumer controls.** An `alert` whose `metadata.fault` differs from `fault_key` (JSON equality; an absent key counts as null) opens a new fault in place of the old one, without a *cleared* for the old one. **Without the key, every consumer is de-duplicated on `status` and time.** | `variables` stays unread. Every consumer's `variables` carries text that changes on every run (observo's `error`, processor's `body`, broker's and index's `message`), so comparing them would never de-duplicate anything. `metadata` has the same shape (`dict[str, Any]`) and is already forwarded to notifier as the dispatch's metadata. Reading one optional key out of it adds meaning without changing the shape. A changed fault is new information, and without the key it would wait for the next reminder: up to 24 h for processor, and never for broker or index (§ Audit). |
| **F3** | **A report goes out when a fault opens, then as a reminder once `now ≥ last report + renotify_seconds − interval_seconds / 2`.** With `renotify_seconds` null there are no reminders: one report per fault. | The reminder lands on the check-in *nearest* the renotify point, not the first one after it. Check-ins arrive with jitter, so plain `≥ renotify` would drop a nightly backup's second failed night. Their timers' `RandomizedDelaySec=10min` puts consecutive runs as little as 23h50m apart under a 24 h renotify (§ Audit). The sweep needs no tolerance: it runs every 60 s. When `interval > 2 × renotify`, every `alert` check-in reports, which is what such a setting asks for. |
| **F4** | **An unheard report is resent.** If the fault's latest report (its newest `alert` event at or after `fault_since` with a non-null `dispatch_status`) is `not_accepted` or `failed`, the next `alert` check-in sends a fresh report with its own `variables`. `partial` counts as heard. | Late, never lost: the same rule as the owed missing alert. A `partial` reached somebody, since a monitor's channels are meant to be redundant. Reports keep no idempotency key, because a resend after `failed` needs a new dispatch, not notifier's old record. |
| **F5** | **A suppressed `alert` check-in is recorded and makes no notifier calls.** It writes `last_*` and its `alert` event with `dispatch_status` null ("owed nothing"), and answers 202 with `dispatches: []`. Notifier's endpoint and preview checks run only when a report is due. | The event keeps the history (D9), and null already means "owed nothing" to `last_report_*` and `undelivered_notices`. Skipping the calls keeps a suppressed check-in off notifier entirely. **The price:** a template that a repeat's `variables` would break gets a 202 instead of a 422, and the 422 shows up on the next report that is due. |
| **F6** | **An `ok` check-in while a fault is open sends a built-in *"[co-status] {name} has cleared"* notice.** It is a fixed template with `variables`: the name, how long the fault lasted (`fault_since` → now), when the fault began and the last `alert` check-in. Its idempotency key is `{monitor_id}:cleared:{fault_since}`. It writes a new `cleared` event with its `dispatch_status` (null with no channels), then nulls `fault_since` and `fault_key`. | Once repeats stop, the end of a fault needs a notice of its own, or it reads as a lost report (#28). The key makes the SDK's retries safe, as for recovery. It is built before `last_checkin_at` moves, and while a fault is open the previous check-in is always an `alert`. |
| **F7** | **Silence doesn't end a fault.** The sweep never touches `fault_since`. The check-in that ends an outage sends *recovered*, then applies F2–F6: `ok` gives recovered + cleared; `alert` in the same fault gives recovered, plus a report only if F3 or F4 says one is due; `alert` with a new key gives recovered + report. | Every fault the operator was told about gets an explicit end. *Recovered* says the consumer is reporting again, not that its fault is over. There are still at most two dispatches, as `CheckinResponse` already allows. |
| **F8** | **`CheckinResponse` keeps its shape.** `dispatches` lists what this check-in sent: a recovery, a report, a cleared notice, at most two. On an `alert` check-in an empty list means suppressed or nothing owed, not lost. **`MonitorOut` gains `fault_since`, `fault_key`, `last_cleared_at` and `last_cleared_status`**, the last two from the same lateral as `last_report_*` (#20). | D6 covers the check-in only; #20 already extended `MonitorOut`. The owner can see that a fault is open and what became of its end. No consumer reads `dispatches` today (§ Audit). |
| **F9** | **The sweep's `undelivered_notices` covers `cleared`** (`{monitor id: {"cleared": status}}`), with the same 24 h window and clearing rule as recovery and report. | A *cleared* that nobody heard leaves the last word on a fault as a report, so it counts towards `notifier-reachable` like the others. |
| **F10** | **Expand-only migration, no backfill.** Add `monitors.fault_since timestamptz NULL` and `monitors.fault_key jsonb NULL`, and widen `ck_monitor_events_kind` with `cleared`. A fault already open at deploy has `fault_since` null, so its next `alert` opens a fault and reports once. | The previous release ignores both columns and never writes `cleared` (deploy spec R7). One extra report per open fault at deploy is cheaper than inferring faults from events that can't see `ok` (F1). |
| **F11** | **Consumers are told before the deploy.** Processor and Observo get comments on #24 and #27. Broker and index get suggested issues in their own repos. The backups are covered by the #27 comment (observo-backup), a watcher issue, and a usa-wa note handed to the operator (co-status has no usa-wa token). | It changes what every consumer receives. Docs in processor and observo become wrong, and processor#39's reason for never sending `alert` goes away. |

**Unchanged:**
- A disabled monitor still records check-ins and still reports, de-duplicated the same way (MVP spec § The check-in). broker `deploy/README.md:682-688` and watcher `RECOVERY.md:181-183` rely on the first `alert` dispatching.
- A report notifier can't render is still a 422 that leaves the monitor untouched, when the report is due.
- Missing alerts, their renotify, and redelivery (#10) are untouched.

### Rejected

- **`status` and time only, with no opt-in key.** A change of fault would wait for the next reminder, and with `renotify_seconds` null it would wait forever (broker, index).
- **De-duplicate only consumers that opt in.** No surprise to anyone, but observo would stay at 12 reports an hour until it shipped a change. The issue asks for the default to change.
- **Comparing all of `variables`, or a monitor setting that names a key inside them.** The first never de-duplicates (F2). The second reads `variables`.
- **Spacing by `renotify_seconds` with no tolerance, or numbering renotify periods from the crossing** (as `missing_key` does). Both drop a backup's second failed night at 23h50m.
- **Silence closes the fault.** Fewer notices, but a fault that was reported never gets a *cleared*.
- **An idempotency key on reports.** A resend after `failed` would get notifier's old failed record back (F4).

## Design

### The decision, `src/core/monitors.py`

Pure, like `should_alert`:

```python
def report_due(monitor, now, fault, last_report) -> bool:
    """Whether this alert check-in sends a report (F2–F4).

    *fault* is the check-in's ``metadata.get("fault")``; *last_report* the
    fault's latest report that owed a notice, as ``(at, dispatch_status)``,
    or None.
    """
```

- A new fault (`fault_since` is null, or `fault != fault_key`) → due.
- A monitor with no channels → never due: the fault is still tracked, and nothing is owed or sent.
- No *last_report* → due (channels added mid-fault).
- `last_report` status `not_accepted` or `failed` → due.
- `renotify_seconds` set and `now - at >= renotify - interval / 2` → due.
- Otherwise not due.

`cleared_notification(monitor, now) -> Notice` sits beside `recovery_notification`, with fixed `CLEARED_TITLE` and `CLEARED_BODY`. `EventKind.CLEARED` is added.

### The check-in, `src/api/routes/monitors.py`

The order after #28 (the MVP spec's steps, amended):

1. **Load the fault's latest report** (`alert` events with `at >= fault_since` and `dispatch_status` not null, newest first, limit 1, on `ix_monitor_events_notice`) when the check-in is `alert` and a fault is open.
2. **Decide**: `report_due` for `alert`; *cleared* is owed for `ok` with a fault open.
3. **Endpoint and template checks**, only if something is to be sent: today's code, with `is_alert` replaced by "report due".
4. **Recovery**, as today.
5. **Record**: `last_*`, `state = ok`, `first_checkin`.
6. **Fault bookkeeping**:
   - `alert` opening a fault sets `fault_since = now` and `fault_key`;
   - `ok` sends *cleared* (built before `last_checkin_at` moves, in step 2), writes `cleared`, and nulls both columns.
7. **Report** if due, as today. Every `alert` check-in writes its `alert` event; a suppressed one with `dispatch_status` null.
8. **Respond**, with the same shape.

`alerting.py` gains `cleared_key(monitor)`: `{id}:cleared:{fault_since}`.

### The sweep, `src/core/sweep.py`

- `CHECKIN_NOTICES[EventKind.CLEARED] = "cleared"`.
- Nothing else: the sweep neither opens nor closes faults (F7).

## Audit

### Production, 2026-10-09

A read-only query, authorized by the operator.

| Monitor | Tenant | interval / grace / renotify (s) | Channels | Alert events (sent) | Today | Under #28 |
|---|---|---|---|---|---|---|
| co-observo-live | co-observo-live | 300 / 600 / 3600 | 2 | 3 (3), the #27 rehearsal | A report every 5 min while a fault lasts | A report when a fault opens, a reminder at about 1 h (the 12th tick), *cleared* on `ok`. `degraded` → `disk_low` shows within 1 h, or at once with `metadata.fault = outcome` |
| co-processor-drift | co-processor | 3600 / 5400 / 86400 | 2 | 1 (1), the #24 test alert | Hourly while lagging | A report at the crossing, then daily, then *cleared* once deployed. `lag` → `off_main`, and `--test-alert` during a lag, wait up to 24 h unless it sends `metadata.fault = kind` |
| co-watcher-backup, co-usa-wa-backup, co-observo-backup | co-watcher, co-usa-wa, co-observo | 86400 / 7200 / 86400 | 2 each | 0 | One report per failed night | The same: every failed night still reports (23h50m ≥ the 12 h threshold). New: *cleared* on the first good night |
| co-broker, co-index | co-broker, co-index | 600 / 1200 / null | 2, 1 | 0 since the cutover | A report every 10 min while there are findings | One report per fault, then *cleared*. **A new finding mid-fault stays silent** until the fault clears, unless they send `metadata.fault` (e.g. their sorted `check:subject` set) |

Every monitor has a template. All seven were `ok`, with `last_status = ok`, at the time of the query: no fault is open, so F10's re-report at deploy has nothing to act on today.

### Consumer clients, 2026-10-09

| Client | Reads from the 202 | Sends `alert` | Schedule |
|---|---|---|---|
| watcher `src/ops/checkin.py`, `src/ops/backup.py` | Status code only | Every failed run | `03:17`, `RandomizedDelaySec=10min`, `Persistent=true` |
| usa-wa `backup/checkin.py`, `backup/run.py` | Status code only | Every failed run | `10:17`, the same |
| observo `ops/checkin.py` (backup) | Status code only | Every failed run | `11:43`, the same |
| observo `ops/liveness.py` | Status code only; always exits 0 | Every 5 min while a fault lasts | `*:0/5` |
| processor `checkin.py`, `drift.py` | Status code (not 202 → exit 1); `detail` on an error | Hourly while lagging | `OnUnitActiveSec=1h` |
| index `deploy/index-checkin.sh` | Logs `next_deadline_at` | Every 10 min while there are findings | `OnUnitActiveSec=10min` |
| broker `src/broker/bus_health.py` | Status code only | Every 10 min while there are findings | `OnUnitActiveSec=10min` |

- **No client reads `dispatches`, `state` or `previous_state`**, so an empty `dispatches` breaks no code.
- usa-wa#458 (open) plans to log the dispatch ids. Its scope already accepts an empty list, but its docs should say that empty means suppressed.
- **Docs that become wrong:**
  - processor `docs/DEPLOYMENT.md:251,280` ("alerts hourly until deployed");
  - processor#39's reason for never sending `alert`;
  - observo `infra/README.md:343-350` (12 reports an hour; nothing on `alert` → `ok`).
- **Tests that need an `ok` before them:**
  - processor `drift --test-alert` proves the channels only when no fault is open, or with a `metadata.fault` of its own;
  - observo's rehearsal (`docs/RECOVERY.md:302-305`) repeats a fault, so it needs an `ok` between cases. It had one in every case of #27.
- No client sends a fault identifier today. The stable parts of `variables` that would make one:
  - observo: `outcome`;
  - processor: `kind`, with `live`;
  - broker and index: the set of `findings[].check` and `subject`;
  - the backups: none needed, since F3 reports every failed night.

## Testing

TDD, red first.

- **`report_due` and `cleared_notification`** (pure, `tests/core/test_monitors.py`):
  - every branch;
  - 23h50m on a 24 h / 24 h monitor is due, and 11h59m is not;
  - `interval > 2 × renotify` is always due;
  - with renotify null, no reminder is ever due;
  - a changed key, and an absent key against a present one;
  - `partial` counts as heard;
  - the cleared wording takes a name containing `{{` as data.
- **The check-in** (`tests/api/`, the real SDK through respx):
  - a crossing reports;
  - a suppressed repeat makes no notifier call, writes an event with null status, and answers `dispatches: []`;
  - a key change reports;
  - a resend after `not_accepted`, and after `failed`;
  - a reminder at the tolerance;
  - `ok` sends cleared, with its key, its event, and the columns reset;
  - the three outage cases (F7);
  - a disabled monitor;
  - a monitor with no channels;
  - a preview failure at the crossing is `not_accepted`, and the next tick resends;
  - a 422 on a due report leaves the fault columns untouched;
  - `MonitorOut`'s new fields.
- **The contract test** stays green unchanged.
- **The sweep:** `cleared` in `undelivered_notices`, and it clears when a later one is delivered.
- **The migration:** upgrade and downgrade; rows the previous release writes stay valid.
