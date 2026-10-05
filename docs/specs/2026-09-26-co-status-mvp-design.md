# co-status MVP: moving the dead-man's timers out of notifier

**Status:** design, awaiting review (2026-09-26)
**Issue:** [CannObserv/notifier#83](https://github.com/CannObserv/notifier/issues/83) ([review](https://github.com/CannObserv/notifier/issues/83#issuecomment-5836869044))
**Follow-ups filed:**
- [status#1](https://github.com/CannObserv/status/issues/1): who watches co-status
- [broker#66](https://github.com/CannObserv/broker/issues/66): broker's hard-coded check-in URL
- [notifier#90](https://github.com/CannObserv/notifier/issues/90): co-index gets its own repo

**Scope:** the MVP only. Public status pages, an admin UI and active probes each get their own spec later. Here they appear only as constraints the MVP must not block.

## Problem

Notifier is a publication service. A consumer sends a template, variables and channels, and notifier renders, validates, delivers and logs. Dead-man's timers (notifier#56) took it past that line: a monitor has notifier decide, on its own clock, that something is wrong. They brought their own state machine, sweep timers and alert wording. notifier#83 froze monitors and asked for them to move to a separate service that sends alerts through notifier like any other tenant.

That service is **co-status**. It is also the cohort's long-term home for monitoring, where an admin UI, public ecosystem status pages and active probes are expected to follow.

**What runs today** (notifier production, 2026-09-25; `notifier_dev` has no monitors):

| Monitor | Tenant | Interval / grace | Renotify | Template | Channels |
|---|---|---|---|---|---|
| `co-broker` | `co-broker` | 600s / 1200s | none | inline | 2 |
| `co-index` | `co-index` | 600s / 1200s | none | inline | 1 |
| `watcher-backup` | `watcher` | 86400s / 7200s | 86400s | inline | 2 |

No monitor uses `template_id`. The `co-broker` and `co-index` tenants use notifier for nothing else: all 56 of their dispatches came from their monitors. Watcher also sends ordinary dispatches.

**How each consumer reaches notifier:**

| Consumer | Where the URL lives | Switching takes |
|---|---|---|
| broker | the constant `NOTIFIER_CHECKIN_BASE` in `src/broker/bus_health.py` | a broker code change (broker#66) |
| co-index | `deploy/index/index-checkin.sh:74`, in the notifier repo | a notifier commit, redeployed to co-index |
| watcher | `/etc/watcher/backup.env`; the key is a systemd credential | config only |

All three read only the HTTP status of a check-in, never its body.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| **D1** | **Repo `CannObserv/status`. VM `co-status` (exe.dev, `pdx`, default size), tailnet node `status`, tag `tag:status`. Databases `status`, `status_dev`, `status_test`. Units `status*`.** | The cohort pattern is VM `co-X`, node `X`, tag `tag:X`, as with `co-index` and `co-broker`. The copied `db_safety` refuses any database not ending `_dev` or `_test` unless the unit opts in. |
| **D2** | **The API binds the tailnet address only: `:9000` production, `:9001` development. `:8000` is reserved for the public status pages; nothing binds it in the MVP, and the VM stays private on the exe.dev proxy.** | The exe.dev proxy can make only one port per VM public, and 8000 is its default. Keeping the API off that port means the public surface will be exactly what the status-pages spec puts there. |
| **D3** | **The MVP is the extraction only:** co-status runs the three monitors, and notifier no longer has any. | Status pages, admin and probes each get their own spec. |
| **D4** | **A fresh repo that copies notifier's patterns.** No fork, and no shared library yet. | Every file belongs to co-status, and the code carried over is already tested. A fork would bring notifier's history and names, plus the risk of a leftover Apprise path. A shared library would mean refactoring notifier first, and the obvious home, `co-core`, needs the GCS wheelhouse and WIF in CI. Revisit once co-status shows which pieces are really shared. |
| **D5** | **co-status sends every alert through notifier's `POST /api/v1/dispatch`, using the `notifier-client` SDK, from its own notifier tenant `co-status`.** It has no Apprise, no Jinja, no templates and no channel secrets. | notifier#83's target shape. `/dispatch` only accepts channels owned by the calling tenant, so each monitor's channels are copied into `co-status` with notifier's `scripts/copy_channels.py`. |
| **D6** | **The check-in contract is identical to notifier's, and monitor IDs are kept.** | Each consumer's switch is a base URL and a key, and needs no re-review. |
| **D7** | **"Monitor" is the general concept.** A `kind` column arrives with probes, defaulting to `heartbeat`. | The standard term already covers push and pull: Uptime Kuma and Better Stack both treat push and HTTP monitors as kinds of one thing. The column costs nothing to add later. |
| **D8** | **Authentication copies notifier's:** tenants, hashed API keys marked `production` or `development`, a key-minting script and a journald audit channel. The tenants are `co-broker`, `co-index` and **`co-watcher`**. | Consumers already send `X-API-Key`. `co-watcher` matches the cohort's naming. Notifier's own `watcher` tenant is not renamed. |
| **D9** | **History records state changes only** (`monitor_events`), never each check-in or its `variables`. | Enough for uptime figures and incident lists later, and those cannot be rebuilt after the fact. Each check-in would add about 290 rows a day that nothing planned reads. |
| **D10** | **Nothing watches co-status in the MVP.** The gap is documented, and status#1 tracks closing it, leaning towards an external dead-man's service. | The same gap notifier has had since #56, plus one new one: while notifier is down, co-status cannot deliver. See [The gap](#the-gap). |
| **D11** | **Postgres 16 and Alembic**, with a fresh history. None of notifier's migrations come across. | The same stack as notifier and the FastAPI skill patterns. The admin UI and status pages will need a real database. |
| **D12** | **co-status is developed on the co-status VM itself.** | As with notifier. Notifier's memory reservation (#85) and the SocratiCode client setup therefore apply there too. **Amended 2026-09-30 (#9):** the units run read-only releases under `/srv/status`, never the development checkout ([deploy spec](2026-09-30-deploy-releases-design.md)). |
| **D13** | **The notifier API key is a systemd credential** (`LoadCredential=`) in a root-only file, never an environment variable. | Watcher's #297 pattern. It is the only delivery secret co-status holds. |
| **D14** | **Cut over one monitor at a time, using `enabled` on both sides.** No dual-posting and no new notifier code. | Notifier keeps watching until co-status's copy has seen a fresh check-in, so there is no moment with nothing watching. See [Cutover](#cutover). |
| **D15** | **co-status tests `alerting.py` with `respx`, the same library and version range as the SDK's own tests. `client.monitors` is deleted from the SDK before notifier's next release; it has never been released.** | `respx` intercepts calls beneath the SDK's `RetryTransport`, so retries can be tested too; the SDK's `test_dispatch_retried_with_idempotency_key` relies on this. A `transport=` parameter on `NotifierClient` was considered and dropped: it would have been a notifier release whose only user was these tests. `client.monitors` sits under *Unreleased* in the SDK's CHANGELOG (no tag through `v0.3.1` contains monitors), and no consumer uses it: broker uses `urllib`, watcher `httpx`, index `curl`. |

## Design

### Components

| Unit | Bind | Database | Sends to |
|---|---|---|---|
| `status.service` | tailnet `:9000` | `status` | notifier `:9000`, production key |
| `status-dev.service` | tailnet `:9001` | `status_dev` | notifier `:9001`, development key |
| `status-sweep.timer` → `.service`, every 60s | none | `status` | notifier `:9000` |
| `status-sweep-dev.timer` → `.service`, every 60s | none | `status_dev` | notifier `:9001` |
| `:8000` | reserved (D2) | | |

**Layout.** As in notifier, `src/api/` may import `src/core/`, never the reverse.

- **`src/api/`:**
  - `/health` and `/ready`, unauthenticated, naming the database they serve;
  - `/api/v1/monitors`, with the same routes, request bodies and responses as notifier's, except `template_id` (see [Data model](#data-model));
  - the API-key dependency.
- **`src/core/`:**
  - models: `tenant`, `api_key`, `monitor`, `monitor_event`;
  - `monitors.py`: deadlines, `should_alert`, the sweep and the built-in wording, ported;
  - **`alerting.py`, the only module that talks to notifier.** It turns "send this alert" into a `notifier-client` call. Nothing above it knows about HTTP.
  - copied from notifier: `db_safety`, `logging`, `api_keys`, `tenants`.
- **`scripts/`:**
  - copied from notifier: `seed_tenant.py`, `rotate_key.py`, `delete_tenant.py`, `tailnet_bind.sh`, `load_env.sh`, `dev_server.sh`, `serve.sh`;
  - the sweep: `sweep.sh` and `sweep_monitors.py`;
  - new: `import_monitors.py`.

**Deliberately left out:** templates, channels, Apprise, Jinja, and Fernet encryption of channel URLs.

### Data model

| Table | Contents |
|---|---|
| `tenants`, `api_keys` | Copied from notifier unchanged. |
| `monitors` | Notifier's columns except `template_id`. A create or update that passes `template_id` gets a 422. The check-in contract is unaffected. |
| `monitor_events` | `id`, `monitor_id` (deleted with its monitor), `kind`, `at`, and a nullable `dispatch_id` pointing to notifier's dispatch log. It never stores `variables`. |

**Event kinds:**

| Kind | Written when |
|---|---|
| `imported` | the import script creates the monitor; history begins here |
| `first_checkin` | `pending` → `ok` |
| `missing` | the sweep marks the monitor `missing` |
| `recovered` | a check-in arrives while `missing` |
| `alert` | an `alert` check-in |
| `paused`, `resumed` | `enabled` is set to `false` or `true` |

`paused` and `resumed` exist so that future uptime figures do not count planned downtime as an outage.

**`channel_ids`** now refer to channels owned by co-status's tenant in notifier. co-status cannot check them against its own database, so creating or updating a monitor checks them against notifier's `channels.list()` and returns a 422 naming any unknown ID.

**`last_variables`** is kept, as in notifier. The status-pages spec must never expose it.

### The check-in

`POST /api/v1/monitors/{id}/checkin` follows the same order as notifier's handler:

1. **Template check** (`alert` only). Call notifier's `preview` with the monitor's templates and the reported `variables`. If rendering fails, return 422 naming the failing section and leave the monitor untouched. Templates cannot be checked when a monitor is created, because rendering needs real `variables`.
2. **Recovery.** If the monitor is `missing`, send the recovery notice and write `recovered`. This happens before `last_checkin_at` moves, so the notice can quote the length of the silence.
3. **Record.** Set `last_checkin_at`, `last_status` and `last_variables`, and set the state to `ok`. Coming from `pending`, write `first_checkin`.
4. **Report** (`alert` only). Send the monitor's `title_template` and `body_template` inline to `/dispatch`, together with the `variables`, and write `alert`.
5. **Respond** with 202 and the same body as notifier's `CheckinResponse`. Its `dispatches` holds the dispatch records notifier returned for steps 2 and 4.

**A disabled monitor still records check-ins and still sends reports.** `enabled` gates only the sweep, as in notifier. The cutover depends on this.

### The sweep

`status-sweep.timer` runs `scripts/sweep.sh`, then `sweep_monitors.py`, then `sweep_monitors()`, ported from notifier with delivery going through `alerting.py`. `TimeoutStartSec` stays: systemd skips a firing while the previous run is still going, so a sweep that never finishes would stop every later one.

**One rule changes:** `last_alert_at` is set only when notifier accepted the dispatch and returned a record, whatever its delivery `status`. `should_alert` treats a `missing` monitor with no `last_alert_at` as still owed an alert:

```python
if not monitor.enabled or not is_overdue(monitor, now):
    return False
if monitor.state != MonitorState.MISSING or monitor.last_alert_at is None:
    return True
if monitor.renotify_seconds is None:
    return False
return now - _as_utc(monitor.last_alert_at) >= timedelta(seconds=monitor.renotify_seconds)
```

The monitor becomes `missing` whether or not the alert went out, because state describes the consumer. If notifier is down, the sweep tries again on every pass, and **the alert arrives late instead of never**.

### `alerting.py`

- **Endpoint check first.** Before each sweep and before each request that would send an alert, confirm that `environment` on notifier's `/health` matches co-status's own. co-status derives its own environment from its database name, as notifier does.
  - The sweep fails loudly on a mismatch and shows up in `systemctl --failed`.
  - A check-in is still recorded, but sends nothing, and an error is logged.

  A co-status pointed at the wrong notifier inverts in the same way a consumer does (notifier's `monitors.md` § *Check the endpoint before the timer*).
- **Deterministic idempotency keys**, scoped to co-status's notifier tenant:

  | Alert | Key |
  |---|---|
  | missing (and renotifies) | `{monitor_id}:missing:{deadline}:{n}`, where `n` is the number of whole `renotify_seconds` periods since the deadline (0 for the first alert) |
  | recovery | `{monitor_id}:recovered:{last_checkin_at before this check-in, or created_at}` |
  | report | none: each `alert` check-in is a new report |

  If a sweep dies after sending but before committing, the next pass sends the same key and notifier returns the existing record, so the alert is not duplicated. The keys also make the SDK's retries safe, because it retries `POST /dispatch` only when a key is present.
- **Read `status` from the 202.** `failed` and `partial` are logged. They do not change state.
- **Notifier unreachable** (a network error, or a 5xx after the SDK's retries):
  - sweep: handled by the rule above;
  - check-in: always recorded and always answered with 202. Recovery and report notices are tried once and logged if they fail, and their event gets a null `dispatch_id`.
- **Unknown channels.** Notifier's 404 lists the unknown IDs. Retry once without them and log an error, because one deleted channel must not silence a monitor. This matches notifier's leniency today.
- **A time budget on the request path.** Every notifier call made while answering a check-in runs inside one `asyncio.timeout` of 8 seconds, below broker's and watcher's 10-second client timeouts. Anything that runs over is logged and dropped. The check-in is still recorded.
- **Built-in wording is a fixed template.** The monitor's name, silence, cadence, last check-in and deadline are passed as `variables`, never pasted into the template text. A name containing `{{` would otherwise become a 422 from notifier, and the alert would be lost. The title prefix is `[co-status]`.

### The gap

With D10, two failures go unannounced in the MVP:

1. **co-status stops.** Every timer stops with it. This is the gap notifier has had since #56, moved rather than closed.
2. **Notifier is down.** Alerts from the sweep are delayed and are sent once notifier answers. Recovery and report notices sent during the outage are lost. Before the extraction, the sweep delivered through Apprise in its own process and needed only Postgres.

co-status's docs state both and link status#1.

> **2026-09-29:** status#1 closed the first and announces the second: healthchecks.io pings from the production sweep ([plan](../plans/2026-09-29-watch-the-watchdog.md)).

### Infrastructure

**VM.** `co-status` in `pdx`, default size, running Postgres 16, the four units and agent sessions. Notifier's memory reservation (#85: `MemoryLow=` for the API and Postgres, and the slice grants) and its earlyoom configuration are copied unchanged.

**Tailnet.** Node `status`, tag `tag:status`, joined with a single-tag auth key minted before the first join (notifier#43). The key is passed as `--auth-key=file:` and then shredded (observo#264). Both APIs bind through the copied `tailnet_bind.sh`, which handles the tailscaled boot race (observo#473).

**ACL:**

| Rule | When |
|---|---|
| `tag:status` → `tag:notifier:9000,9001` | before the first sweep |
| `tag:status` added to the sources of #57's rule (`→ tag:index:6333,11434`) | during provisioning |
| `tag:broker`, `tag:index`, `tag:watcher` → `tag:status:9000` | before each consumer switches |
| remove `tag:broker` → `tag:notifier:9000` and `tag:index` → `tag:notifier:9000` | during removal from notifier |

Consumers get `:9000` only. No consumer runs a dev process against co-status, so `:9001` is reachable only from the VM itself. Watcher keeps its rule to notifier, because it still sends ordinary dispatches.

**On the notifier side:**
- a `co-status` tenant in `notifier` with a **production** key, and the monitors' channels copied in with `copy_channels.py`;
- a `co-status` tenant in `notifier_dev` with a **development** key, and dev channels seeded with `seed_dev_channels.py`.

**Secrets:**

| Where | What |
|---|---|
| `/etc/status/.env` | `DATABASE_URL` |
| repo `.env` (git-ignored) | `TEST_DATABASE_URL`, `DEV_DATABASE_URL`, `GH_TOKEN` |
| systemd credential, root-only file (D13) | the notifier API key, one per environment |
| the unit files only | `STATUS_ALLOW_PROD_DB=1`, the opt-in to the production database |

**Operator prerequisites** (an agent cannot do these):
- an `EXE_API_TOKEN` with `new` permission;
- `TAILSCALE_KEY_STATUS`, single-tag;
- an SSH key registered to the exe.dev account;
- the ACL edits above;
- the Qdrant key, installed with notifier's `install_qdrant_key.sh`.

### Cutover

**Order:**
1. **`co-index`.** Nothing in production depends on it, and both ends of its check-in are in the notifier repo.
2. **`co-watcher-backup`.** Config only, and a 26-hour window.
3. **`co-broker`,** once broker#66 has landed.

**For each monitor:**

1. **Prepare.** Mint the consumer's key in co-status. Run `copy_channels.py` on notifier, which prints each old channel ID with its new one. Export the monitor row with the read-only `psql` query in co-status's runbook.
2. **Import it disabled.** `import_monitors.py`:
   - keeps `id`, `created_at`, `state`, `last_checkin_at`, `last_status`, `last_variables` and `last_alert_at`;
   - renames the tenant `watcher` to `co-watcher` and the monitor `watcher-backup` to `co-watcher-backup`;
   - rewrites `channel_ids` using the mapping from step 1;
   - refuses a row that has a `template_id` or an ID that already exists;
   - writes `imported` and `paused`;
   - only rehearses by default, like notifier's scripts.
3. **The consumer switches** its base URL and key. Its check-ins now land on the disabled copy, which records them and sends any reports.
4. **Confirm** that co-status's `last_checkin_at` is later than the import.
5. **Hand over.** Enable co-status's copy (`resumed`), then disable notifier's copy with `PATCH enabled=false`, using the consumer's old notifier key. This must happen within notifier's window: 30 minutes after the last check-in there for broker and index, 26 hours for watcher.

**There is no moment with nothing watching.** Until step 5, notifier watches. If the switch is broken, step 4 never succeeds and notifier's copy raises the alarm, which is the right alert. After step 5, co-status's deadline counts from a fresh check-in, so the handover cannot raise a false alarm. Rolling back before step 5 means reverting the consumer. After step 5, notifier's disabled rows stay for **7 days** as a fallback.

### Removal from notifier

Once all three monitors have run on co-status for 7 days:

1. Stop and disable `notifier-sweep*`, then delete the four units from `deploy/`.
2. Delete `src/api/routes/monitors.py`, `src/api/schemas/monitor.py`, `src/core/monitors.py`, the monitor model, `scripts/sweep.sh`, `scripts/sweep_monitors.py`, and their tests.
3. Add a migration that drops `monitors`.
4. Delete `client.monitors` from the SDK, and its *Unreleased* CHANGELOG entries (D15). It was never released, so no release note is owed.
5. Retire the `co-broker` and `co-index` tenants with `delete_tenant.py`. Their 56 dispatch records are deleted with them.
   **Keep the `watcher` tenant and its channels**: `watcher.service` dispatches through them. Revoke only the backup's own key, the tenant's second production key (notifier#62); watcher then deletes its `.pre-status` copy (CannObserv/watcher#330).
6. Remove the two ACL rules.
7. Docs:
   - `AGENTS.md`: the dead-man's paragraph, the freeze, the sweep rows in the infrastructure and lifecycle tables, and the monitor rules in the API boundary principles;
   - `docs/reference/monitors.md` shrinks to a pointer to co-status;
   - `DEPLOYMENT.md`, `tailscale.md` and `ARCHITECTURE.md` are updated to match.

## Testing

Test-first throughout: red, green, refactor.

**Copied from notifier:**
- the pytest setup: a `status_test` database, the `integration` marker, and a conftest that points `DATABASE_URL` at the test database;
- the coverage gate and the `tests/ci/` checks that pin it: `fail_under = 80`, `core = "sysmon"`, CPython 3.12, `uv sync --locked`;
- the ruff rule list with `test_lint_selectors.py`, and the dependency-policy checks;
- `test_no_channel_urls.py`, because the repo is public;
- CI with `lint`, `test` and `migrations` jobs. It needs no cloud login and no wheelhouse, because `notifier-client` installs from a public git tag.

**Ported:** notifier's monitor tests (deadlines, `should_alert`, durations, the routes, the sweep script), rewritten wherever they asserted on Apprise.

**New:**
- **Contract.** co-status's OpenAPI schemas for the check-in request and response must equal a snapshot of notifier's, taken at a pinned notifier commit with notifier's `dump_openapi.py`. This keeps D6 true over time.
- **`alerting.py`**, through `respx` (D15):
  - the endpoint check runs first;
  - idempotency keys are deterministic;
  - a `missing` monitor with no `last_alert_at` is retried;
  - an unknown-channel 404 is retried once without those channels;
  - the request-path budget drops the notifier call but still records the check-in;
  - a name containing `{{` arrives as data.
- **Disabled monitors** record check-ins and send reports.
- **Import:** dry run by default; it refuses a `template_id` or an existing ID; it applies the renames and the channel mapping.
- **Deploy:** the unit files, covering `TimeoutStartSec` on the sweep, `LoadCredential`, the tailnet-only bind and the memory reservation.

**Proving the alarm fires** (notifier#56: an alarm is seen to fire, not assumed to):
1. **Dev.** co-status `:9001` sends to notifier `:9001`. Create a monitor, check in, let it go missing, and see the alert arrive in a dev channel. Then check in and see the recovery notice.
2. **Production.** Create a throwaway `co-status-drill` monitor with a 60-second interval and never check in. It should alert within about 2 minutes. Check in, see the recovery notice, then delete the monitor.

## Plan

### Phase 1: scaffold
Copy the infrastructure listed under [Components](#components) and [Testing](#testing). Get CI green on an app that serves only `/health` and `/ready`.

### Phase 2: port and build
Monitors, `monitor_events`, `alerting.py`, the check-in route, the sweep and `import_monitors.py`, all test-first. Add the contract snapshot test.

### Phase 3: provision
Once the operator prerequisites are in place: the VM, Postgres, the units, the tailnet, the first two ACL rows, and the `co-status` tenants, keys and channels in notifier. Reboot once and confirm the node comes back with the same identity and bind.

### Phase 4: prove the alarm
The dev drill, then the production drill.

### Phase 5: cutover
`co-index`, then `co-watcher-backup`, then `co-broker` (after broker#66).

### Phase 6: soak
7 days with all three monitors on co-status and notifier's copies disabled.

### Phase 7: removal from notifier
As listed under [Removal from notifier](#removal-from-notifier). notifier#83 closes.

## Risks

1. **The handover window is missed.** Notifier's copy sends a false "stopped reporting" alert after 30 minutes. It is noisy, not silent. The runbook times steps 4 and 5.
2. **Notifier is down.** Sweep alerts are delayed, and recovery and report notices are lost ([The gap](#the-gap)). Tracked in status#1.
3. **co-status is down.** Nothing says so (D10). Tracked in status#1.
4. **The check-in contract drifts** from notifier's and a consumer breaks. The snapshot test catches it.
5. **Endpoint inversion.** co-status dev sending to notifier production, or the reverse. The endpoint check catches it.
6. **Copied infrastructure drifts** from notifier's copies. This is D4's accepted cost. Revisit a shared library when a third service copies the same files.
7. **Memory** on a VM shared with agent sessions. The reservation is copied (D12).

## Success criteria

- [x] `co-status` in `pdx`, `tag:status`, survives a reboot with the same identity (2026-09-28; again 2026-09-29 for #5)
- [x] The API answers on the tailnet and is unreachable from the exe.dev proxy and the internet; nothing listens on `:8000` (binds `100.88.216.92:9000,9001` only)
- [x] CI green (`lint`, `test`, `migrations`), coverage at least 80%
- [x] The contract snapshot test passes against notifier's pinned schema
- [x] The dev and production drills were both seen to fire and to recover (2026-09-28)
- [x] All three monitors run on co-status and notifier's copies are disabled, with no false alert during any handover (2026-09-28, RUNBOOK § Cutover)
- [x] 7-day soak completed (2026-09-28 18:31 → 2026-10-05 18:31Z), with one 40-minute sweep outage on 2026-09-29 that missed nothing; cause fixed in #9
- [x] Notifier has no monitor code, tables, timers or SDK methods; `co-broker` and `co-index` are retired from it; the watcher backup's key is revoked; the two ACL rules are removed; its docs are updated (2026-10-02, notifier#83)
- [x] co-status's docs state [the gap](#the-gap) and link status#1 — superseded: #1 closed the gap, and [monitors.md § Who watches co-status](../reference/monitors.md#who-watches-co-status) describes what does

## Out of scope

- Watching co-status, and delivery while notifier is down (status#1)
- Public status pages, an admin UI, active probes, and cohort-wide rollups (each gets its own spec)
- Renaming notifier's `watcher` tenant
- Moving co-index's deploy files out of notifier (notifier#90)
- A co-status client SDK: consumers use plain HTTP
- Retrying recovery and report notices lost while notifier is down
