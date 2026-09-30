#!/usr/bin/env bash
# Guarded dev-server launch. Use this instead of hand-running uvicorn.
#
# The old recipe sourced /etc/status/.env — which sets DATABASE_URL to
# production — and then ran uvicorn on 9001, so the "dev" server shared one
# database with the live service on 9000 (issue notifier#22, root incident
# CannObserv/archiver#98).
#
# This script loads secrets the same way, then overrides DATABASE_URL with
# DEV_DATABASE_URL and hands the result to src.core.db_safety for the
# production check. The parsing lives in python only — bash never
# reimplements it, so the two cannot diverge.
set -euo pipefail

cd -P "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${STATUS_DEV_SERVER_SKIP_ENV_FILES:-}" != "1" ]]; then
  # Resolved from this script's own location, not the cwd — so it does not
  # silently depend on the cd above.
  # shellcheck disable=SC1091
  . "$(dirname "${BASH_SOURCE[0]}")/load_env.sh"
fi

# Never inherit the production opt-in; it belongs to the systemd unit alone.
unset STATUS_ALLOW_PROD_DB

if [[ -z "${DEV_DATABASE_URL:-}" ]]; then
  cat >&2 <<'MSG'
dev_server: DEV_DATABASE_URL is not set.

The dev server must not share the production database. Create a dev database
and point DEV_DATABASE_URL at it in the repo .env (git-ignored):

  createdb status_dev
  echo 'DEV_DATABASE_URL=postgresql+asyncpg://USER@localhost/status_dev' >> .env
  DATABASE_URL="$DEV_DATABASE_URL" uv run alembic upgrade head

The name must end in _test or _dev; see src/core/db_safety.py.
MSG
  exit 1
fi

export DATABASE_URL="$DEV_DATABASE_URL"

uv run --frozen --no-sync python -m src.core.db_safety

# An unmigrated dev database starts cleanly and then 500s on every
# authenticated request with "relation ... does not exist" (issue notifier#23),
# and one merely behind the code 500s on whatever touches the missing column
# (#9). src.core.schema_state is the check the sweep and /ready use too: it
# prints the state, and exits 1 when behind or unmigrated, 2 when unreachable.
schema_rc=0
schema="$(uv run --frozen --no-sync python -m src.core.schema_state)" || schema_rc=$?
case "$schema_rc" in
  0) ;;
  1)
    cat >&2 <<MSG
dev_server: the dev database is ${schema:-behind this code}. Migrate it first:

  DATABASE_URL="\$DEV_DATABASE_URL" uv run alembic upgrade head
MSG
    exit 1
    ;;
  *)
    echo "dev_server: cannot read migration state — is the dev database reachable?" >&2
    exit 1
    ;;
esac

# --reload is right for a hand-run server and wrong for a service. Under
# systemd an edit mid-request drops a consumer's connection, and a syntax
# error on main leaves the reloader wedged and *running* — so
# Restart=on-failure never fires and the endpoint is silently dead.
# deploy/status-dev.service sets STATUS_DEV_RELOAD=0 (issue notifier#24).
reload_args=()
if [[ "${STATUS_DEV_RELOAD:-1}" == "1" ]]; then
  reload_args=(--reload)
fi

# Same tailnet-only bind as production (notifier#43 D3): the dev endpoint carries real
# consumer traffic from watcher's non-production processes, so it has no more
# business on 0.0.0.0 than :9000 does.
HOST="$("$(dirname "${BASH_SOURCE[0]}")/tailnet_bind.sh")"

exec uv run --frozen --no-sync uvicorn src.api.main:app \
  --host "$HOST" \
  --port 9001 \
  "${reload_args[@]}" \
  --log-config src/core/log_config.json
