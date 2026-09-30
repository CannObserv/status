#!/usr/bin/env bash
# Guarded production launch. deploy/status.service ExecStart's this, never a
# uvicorn line of its own.
#
# Two reasons, the second specific to notifier#43:
#
#   * One spelling. status-dev.service has argued since notifier#24 that a second
#     uvicorn line is a second code path that drifts out from under the guards.
#   * systemd cannot hand us the address. The service binds this host's tailnet
#     address alone (notifier#43 D3), and systemd reads every EnvironmentFile= *before*
#     ExecStartPre runs — so no ExecStartPre can resolve the address into the
#     environment ExecStart sees. Resolving and binding must happen in one
#     process, which makes that process a script.
#
# Unlike dev_server.sh this does NOT source env files or touch DATABASE_URL:
# the unit supplies both, and production is the database it is supposed to
# open. STATUS_ALLOW_PROD_DB stays the unit's business.
set -euo pipefail

cd -P "$(dirname "${BASH_SOURCE[0]}")/.."

# Blocks until tailscaled has assigned an address, then fails loudly rather
# than falling back to a bind reachable from anywhere else (observo#473/#479).
HOST="$("$(dirname "${BASH_SOURCE[0]}")/tailnet_bind.sh")"

# --frozen --no-sync: run what scripts/deploy.sh built; never sync at start (#9, R5).
exec uv run --frozen --no-sync uvicorn src.api.main:app \
  --host "$HOST" \
  --port 9000 \
  --log-config src/core/log_config.json
