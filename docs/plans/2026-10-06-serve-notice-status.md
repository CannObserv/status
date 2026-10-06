---
title: Serve check-in notice delivery status to the monitor's owner (#20)
date: 2026-10-06
status: approved
---

# Serve check-in notice delivery status to the monitor's owner

Issue: [#20](https://github.com/CannObserv/status/issues/20), split from [#8](https://github.com/CannObserv/status/issues/8). Builds on #8's `monitor_events.dispatch_status` and #19's `not_accepted` ([plan](2026-10-05-checkin-not-accepted.md)).

## Problem

The check-in keeps each recovery's and report's delivery status on its `monitor_events` row (#8, #19). Only the operator sees it, through `notifier-reachable` and the sweep's journal line. #6 serves `last_alert_status` for missing alerts, but nothing serves a notice's status. A report is the consumer saying "something is wrong", and the consumer cannot find out through co-status that nobody heard it.

## Approach

- **Fields on the monitor, not an events endpoint** (the issue's first question). `MonitorOut` gains four optional fields: `last_report_at`, `last_report_status`, `last_recovery_at`, `last_recovery_status`. They sit beside `last_alert_at` / `last_alert_status` and read the same way. Adding response fields doesn't break the API, and the check-in contract (D6) is untouched.
- **Which event:** the latest `alert` (report) or `recovered` (recovery) event *with a status*: `succeeded`, `partial`, `failed` or `not_accepted`. This is the sweep's rule. A null status means nothing was owed (no channels) or the row predates #8/#19, so it doesn't replace a known status. `last_report_at` is when that notice was sent, as `last_alert_at` is for the alert. With no such event, both fields are null.
- **Any age, no 24-hour window** (the second question). The window exists so a one-off failure can't hold the operator's check down forever. The owner reads a fact with its own timestamp and can judge its age. Hiding a failed report after 24 h would tell the owner that nothing went wrong.
- **One query per request, constant per monitor.** `GET /monitors`, `GET /monitors/{id}` and `PATCH` select the monitor with two `LATERAL` subqueries (one per kind), each `ORDER BY at DESC LIMIT 1`. A new partial index, `(monitor_id, kind, at) WHERE dispatch_status IS NOT NULL`, makes each lookup a single index probe. Without it, the recovery lookup for a monitor that reports every tick and has never recovered walks every `alert` row it ever wrote. `POST` returns nulls, because a new monitor has no events.
- **Expand-only migration** (the index alone). The running release ignores the index and the new fields. Rollback-safe.

## Tradeoffs / alternatives

- **A read-only events endpoint** (`GET /monitors/{id}/events`). Deferred, not rejected. A status page will want history (D2, D9), and its shape (paging, which kinds, public or not) belongs to that spec. Fields answer "did my last report reach anyone?" in the response the owner already reads. An endpoint added later doesn't conflict.
- **Denormalised columns on `monitors`**, written by the check-in, as #6 did. Rejected: the event row already holds the truth (#8 put it there on purpose). Four columns would duplicate it and need a backfill. #6 used the monitor row only because renotifies write no event.
- **Only within #8's 24-hour window.** Rejected, as above.
- **`DISTINCT ON`, as the sweep does.** Rejected for the API: without the window it reads every matching row, because Postgres 16 has no skip scan. `LATERAL ... LIMIT 1` stops at one row per kind.
- **Latest event of the kind, null status included.** Rejected: a no-channel or pre-#8 row would hide a known failure, and the sweep already ignores nulls.

## Steps

1. **Index + migration** (test first): `MonitorEvent.__table_args__` gains the partial index; Alembic revision; `alembic check` clean.
2. **Schema + route** (tests first, `tests/api/test_monitors_route.py`): get, list and patch serve each kind's latest status and `at`. A later delivered report replaces an earlier failed one. A later null-status report doesn't. Recovery and report are independent. `not_accepted` is served as is. Null with no events. Another tenant's events never leak (it can't see the monitor). A real check-in, end to end, shows `failed` from a refusing notifier.
3. **Docs:** `MonitorOut` field comments, monitors.md (§ The sweep: drop "only the operator sees it"; say what the owner sees), the `dispatch_status` docstring, the RUNBOOK if it tells owners to ask the operator.
4. **Ship:** CI on the branch, merge on explicit OK, then `scripts/deploy.sh` (the migration runs on dev, then live).

## Open questions / risks

- Field names: `last_report_*` / `last_recovery_*` follow `last_alert_*` and #8's notice names. `last_report_*` could be confused with `last_status` / `last_variables`, which describe the latest *check-in*, not the latest notice. The field comments say which is which.
