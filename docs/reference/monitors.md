# Monitors — the dead-man's timer

How co-status alerts on the **absence** of a report, not only on its contents. Taken over from notifier (CannObserv/notifier#56, #83) with the contract unchanged; what changed is where alerts go. Design: [the MVP spec](../specs/2026-09-26-co-status-mvp-design.md).

## Why absence

A findings-only push is silent in exactly the cases that matter most. A dead probe, a stopped timer, a wedged `uv run` and a dead node all produce zero findings and zero traffic — indistinguishable, from outside, from a healthy consumer. CannObserv/observo#473 is what that costs: a crash-looped Redis left a cluster starved for two weeks.

So a consumer checks in **every tick regardless of findings**, and co-status alerts when a check-in fails to arrive.

## The model

| Field | Meaning |
|---|---|
| `interval_seconds` | The cadence the consumer promises |
| `grace_seconds` | Slack on top of it before silence is an outage |
| `renotify_seconds` | `null` alerts once per outage; a value repeats every N seconds while missing |
| `channel_ids` | Channels owned by co-status's own tenant **in notifier**, checked against notifier on every write |
| `title_template` + `body_template` | The consumer's Jinja for an `alert` check-in; rendered by notifier, never here |
| `enabled` | `false` pauses the timer for planned downtime; check-ins still land |
| `state` | `pending` → `ok` → `missing`, and back |

The deadline is `(last_checkin_at or created_at) + interval + grace`. Anchoring on `created_at` before the first check-in is deliberate: a monitor configured here but never wired up on the consumer's side is the likeliest misconfiguration of all.

Every state change is recorded in `monitor_events` — `imported`, `first_checkin`, `missing`, `recovered`, `alert`, `paused`, `resumed` — never with the consumer's `variables` (spec D9). An event that sent something keeps notifier's `dispatch_id` and, beside it, the delivery `status` as `dispatch_status` ([#8](https://github.com/CannObserv/status/issues/8)). A recovery or report notifier never took has no dispatch id, and `dispatch_status` is co-status's own `not_accepted` ([#19](https://github.com/CannObserv/status/issues/19)).

## The API

Byte-for-byte notifier's check-in (spec D6), so a consumer moves by base URL and key alone:

```bash
curl -s "http://status:9000/health"
# {"status":"ok","build":"…","database":"status","environment":"production"}

# The key on stdin, never in curl's argv, where `ps` shows it.
printf 'X-API-Key: %s\n' "$KEY" | curl -sX POST "http://status:9000/api/v1/monitors/$ID/checkin" \
  -H @- -H 'Content-Type: application/json' -d '{
    "status": "ok",
    "variables": {"source": "co-broker", "finding_count": 0, "findings": []}
  }'
```

- **`variables` is opaque.** Stored verbatim and forwarded to notifier; nothing here reads inside it.
- **`status` is the consumer's judgement.** `ok` records the check-in and sends nothing; `alert` also sends the monitor's templates to notifier with the `variables`.
- **An `ok` check-in is never checked against the template.** Rejecting a heartbeat over its payload would silence the timer to protect a notification that was never going to be sent.
- **An `alert` notifier cannot render is a 422** naming the section, and the monitor is left untouched — notifier's `preview` is the check.
- **The monitor says what became of its notices.** A 202 means the check-in was recorded, not that anyone heard the report. `GET /monitors` and `/monitors/{id}` serve `last_alert_status` for the missing alert ([#6](https://github.com/CannObserv/status/issues/6)), and `last_report_status` and `last_recovery_status`, each with its `_at`, for the latest report and recovery that owed a notice ([#20](https://github.com/CannObserv/status/issues/20)): `succeeded`, `partial`, `failed` or `not_accepted` ([§ The sweep](#the-sweep)). Each is served at any age. `last_report_*` is the latest *report*; the latest check-in is `last_checkin_at` and `last_status`.

`tests/api/test_contract.py` holds the request, the 202 and the path parameter to a snapshot of notifier's OpenAPI at `notifier@2c02dbf`.

### What gets sent

| Event | Notification |
|---|---|
| `status: "alert"` check-in | The monitor's own templates, rendered by notifier against `variables` |
| Deadline passes | Built in: *"[co-status] {name} has stopped reporting"* |
| First check-in after an outage | Built in: *"[co-status] {name} has recovered"* |

The built-in wording is a fixed template with the facts passed as `variables`, so a monitor name containing `{{` is data, never template syntax.

## Alerts go through notifier

co-status delivers nothing itself (spec D5). Every alert is a `POST /api/v1/dispatch` to notifier from co-status's own tenant, through `src/core/alerting.py`:

- **Which notifier is derived, not configured:** production sends to `notifier:9000`, development to `notifier:9001`, and `/health`'s `environment` is checked before sending. A co-status pointed at the wrong notifier refuses rather than inverting.
- **A 202 is not a delivery** (notifier#70). `status` is logged when it is `failed` or `partial`. The sweep keeps it as `last_alert_status`, and the check-in on its event as `dispatch_status`, or `not_accepted` when notifier took nothing (see below).
- **Missing and recovery alerts carry deterministic idempotency keys**, so a pass that dies between sending and committing does not send twice.
- **A check-in is always recorded and answered**, whatever notifier does, inside an 8-second budget below the consumers' 10-second timeout.

## The sweep

`status-sweep.timer` fires every 60 seconds: `scripts/sweep.sh` → `scripts/sweep_monitors.py` → `sweep_monitors()`.

**A monitor is marked `missing` whether or not the alert went out** — the state describes the consumer. But **`last_alert_at` moves only when notifier accepted the alert.** A monitor that went missing while notifier was unreachable is still owed its alert, and every pass retries it under the same key until notifier takes it: late, never lost. The journal line reports `owed` every pass.

**Accepted is not delivered** ([#6](https://github.com/CannObserv/status/issues/6)). `last_alert_status` holds notifier's delivery `status` for the alert at `last_alert_at` (served with the monitor, so its owner can see it), and is cleared when a new outage begins. Every pass lists each `missing` monitor whose last alert came back `failed` or `partial` as `undelivered` — every pass, not just the one that sent it, so the heartbeat below does not go *down* and then *up* 60 seconds later while nobody has been told. It clears when the monitor recovers or a later renotify is delivered. A disabled monitor is not reported, but resuming one still missing brings it back. A `partial` counts: a monitor's channels are meant to be redundant. Nothing is resent ([#7](https://github.com/CannObserv/status/issues/7)).

**The check-in's notices too** ([#8](https://github.com/CannObserv/status/issues/8)). A recovery or report has no pass of its own: the check-in sends it once and answers. Its `recovered` or `alert` event keeps the delivery `status` as `dispatch_status`. When notifier never took it, it is `not_accepted` ([#19](https://github.com/CannObserv/status/issues/19)): unreachable, out of the budget, refused (revoked key, 422, every channel deleted), the endpoint check or the report's preview failed, or the API has no notifier key. A monitor with no channels owed nothing and keeps null, as the sweep's `undeliverable` does. Every pass lists, per monitor, the latest recovery and the latest report of the last 24 hours, if it came back `failed`, `partial` or `not_accepted`, as `undelivered_notices` (`{monitor id: {"recovery" | "report": status}}`). It clears when a later one of the same kind is delivered: a consumer reporting `alert` every tick clears on its next delivered report. A delivered recovery does not clear a report. A later notice notifier never took is reported in an earlier failure's place, as `not_accepted`. Otherwise it clears 24 hours after it was sent: a one-off report has no later one, and nothing resends it, accepted or not. A resend would need the report's `variables`, which events never keep (D9). Disabled monitors are not reported, as above. The owner sees the latest of each kind, at any age, on the monitor itself ([§ The API](#the-api), [#20](https://github.com/CannObserv/status/issues/20)): the 24 hours bound the operator's check, not what the owner is told.

**A timer, not a task in the API process**, and **`TimeoutStartSec` is not decoration**: both for notifier's reasons (its monitors.md § The sweep).

## Who watches co-status

**healthchecks.io, outside the cohort** ([#1](https://github.com/CannObserv/status/issues/1), [#13](https://github.com/CannObserv/status/issues/13), `src/core/heartbeat.py`). The spec (D10) shipped two unannounced failures: co-status stopping, and notifier being down. A third came with the API itself: a failed API was invisible until consumers went `missing`. Every production pass now pings three checks at `https://hc-ping.com/<ping-key>/<slug>`:

| Check | Success ping | `/fail` | Silence past the 5-minute grace |
|---|---|---|---|
| `co-status-sweep` | The pass completed and committed. Body: the counts. | The pass raised, Postgres down included. Body: the exception's type, never its message. | Timer, VM or OOM killer |
| `notifier-reachable` | notifier's `/health` answered in production, nothing was left `owed`, and nothing is `undelivered` or in `undelivered_notices` | Unreachable at the start of the pass, *n* alerts owed, *n* undelivered, *n* check-in notices undelivered: the body names each that applies, and counts `failed` and `partial` (per notice for the check-in's, `not_accepted` too: `1 report failed`, `1 recovery not_accepted`) | The sweep itself is not running |
| `co-status-api` | `GET http://status:9000/ready`, the name consumers use, answered 200 from `production`. Body: the answer. | Still not, after asking every 2 s for 20 s. Body: the answer (`503 {…}`), or the error and its message (`ConnectError: All connection attempts failed`) | The sweep itself is not running |

healthchecks alerts over its own email and Slack, **never through notifier**.

- **A ping never fails a pass.** A failed ping is a `healthchecks ping … failed` warning in the journal; if it persists, the checks' silence is the alert.
- **The key is a credential on `status-sweep.service` and `status-drift.service` alone** (D13): anyone holding it can report a dead sweep as alive. Without `/etc/status/hc-ping.key`, the sweep runs, warns every pass and pings nothing. The unit's `SetCredential=` fallback exists because a missing `LoadCredential=` file would otherwise fail the unit (243), and with it every timer.
- **Why the API is watched from the sweep.** The API records check-ins. While it is down, consumers' check-ins fail, and after each one's grace the sweep reports it `missing` through notifier: a false alarm about the consumer, when the fault is co-status. The API is asked last, after the pass and its own pings, whatever the pass did, so a slow API never delays an alert. The 20-second retry rides out a restart: the forced pass of a deploy runs while uvicorn is still starting.
- **What `co-status-api` cannot see.** The probe runs on the API's own host, so it never crosses the tailnet ACL. A consumer that the ACL blocks still goes `missing` while the check stays green.
- **No `OnFailure=`.** Under `Restart=` it fires on every crash, even one the restart fixes, because systemd's default `RestartMode=normal` passes through `failed`. With `RestartMode=direct` it fires only once the unit gives up. It never fires for an API that is up but not ready, and a check fed only `/fail` never comes back up by itself. A failed unit cannot answer `/ready`, so the probe reports it one pass later ([plan](../plans/2026-10-01-watch-the-api.md)).
- **Dev is deliberately unwatched.** The dev sweep pings nothing and asks nothing, and no dev unit has `OnFailure=`. `scripts/deploy.sh` verifies `:9001` on every deploy, and dev's callers see their own failures.
- **`notifier-reachable` is broader than its name.** It also goes `/fail` when notifier *refuses* an alert: a revoked key (401), a monitor whose channels were all deleted, a 422. It stays down until that is fixed, and healthchecks alerts once per change of state, so a real outage starting meanwhile raises no new alert. A refused alert is still an alert that reaches no one, which is why it counts. The same goes for an alert, recovery or report notifier accepted and could not deliver, and for a recovery or report it never took: either holds the check down for up to 24 hours, including after a short outage ends.

**A fourth check watches what is deployed, not what runs: `co-status-drift`** ([#12](https://github.com/CannObserv/status/issues/12), `src/core/drift.py`). Since #9, pushed is not deployed. Every hour `status-drift.timer` asks GitHub how far the live release's `REVISION` is behind `origin/main`:

- **Up** while live is `main`, behind only in paths that never run (`docs/`, `tests/`, `.github/`, the skills, `*.md`), or behind in code for under 8 h. The clock starts at the push that brought the code, from the `created_at` of its CI run, never from a commit date.
- **`/fail`** past 8 h. The body names both builds, the push and `main`'s CI result: a red one is refused by the deploy gate ([#11](https://github.com/CannObserv/status/issues/11)), so the fix is CI, not a deploy. Also `/fail` at once when live is not on `main`, GitHub does not know it (404: `main` rewritten, or the repo no longer public), or it has no `REVISION`.
- **`/log`** when GitHub cannot answer (unauthenticated, 60 requests an hour per address; a run costs 1 in sync or behind in docs, 2 behind in code, at most 10 past the grace). The check's state does not change; an outage longer than its 2-hour grace goes silent, and that alerts.
- Production only, own unit, no database and no notifier key: GitHub never joins the sweep's failure modes. Dev is not checked; it is often ahead of `main` on purpose.

**What is still open:** notifier being down is now *announced*, not closed. Missing alerts still wait for notifier. Recovery and report notices sent during the outage are surfaced for 24 hours ([#19](https://github.com/CannObserv/status/issues/19)) but never resent, and neither is one notifier accepted and failed to deliver ([#8](https://github.com/CannObserv/status/issues/8)). A healthchecks.io outage produces false alarms, never silence.

## Check the endpoint before the timer

A consumer pointed at the wrong co-status is worse than a broken one: check-ins land in `status_dev`, and the production sweep reports a healthy consumer as dead. Assert on `environment`, never `build`:

```bash
test "$(curl -s "$STATUS_URL/health" | jq -r .environment)" = production
```
