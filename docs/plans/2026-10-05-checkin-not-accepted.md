---
title: Surface check-in notices notifier never accepted (#19)
date: 2026-10-05
status: in review
---

# Surface check-in notices notifier never accepted

Issue: [#19](https://github.com/CannObserv/status/issues/19), split from [#8](https://github.com/CannObserv/status/issues/8). Builds on #8's `monitor_events.dispatch_status` and `SweepReport.undelivered_notices` ([plan](2026-10-05-checkin-undelivered.md)).

## Problem

The check-in route sends a recovery or report once, inside the 8 s budget. When notifier never takes it, the event is written with `dispatch_id` and `dispatch_status` both null, the same as an event that had nothing to send. The only trace is an `ERROR` in the API journal. This happens when notifier is unreachable or the budget runs out, when it refuses the notice (revoked key, 422, every channel deleted), when the endpoint check or the report's preview fails, or when the API has no notifier key. A sustained outage fails `notifier-reachable` anyway. A single timed-out or refused report does not.

## Approach

- **A value of our own, not a column** (the issue's first question): `dispatch_status = "not_accepted"` with `dispatch_id` null. It becomes `alerting.NOT_ACCEPTED`, next to `DELIVERED`. notifier's vocabulary is `succeeded | partial | failed`, so there is no collision. There is no migration, because the column is already a free string.
- **When:** the check-in warranted the notice (`recovering`, or `status: alert`), the monitor has channels, and no dispatch came back for any reason, including no key and an endpoint mismatch. A monitor with **no channels** keeps null: there was nothing to send. That matches the sweep's `undeliverable`, which does not fail `notifier-reachable` either.
- **The sweep needs no new query.** `_undelivered_notices` already takes the latest non-null status of each kind within `NOTICE_WINDOW` and reports anything except `succeeded`. `not_accepted` rows now count. They are reported, they clear the same way (a later delivered notice of that kind, or 24 h), and `notifier-reachable`'s body reads `1 report not_accepted`.
- **A later never-accepted notice now replaces an earlier failure** of the same kind in the report, as `not_accepted` instead of `failed`. #8 skipped null rows so that a lost notice would not hide a failed one. Now the lost notice is reported itself, so the kind stays reported, under the newer reason.
- **Surface only, no resend** (the second question). The reasons are under Tradeoffs.
- **Rollback-safe:** the running release (`000f257`) also reads `not_accepted` as not `succeeded` and reports it. The only difference is that nothing would write new ones.

## Tradeoffs / alternatives

- **A separate column** (for example `dispatch_error`). Rejected. It needs a migration, the sweep's query would have to read two columns, and #20 would serve two fields where one answers "did it reach anyone?"
- **The sweep resends it** (an owed report). Rejected for now:
  - A report's content is the consumer's `variables`, and events never keep them (spec D9). Only `monitors.last_variables` does, and only for the latest check-in. A resend would need D9 reopened.
  - Reports are sent without an idempotency key today.
  - A recovery quotes a silence measured from a `last_checkin_at` that has since moved. Resent after the monitor goes missing again, it would announce a recovery that is no longer true.
  - A consumer that is still in trouble reports again on its next tick, and that report is a fresh send.
  - If resending is wanted, it belongs beside #10 (redelivery), as its own issue.
- **Mark monitors with no channels too.** Rejected: nothing was owed, and the API already rejects channels notifier doesn't know.
- **Cost:** a recovery or report lost during a short notifier outage holds `notifier-reachable` down for up to 24 h after notifier is back. That is the trade #8 made: the check should stay down until somebody has seen it. The body tells this apart from an outage, which reads `notifier unreachable at sweep start`.

## Steps

1. **Check-in records it** (tests first, `tests/api/`). Both recovery and report get `not_accepted` when notifier is unreachable at the endpoint check, on an endpoint mismatch, when the preview is unavailable (report only), on a refusal (`AlertRejected`), when every channel is deleted, when the budget runs out, and when there is no alerter. A monitor with no channels gets null. Delivered and accepted-but-failed are unchanged. The check-in is still a 202 with the same body.
2. **Sweep** (tests first): `not_accepted` is reported for each kind. A later delivered notice clears it. Update #8's "a later notice not accepted does not hide an earlier failure" test to the new rule: the kind stays reported, as `not_accepted`.
3. **Heartbeat** (test first): the fail body counts `not_accepted` beside `failed` and `partial`.
4. **Docs:** `MonitorEvent.dispatch_status` docstring, the `sweep.py`, `alerting.py` and `heartbeat.py` docstrings, monitors.md (§ What gets sent, § Who watches the sweep, § What is still open), and the RUNBOOK triage: for `not_accepted`, the API journal's `not accepted` / `not sending` / `cannot check` lines.
5. **Ship:** CI on the branch, merge on explicit OK, then `scripts/deploy.sh`. There is no migration.

## Open questions

- The name `not_accepted`. It matches `AlertNotAccepted` and the issue's own example. #20 will serve it to owners as is.
