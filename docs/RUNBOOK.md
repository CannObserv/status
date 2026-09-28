# co-status runbook

Setting up the co-status VM, running it, and moving monitors over from notifier. Design and reasons: [the MVP spec](plans/2026-09-26-co-status-mvp-design.md). Tracking: [#2](https://github.com/CannObserv/status/issues/2).

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
| What did it find? | `journalctl -u status-sweep -f` — `checked`, `alerted`, `owed`, `undeliverable` every pass |
| Force a pass | `sudo systemctl start status-sweep.service` |
| Which key was minted, revoked, or destroyed with its tenant | `journalctl -t status-keys` |

**`owed` that does not drain** means notifier is not accepting alerts: check notifier's `/health` from here, and `journalctl -u status-sweep` for the reason. The alerts go out, under their original keys, on the first pass notifier accepts them.

**Nothing watches this host yet** ([monitors.md § The gap](reference/monitors.md#the-gap), [#1](https://github.com/CannObserv/status/issues/1)).

## Cutover: moving one monitor from notifier

Order: `co-index`, then `co-watcher-backup`, then `co-broker` (after CannObserv/broker#66). Notifier keeps watching until step 5, so there is no moment with nothing watching (spec § Cutover).

### 1. Prepare

```bash
# co-status (this host): the consumer's tenant and key.
. scripts/load_env.sh
STATUS_ALLOW_PROD_DB=1 uv run python scripts/seed_tenant.py co-broker co-broker production

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

- **co-index**: notifier's `deploy/index/index-checkin.sh`, redeployed to co-index.
- **co-watcher**: `/etc/watcher/backup.env` and the unit's `notifier-key` credential file on the watcher VM.
- **co-broker**: CannObserv/broker#66.

### 4. Confirm

```bash
curl -s -H "X-API-Key: $KEY" "http://status:9000/api/v1/monitors/<id>" | jq .last_checkin_at
```

Later than the import. If it never moves, the switch is broken and notifier's copy will say so — the right alarm.

### 5. Hand over

Within notifier's window since its last check-in (30 minutes for broker and index, 26 hours for watcher):

```bash
# co-status: enable (writes `resumed`)
curl -s -X PATCH -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"enabled": true}' "http://status:9000/api/v1/monitors/<id>"

# notifier: disable its copy, with the consumer's old notifier key
curl -s -X PATCH -H "X-API-Key: $OLD_NOTIFIER_KEY" -H 'Content-Type: application/json' \
  -d '{"enabled": false}' "http://notifier:9000/api/v1/monitors/<id>"
```

notifier's disabled row stays for 7 days as the fallback. Rolling back before step 5 is reverting the consumer.
