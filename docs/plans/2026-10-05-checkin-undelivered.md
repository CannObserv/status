---
title: Surface undelivered check-in notices (#8)
date: 2026-10-05
status: in progress
---

# Surface undelivered check-in notices

Issue: [#8](https://github.com/CannObserv/status/issues/8), split from [#6](https://github.com/CannObserv/status/issues/6). Builds on #6's sticky `undelivered` and on #1's `notifier-reachable` check ([monitors.md § Who watches the sweep](../reference/monitors.md)).

## Problem

The check-in route sends two notices through `Alerter.send`: **recovery** (a `missing` monitor checked in) and **report** (the consumer sent `status: alert`). When notifier accepts one and its delivery `status` is `failed` or `partial`, the only trace is `Alerter.send`'s warning in the API journal. The check-in is recorded and answered, as it must be, and nobody finds out. For a report that is the consumer's own "something is wrong", and nothing else would say it.

## Approach

- **Record the status beside the dispatch id.** New nullable column `monitor_events.dispatch_status`, notifier's delivery `status` for the dispatch in `dispatch_id`. Written wherever a dispatch id is: `recovered` and `alert` events from the check-in, and `missing` events from the sweep (history only; #6's `monitors.last_alert_status` stays what the sweep reads for missing alerts, because renotifies write no event). Expand-only migration; existing rows read as not known undelivered.
- **No new event kind** (the issue's first question). Reports already write an `alert` event with the dispatch id; the issue predates that.
- **What a pass reports.** For each enabled monitor, the latest *accepted* notice of each kind (`recovered`, `alert`) sent within `NOTICE_WINDOW` (24 h). If its status is not `succeeded`, it goes in `SweepReport.undelivered_notices`: monitor id → `{"recovery" | "report": status}`. One query, `DISTINCT ON (monitor_id, kind)`, on the existing `(monitor_id, at)` index.
- **How long it stays** (the issue's second question): until a later notice of the same kind for that monitor is delivered, or 24 h after it was sent, whichever comes first. A consumer alerting every tick clears on its next delivered report. A one-off has no next one, and nothing resends (#7), so without the window it would hold `notifier-reachable` down forever. The window is a working day: long enough to be seen; and every pass inside it fails, so the check does not go *down* and then *up* 60 s later (#6's reason for sticky).
- **`notifier-reachable` fails on it.** The fail body gains `n check-in notice(s) undelivered (1 recovery failed, 1 report partial)`. `co-status-sweep`'s counts and the journal line gain `undelivered_notices`.
- **Disabled monitors are not reported**, as with #6.

## Tradeoffs / alternatives

- **Columns on `Monitor`** (status and time, per kind: four columns), as #6 did. Rejected: #6 went to `Monitor` because renotifies write no event. The check-in writes an event for every notice it sends, so the status belongs on that row, keeps per-notice history for incident lists later, and "latest of each kind" is the index's order.
- **Until a later delivered notice only, no window.** Rejected: a report that never recurs pins the check down indefinitely, blind to every new cause (healthchecks.io notifies on transitions only).
- **Per-pass only** (surfaced once). Rejected for #6's reason: *down* then *up* a minute later reads as resolved.
- **Any later delivered dispatch for the monitor clears it.** Rejected: a delivered recovery does not carry the report that failed.
- **Cost of the window:** while a notice is surfaced, `notifier-reachable` is already down, so a new cause inside those 24 h does not page again. Same trade #6 made for an outage's duration; the fail body still names every cause.

## Steps

1. **Model + migration** (test first): `MonitorEvent.dispatch_status`; Alembic revision; `alembic check` clean.
2. **Check-in records it** (tests first): recovery and report events carry the dispatch's status; a notice notifier did not accept leaves both null.
3. **Sweep records it on `missing` events** (test first).
4. **Sweep reports** (tests first): failed/partial report and recovery reported; delivered not; a later delivered notice of the same kind clears; a different kind does not; older than the window not; a later notice not accepted does not hide an earlier failure; disabled not reported; one monitor can carry both kinds.
5. **Heartbeat** (tests first): fail body, counts. Journal line in `scripts/sweep_monitors.py`.
6. **Docs:** monitors.md (§ What gets sent, § Who watches the sweep), RUNBOOK (triage, jq line), AGENTS if needed, module docstrings.
7. **Ship:** CI on the branch, merge on explicit OK, `scripts/deploy.sh` (migration runs on dev, then live).

## Split out

- **Notices notifier never accepted** (unreachable, rejected, out of budget, preview unavailable): the check-in path has no owed alert, so these are lost with only an ERROR in the API journal. Separate issue.

## Open questions

None.
