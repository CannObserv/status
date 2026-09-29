# co-status runbook

Setting up the co-status VM, running it, and moving monitors over from notifier. Design and reasons: [the MVP spec](specs/2026-09-26-co-status-mvp-design.md). Tracking: [#2](https://github.com/CannObserv/status/issues/2).

## First-time setup

**Run 2026-09-28 (Phase 3)** on `co-status`: node `status`, `100.88.216.92`, `tag:status`. Corrected against the machine where it was wrong.

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

# The healthchecks.io ping key (#1), production sweep only; § Watching the
# sweep. Absent, the sweep runs unwatched rather than not at all.
sudo install -m 400 -o root -g root /dev/null /etc/status/hc-ping.key
sudo tee /etc/status/hc-ping.key >/dev/null <<< '<ping-key>'

# Postgres
sudo -u postgres psql -c "CREATE USER status WITH PASSWORD '<generated>';"
sudo -u postgres psql -c "CREATE DATABASE status OWNER status;"
sudo -u postgres psql -c "CREATE DATABASE status_test OWNER status;"
sudo -u postgres psql -c "CREATE DATABASE status_dev OWNER status;"

# The dev endpoint's database, in the git-ignored repo .env — never in
# /etc/status/.env, which is the production file.
cd /home/exedev/status
grep -q '^DEV_DATABASE_URL=' .env 2>/dev/null || \
  echo 'DEV_DATABASE_URL=postgresql+asyncpg://status:<generated>@localhost:5432/status_dev' >> .env

# Dependencies, then migrations on both databases: an unmigrated status_dev
# makes status-dev refuse to start.
uv sync
. scripts/load_env.sh
uv run alembic upgrade head
DATABASE_URL="$DEV_DATABASE_URL" uv run alembic upgrade head

# Units: the API on :9000, the dev API on :9001, and both sweeps. The sweep
# *timers* are enabled; their .service units are started by the timers.
sudo cp deploy/status.service deploy/status-dev.service \
        deploy/status-sweep.service deploy/status-sweep.timer \
        deploy/status-sweep-dev.service deploy/status-sweep-dev.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now status status-dev
sudo systemctl enable --now status-sweep.timer status-sweep-dev.timer

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
| Code committed to main | `sudo systemctl restart status status-dev` |
| After model changes | `. scripts/load_env.sh && uv run alembic upgrade head`, the same with `DATABASE_URL="$DEV_DATABASE_URL"`, then restart both |
| Is the sweep firing? | `systemctl list-timers 'status-sweep*'` |
| What did it find? | `journalctl -u status-sweep -f` — `checked`, `alerted`, `owed`, `undeliverable`, `undelivered` every pass |
| Force a pass | `sudo systemctl start status-sweep.service` |
| Which key was minted, revoked, or destroyed with its tenant | `journalctl -t status-keys` |

**`owed` that does not drain** means notifier is not accepting alerts: check notifier's `/health` from here, and `journalctl -u status-sweep` for the reason. The alerts go out, under their original keys, on the first pass notifier accepts them.

**`undelivered` that does not clear** means notifier took a missing alert but a channel failed it (`failed`), or failed one of several (`partial`). `journalctl -u status-sweep | grep 'with status'` gives each dispatch id with its `monitor_id`; notifier's dispatch attempts say which channel and why. Fixing the channel does not clear it: it clears when the monitor recovers or a later renotify is delivered. Pausing the monitor only hides it until it is resumed. Without `renotify_seconds` nothing resends ([#7](https://github.com/CannObserv/status/issues/7)), so tell the monitor's owner directly.

**healthchecks.io watches the sweep** ([monitors.md § Who watches the sweep](reference/monitors.md#who-watches-the-sweep)). An alert from it means:

| Check down | Look at |
|---|---|
| `co-status-sweep`, silent | `systemctl list-timers 'status-sweep*'`, `systemctl status status-sweep`, then the VM |
| `co-status-sweep`, `/fail` | `journalctl -u status-sweep -n 50`; the ping body names the exception type. A connection error is usually Postgres |
| `notifier-reachable` | The ping body names each cause that applies: unreachable, *n* owed, *n* undelivered. notifier's `/health` from here; then `journalctl -u status-sweep \| grep 'not accepted'` — a refusal (revoked key, deleted channels) keeps it down just as an outage does. For undelivered, see above |
| Both silent, host fine | `journalctl -u status-sweep \| grep -i healthchecks`: a missing key or a ping that cannot get out |

## Watching the sweep: healthchecks.io

Org account, free plan (#1). Set up once:

1. Create two checks: slugs **`co-status-sweep`** and **`notifier-reachable`**, **period 1 minute, grace 5 minutes**. That absorbs `OnBootSec=2min` and `TimeoutStartSec=120` without flapping.
2. Attach the project's email and Slack integrations to both. Never route them through notifier: it is one of the things being watched.
3. Copy the project's **ping key** (project Settings → Ping key) into `/etc/status/hc-ping.key`, as under First-time setup. It is a credential: never in `.env`, a tracked file or a chat.
4. Install the unit and confirm:

```bash
sudo cp deploy/status-sweep.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl start status-sweep.service
journalctl -u status-sweep -n 5 -o cat | grep -i healthchecks   # nothing: every ping answered OK
```

Both checks turn green in the dashboard within a minute. **Test an alert once.** It must reach email and Slack, and the next pass turns the check green again. The key goes to curl as config on stdin, so it never appears in `ps`:

```bash
sudo sh -c 'printf "url = https://hc-ping.com/%s/co-status-sweep/fail\n" "$(cat /etc/status/hc-ping.key)" | curl -fsS -X POST -K -'
```

**Rotating the key:** healthchecks.io → project Settings → Ping key → regenerate. Then rewrite the file; the next pass reads it, and no restart is needed.

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
journalctl -u status-sweep -n 1 -o cat | jq -c '{checked, alerted, owed, undeliverable, undelivered}'   # checked: 3
```

**What Phase 7 (removal from notifier) must know**, per consumer. The notifier side is notifier's work (spec § Removal from notifier); these are the leftovers the cutover created.

| Consumer | In notifier | Tailnet | On the consumer, afterwards |
|---|---|---|---|
| `co-index` | retire the `co-index` tenant (`delete_tenant.py`) | remove `tag:index` → `tag:notifier:9000` | delete `notifier.env.pre-status-83` |
| `co-broker` | retire the `co-broker` tenant (`delete_tenant.py`) | remove `tag:broker` → `tag:notifier:9000` | delete `/etc/broker/notifier.env.pre-status-66` |
| `co-watcher-backup` | **keep the `watcher` tenant and its channels**: `watcher.service` dispatches through them. Revoke only the backup's own key, the tenant's second production key (notifier#62) | **keep** `tag:watcher` → `tag:notifier:9000` | delete `/etc/watcher/backup-notifier.key.pre-status` |
