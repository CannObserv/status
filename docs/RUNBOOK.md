# co-status runbook

Setting up the co-status VM, running it, and moving monitors over from notifier. Design and reasons: [the MVP spec](specs/2026-09-26-co-status-mvp-design.md). Tracking: [#2](https://github.com/CannObserv/status/issues/2).

## First-time setup

**Run 2026-09-28 (Phase 3)** on `co-status`: node `status`, `100.88.216.92`, `tag:status`. Corrected against the machine where it was wrong. The units moved from the development checkout to `/srv/status` releases on 2026-09-30 (#9).

Prerequisites the operator supplies (spec § Infrastructure): `TAILSCALE_KEY_STATUS` (single-tag `tag:status`, pre-approved, non-ephemeral), the ACL rows, and both notifier API keys.

```bash
# Join the tailnet first: both API units bind this host's tailnet address and
# will not start without one. Key from a file, then shredded (observo#264).
sudo install -m 600 /dev/null /run/ts.key
sudo tee /run/ts.key >/dev/null <<< 'tskey-auth-...'   # tag:status, pre-approved, NOT ephemeral
sudo systemctl enable --now tailscaled
sudo tailscale up --auth-key=file:/run/ts.key --hostname=status --ssh
sudo shred -u /run/ts.key
tailscale ip -4

sudo apt-get install -y postgresql-16 postgresql-client-16

# The production env file. DATABASE_URL only: the notifier key is a
# credential, never an env var (D13).
sudo mkdir -p /etc/status
sudo tee /etc/status/.env > /dev/null <<'EOF'
DATABASE_URL=postgresql+asyncpg://status:<generated>@localhost:5432/status
EOF
sudo chmod 640 /etc/status/.env

# The notifier keys, root-only; systemd hands them to the units (D13).
# Production key for notifier :9000, development key for notifier :9001 —
# notifier's `co-status` tenant in each database.
sudo install -m 400 -o root -g root /dev/null /etc/status/notifier-api.key
sudo tee /etc/status/notifier-api.key >/dev/null <<< 'nk_...'       # production
sudo install -m 400 -o root -g root /dev/null /etc/status/notifier-api-dev.key
sudo tee /etc/status/notifier-api-dev.key >/dev/null <<< 'nk_...'   # development

# The healthchecks.io ping key (#1), production sweep only; § Watching
# co-status. Absent, the sweep runs unwatched rather than not at all.
sudo install -m 400 -o root -g root /dev/null /etc/status/hc-ping.key
sudo tee /etc/status/hc-ping.key >/dev/null <<< '<ping-key>'

# Postgres
sudo -u postgres psql -c "CREATE USER status WITH PASSWORD '<generated>';"
sudo -u postgres psql -c "CREATE DATABASE status OWNER status;"
sudo -u postgres psql -c "CREATE DATABASE status_test OWNER status;"
sudo -u postgres psql -c "CREATE DATABASE status_dev OWNER status;"

# The dev units' database (#9, R11): its own file, never /etc/status/.env,
# which is the production file. The development checkout's git-ignored .env
# carries DEV_DATABASE_URL and TEST_DATABASE_URL too, for hand-run servers and
# pytest; no unit reads it.
sudo install -m 640 -o root -g exedev /dev/null /etc/status/dev.env
echo 'DEV_DATABASE_URL=postgresql+asyncpg://status:<generated>@localhost:5432/status_dev' \
  | sudo tee /etc/status/dev.env > /dev/null

# The deploy root (docs/DEPLOYMENT.md), root's so that only sudo changes what
# the units run (#14); deploy.sh refuses it otherwise. The first deploy builds a release from
# origin/main, migrates status_dev then status, links both targets, and
# installs their units (#18); --no-restart because none is enabled yet. It
# names each new unit with its enable --now, and warns about every host
# config below, not yet installed.
sudo install -d -m 755 /srv/status
cd /home/exedev/status && scripts/deploy.sh --no-restart

# Units: the API on :9000, the dev API on :9001, and both sweeps, all running
# /srv/status/{live,dev}. The sweep *timers* are enabled; their .service units
# are started by the timers. Every later deploy installs unit edits itself.
sudo systemctl enable --now status status-dev
sudo systemctl enable --now status-sweep.timer status-sweep-dev.timer
# The drift check (#12), production only; § Watching co-status first.
sudo systemctl enable --now status-drift.timer

# Memory reservation. Values are notifier's (notifier#74, #85) as a starting
# point; re-measure here and adjust (spec Phase 3).
sudo cp deploy/99-status-memory.conf /etc/sysctl.d/
sudo sysctl -p /etc/sysctl.d/99-status-memory.conf
# Install earlyoom BEFORE copying its config. The other order leaves dpkg
# asking whether to keep a modified conffile, which fails without a terminal
# and aborts the install half-configured (found on the first run here).
sudo apt-get install -y earlyoom
sudo cp deploy/earlyoom.default /etc/default/earlyoom
sudo systemctl enable earlyoom && sudo systemctl restart earlyoom
for f in system.slice.d/10-memory-protection.conf \
         system-postgresql.slice.d/10-memory-protection.conf \
         postgresql@16-main.service.d/10-memory.conf; do
  sudo install -D -m 644 "deploy/$f" "/etc/systemd/system/$f"
done
sudo systemctl daemon-reload

# needrestart lists restarts and never performs them (notifier#91).
sudo install -D -m 644 deploy/needrestart.conf.d/status.conf \
     /etc/needrestart/conf.d/status.conf
```

**Check before anything depends on it:**

```bash
curl -s "http://$(tailscale ip -4):9000/health"   # "environment":"production"
curl -s "http://$(tailscale ip -4):9001/health"   # "environment":"development"
systemctl list-timers 'status-sweep*'             # both scheduled
journalctl -u status-sweep -n 3                   # "monitor sweep complete"
```

Reboot once and confirm the node returns with the same identity, tag and bind.

## Routine ops

| Situation | Action |
|---|---|
| Ship what is on `origin/main` | `scripts/deploy.sh`: once its CI passed (it waits for a run still going), migrates, switches, restarts and verifies dev and then live, and switches back on failure ([DEPLOYMENT.md](DEPLOYMENT.md)) |
| Ship to live past CI (emergency, or GitHub down) | `scripts/deploy.sh --skip-ci [<sha>]`; logged to `journalctl -t status-deploy` ([§ The CI gate](DEPLOYMENT.md#the-ci-gate)) |
| Try a pushed branch on :9001 | `scripts/deploy.sh --dev origin/<branch>` |
| Roll back | `journalctl -t status-deploy -n 20`, then `scripts/deploy.sh <previous build>` |
| Change a unit | Edit `deploy/`, merge, `scripts/deploy.sh`: it installs the units that differ and reloads; a new unit it names with its `enable --now` ([DEPLOYMENT.md § Units](DEPLOYMENT.md#units)) |
| Change a host config (sysctl, earlyoom, slices, needrestart) | Edit `deploy/`, merge, then install it by hand as under First-time setup. A live deploy warns while it differs |
| What is running | `readlink /srv/status/live /srv/status/dev`; `build` in `/health` and in every sweep line |
| Who changed a release or a link by hand? | Root owns them, so any change took sudo (#14): `sudo journalctl _COMM=sudo -o short-iso \| grep -E '/srv/status\|COMMAND=/(usr/)?bin/(ba)?sh$\|COMMAND=/(usr/)?bin/su( \|$)'`. A root shell shows only as a shell, never what it did. Lines that fall within a deploy (`journalctl -t status-deploy`) are the deploy's own ([DEPLOYMENT.md § Who owns a release](DEPLOYMENT.md#who-owns-a-release)) |
| Is the sweep firing? | `systemctl list-timers 'status-sweep*'` |
| What did it find? | `journalctl -u status-sweep -f` — `checked`, `alerted`, `owed`, `undeliverable`, `undelivered`, `undelivered_notices` every pass |
| Force a pass | `sudo systemctl start status-sweep.service` |
| Is live behind `main`? | `sudo systemctl start status-drift && journalctl -u status-drift -n 3 -o cat` (#12) |
| Which key was minted, revoked, or destroyed with its tenant | `journalctl -t status-keys` |

**`owed` that does not drain** means notifier is not accepting alerts: check notifier's `/health` from here, and `journalctl -u status-sweep` for the reason. The alerts go out, under their original keys, on the first pass notifier accepts them.

**`undelivered` that does not clear** means notifier took a missing alert but a channel failed it (`failed`), or failed one of several (`partial`). `journalctl -u status-sweep | grep 'with status'` gives each dispatch id with its `monitor_id`; notifier's dispatch attempts say which channel and why. The sweep redelivers it at about +1, +6, +21 and +81 minutes ([#10](https://github.com/CannObserv/status/issues/10)), and the journal line's `redelivered` shows each result. Fix the channel within that window and the next redelivery clears it. Once `redelivered` says `capped`, notifier has used every attempt on that dispatch, and fixing the channel no longer helps. It then clears only when the monitor recovers or a later renotify is delivered, so tell the monitor's owner directly. Pausing the monitor only hides it until it is resumed.

**`undelivered_notices`** is the same for the check-in path: notifier took a recovery (`"recovery"`) or the consumer's own `alert` report (`"report"`) and a channel failed it ([#8](https://github.com/CannObserv/status/issues/8)). The dispatch is in the **API's** journal, not the sweep's: `journalctl -u status | grep 'with status'`, or by monitor: `sudo -u postgres psql status -c "SELECT kind, at, dispatch_id, dispatch_status FROM monitor_events WHERE monitor_id = '<id>' ORDER BY at DESC LIMIT 5"`. A report is the consumer saying something is wrong, and nobody heard it: tell the monitor's owner what it said (notifier's dispatch has the rendered text). The owner can see the status, but not the text, as `last_report_status` on the monitor ([#20](https://github.com/CannObserv/status/issues/20)). It clears when the next notice of that kind is delivered, or 24 hours after it was sent. Nothing resends it.

**`not_accepted`** in `undelivered_notices` means notifier never took the notice, so there is no dispatch and no rendered text ([#19](https://github.com/CannObserv/status/issues/19)). The reason is in the API's journal at that time: `journalctl -u status | grep -E 'not accepted|not sending|cannot check|no notifier key'`. Fix that first (an outage, a revoked key, deleted channels). The report's `variables` survive only as the monitor's `last_variables`, and only until its next check-in; events never keep them (D9).

**healthchecks.io watches the sweep, the API and what is deployed** ([monitors.md § Who watches co-status](reference/monitors.md#who-watches-co-status)). An alert from it means:

| Check down | Look at |
|---|---|
| `co-status-sweep`, silent | `systemctl list-timers 'status-sweep*'`, `systemctl status status-sweep`, then the VM |
| `co-status-sweep`, `/fail` | `journalctl -u status-sweep -n 50`; the ping body names the exception type. A connection error is usually Postgres |
| `notifier-reachable` | The ping body names each cause that applies: unreachable, *n* owed, *n* undelivered, *n* check-in notices undelivered. notifier's `/health` from here; then `journalctl -u status-sweep \| grep 'not accepted'` — a refusal (revoked key, deleted channels) keeps it down just as an outage does. For undelivered and undelivered notices, see above |
| `co-status-api`, `/fail` | The API was not ready for 20 s; the body is its answer or the error. `systemctl status status`, `journalctl -u status -n 50`. By body: **`ConnectError: All connection attempts failed`**, nothing listening; **`Name or service not known`**, the tailnet no longer resolves `status` (renamed node? `tailscale status`); **`ConnectTimeout`**, packets go nowhere (`tailscale status`); **`TimeoutError`** or **`ReadTimeout`**, connected but no answer: a wedged API or a Postgres that hangs; **`503`** with `"db":false`, Postgres; with `schema_state`, a migration ([DEPLOYMENT.md § The schema check](DEPLOYMENT.md#the-schema-check)); **`environment` not `production`**, `DATABASE_URL` in `/etc/status/.env` |
| `co-status-api`, silent | The sweep is not running; `co-status-sweep` says the same |
| `co-status-drift`, `/fail` | Live has lagged `main` in code for over 8 h, or is not on `main`. The body names both builds, the push the clock started at, and `main`'s CI. **CI `success`**: `scripts/deploy.sh`. **Anything else**: fix CI on `main` first; the gate refuses it ([DEPLOYMENT.md § The CI gate](DEPLOYMENT.md#the-ci-gate)). **Not on `main`, or `GitHub does not know live`**: `readlink /srv/status/live` against `git log origin/main`: `main` was rewritten after the deploy (live deploys only take commits on `main`). Every call 404ing means the repo is no longer public, and the CI gate needs a token too ([DEPLOYMENT.md § The CI gate](DEPLOYMENT.md#the-ci-gate)). Clears on the next hourly run after a deploy, or now: `sudo systemctl start status-drift`. **After a rollback** this fires 8 h later by design: `main` still holds what was rolled back, so fix or revert it there and deploy |
| `co-status-drift`, silent | `systemctl list-timers status-drift.timer`, `journalctl -u status-drift -n 20`. **`203/EXEC`** after a rollback: the build predates #12 and has no `scripts/drift.sh`; the check stays silent until live is past it. `GitHub did not answer` lines (and `/log` entries in the check's dashboard) mean GitHub refused or was unreachable for over 2 h: rate limit or outage, nothing to fix here |
| All silent, host fine | `journalctl -u status-sweep \| grep -i healthchecks`: a missing key or a ping that cannot get out. This host's only DNS is Tailscale's (100.100.100.100), so tailscaled down silences every ping: `tailscale status` |

**A failed API stays failed.** After 5 failed starts in 15 minutes, `status.service` stops retrying. Fix the cause, then `sudo systemctl reset-failed status && sudo systemctl start status`. A bad release is a rollback instead (above).

**While the API is down, every consumer goes `missing`** once its own interval and grace run out, and notifier carries those alerts. They are about co-status, not the consumers: tell their owners. Each recovers, with a recovery notice, on its first check-in after the API is back. **A consumer `missing` while `co-status-api` is green** is that consumer, or the tailnet ACL between it and `tag:status:9000`: the probe runs on this host and never crosses the ACL.

## Watching co-status: healthchecks.io

Org account, free plan (#1, #13). Set up once:

1. Create four checks: slugs **`co-status-sweep`**, **`notifier-reachable`** and **`co-status-api`**, **period 1 minute, grace 5 minutes**: that absorbs `OnBootSec=2min` and `TimeoutStartSec=120` without flapping. And **`co-status-drift`** (#12), **period 1 hour, grace 2 hours**: hourly runs, and GitHub refusing for a run or two. Create a check before deploying the sweep that pings it: until it exists, every ping to it answers 404 and the sweep warns on every pass.
2. Attach the project's email and Slack integrations to all four. Never route them through notifier: it is one of the things being watched.
3. Copy the project's **ping key** (project Settings → Ping key) into `/etc/status/hc-ping.key`, as under First-time setup. It is a credential: never in `.env`, a tracked file or a chat.
4. Deploy (`scripts/deploy.sh` installs the unit, [DEPLOYMENT.md § Units](DEPLOYMENT.md#units)) and confirm:

```bash
sudo systemctl start status-sweep.service
journalctl -u status-sweep -n 5 -o cat | grep -i healthchecks   # nothing: every ping answered OK
```

The sweep's three turn green in the dashboard within a minute. The drift check runs hourly from `status-drift.timer` (First-time setup); its first run, now:

```bash
sudo systemctl start status-drift.service
journalctl -u status-drift -n 3 -o cat     # "drift check: live <build> is main", or how far behind
```

**Test an alert once.** It must reach email and Slack, and the next pass turns the check green again. The key goes to curl as config on stdin, so it never appears in `ps`:

```bash
sudo sh -c 'printf "url = https://hc-ping.com/%s/co-status-sweep/fail\n" "$(cat /etc/status/hc-ping.key)" | curl -fsS -X POST -K -'
```

**Rotating the key:** healthchecks.io → project Settings → Ping key → regenerate. Then rewrite the file; the next pass reads it, and no restart is needed.

**The dev units are not watched**, by design (#13): no check, no `OnFailure=`. `scripts/deploy.sh` verifies `:9001` on every deploy. Otherwise, run `systemctl status status-dev status-sweep-dev`.

## Cutover: moving one monitor from notifier

**Run 2026-09-28 (Phase 5)** for all three, in this order. Notifier keeps watching until step 5, so there is no moment with nothing watching (spec § Cutover).

| Monitor | First check-in on co-status | co-status enabled | notifier disabled | Record |
|---|---|---|---|---|
| `co-index` | 17:22:17 | 17:22:29 | 17:22:30 | #2 |
| `co-broker` | 18:21:06 | 18:26:40 | 18:26:52 | CannObserv/broker#66 |
| `co-watcher-backup` | 18:28:51 | 18:31:18 | by 18:34 | CannObserv/watcher#330 |

**Keys never go on a command line.** `curl -H "X-API-Key: $KEY"` puts the key in curl's argv, where `ps` shows it. Every call below passes it on stdin: `printf 'X-API-Key: %s\n' "$KEY" | curl -H @- …` (`printf` is a shell builtin).

### 1. Prepare

```bash
# co-status (this host): the consumer's tenant and key. The key waits
# root-only in /etc/status/pending/ until the consumer has it (step 3);
# the other three lines are the ids to record.
. scripts/load_env.sh
out=$(STATUS_ALLOW_PROD_DB=1 uv run python scripts/seed_tenant.py co-broker co-broker-checkin production)
grep -v '^raw_key=' <<< "$out"
sudo install -d -m 700 /etc/status/pending
sudo install -m 400 -o root -g root /dev/null /etc/status/pending/co-broker.key
sed -n 's/^raw_key=//p' <<< "$out" | sudo tee /etc/status/pending/co-broker.key >/dev/null
unset out

# notifier (notifier.exe.xyz): copy the monitor's channels into notifier's
# `co-status` tenant. Keep the source_channel_id / channel_id pairs it prints.
NOTIFIER_ALLOW_PROD_DB=1 uv run python scripts/copy_channels.py \
    --to-tenant <notifier co-status tenant id> --expect-name co-status \
    --channel <source channel id>=<new name> [--channel ...] --dry-run
# …then without --dry-run
```

Export the monitor's row, **read-only**, on notifier's host:

```bash
sudo -u postgres psql -X -q -A -t -d notifier -v ON_ERROR_STOP=1 -v id=<monitor id> \
  > co-broker.json <<'SQL'
SET default_transaction_read_only = on;
SELECT row_to_json(r) FROM (
  SELECT m.id, t.name AS tenant_name, m.name, m.enabled, m.interval_seconds,
         m.grace_seconds, m.renotify_seconds, m.channel_ids, m.template_id,
         m.title_template, m.body_template, m.state, m.last_checkin_at,
         m.last_status, m.last_variables, m.last_alert_at, m.created_at
  FROM monitors m JOIN tenants t ON t.id = m.tenant_id
  WHERE m.id = :'id'
) r;
SQL
```

Shred the export on both hosts once step 2 is done.

### 2. Import it, disabled

```bash
. scripts/load_env.sh
STATUS_ALLOW_PROD_DB=1 uv run python scripts/import_monitors.py \
    --row co-broker.json --expect-tenant co-broker \
    --channel <source channel id>=<channel id> [--channel ...] --dry-run
# …then without --dry-run
```

`watcher` becomes `co-watcher` and `watcher-backup` becomes `co-watcher-backup` on the way in; the id does not change. The monitor arrives disabled, with `imported` and `paused` events.

### 3. The consumer switches

Base URL `http://status:9000`, and the key minted in step 1. Nothing else changes: same path, body, and monitor id (spec D6).

**The key crosses terminal to terminal.** The consumer's operator reads it in the co-status browser terminal (https://co-status.xterm.exe.xyz) with `sudo cat /etc/status/pending/<tenant>.key` and writes it into the consumer's credential file, root-only. It goes into no issue, chat or shell history.

**The consumer keeps its old notifier key**, root-only, renamed aside. Step 5 needs it, and until Phase 7 it is the only credential that can pause notifier's copy.

- **co-index**: notifier's `deploy/index/index-checkin.sh`, redeployed to co-index. Old env file: `notifier.env.pre-status-83`.
- **co-watcher**: `/etc/watcher/backup.env` and the unit's `notifier-key` credential file on the watcher VM. Old key: `/etc/watcher/backup-notifier.key.pre-status`.
- **co-broker**: CannObserv/broker#66 (`8a00f56`), key in `/etc/broker/status.env`. Old env file: `/etc/broker/notifier.env.pre-status-66`.

A manual run on the consumer lands the first check-in at a chosen time instead of the next tick.

### 4. Confirm

```bash
sudo cat /etc/status/pending/<tenant>.key | { read -r KEY
  printf 'X-API-Key: %s\n' "$KEY" | curl -s -H @- "http://status:9000/api/v1/monitors/<id>"; } \
  | jq .last_checkin_at
```

Later than the import. If it never moves, the switch is broken and notifier's copy will say so — the right alarm.

### 5. Hand over

Within notifier's window since its last check-in there (interval + grace: 30 minutes for broker and index, 26 hours for watcher). A miss is noisy, not silent: notifier's copy alerts once. Disable it anyway and carry on.

```bash
# co-status (this host): enable (writes `resumed`), then shred the pending key.
sudo cat /etc/status/pending/<tenant>.key | { read -r KEY
  printf 'X-API-Key: %s\n' "$KEY" | curl -s -X PATCH -H @- -H 'Content-Type: application/json' \
    -d '{"enabled": true}' "http://status:9000/api/v1/monitors/<id>"; } | jq .enabled
sudo shred -u /etc/status/pending/<tenant>.key
```

```bash
# The consumer's host: disable notifier's copy with the old notifier key, so
# that key never leaves the host it lives on.
printf 'X-API-Key: %s\n' "$OLD_NOTIFIER_KEY" | curl -s -X PATCH -H @- \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false}' "http://notifier:9000/api/v1/monitors/<id>"
```

Then the consumer's next **scheduled** run must land on co-status by itself, and the next sweep's `checked` must count the monitor. The monitor's soak clock starts at the handover, not at the first check-in.

notifier's disabled row stays for 7 days as the fallback. Rolling back before step 5 is reverting the consumer.

## After the cutover

**The soak** (Phase 6) ends 7 days after the last handover: no earlier than 2026-10-05 18:31 UTC. healthchecks.io watches the sweep (#1); check it daily anyway until then, because the counts are what the soak is about:

```bash
systemctl list-timers 'status-sweep*'
journalctl -u status-sweep -n 1 -o cat | jq -c '{checked, alerted, owed, undeliverable, undelivered, undelivered_notices}'   # checked: 3
```

**What Phase 7 (removal from notifier) must know**, per consumer. The notifier side is notifier's work (spec § Removal from notifier); these are the leftovers the cutover created.

| Consumer | In notifier | Tailnet | On the consumer, afterwards |
|---|---|---|---|
| `co-index` | retire the `co-index` tenant (`delete_tenant.py`) | remove `tag:index` → `tag:notifier:9000` | delete `notifier.env.pre-status-83` |
| `co-broker` | retire the `co-broker` tenant (`delete_tenant.py`) | remove `tag:broker` → `tag:notifier:9000` | delete `/etc/broker/notifier.env.pre-status-66` |
| `co-watcher-backup` | **keep the `watcher` tenant and its channels**: `watcher.service` dispatches through them. Revoke only the backup's own key, the tenant's second production key (notifier#62) | **keep** `tag:watcher` → `tag:notifier:9000` | delete `/etc/watcher/backup-notifier.key.pre-status` |
