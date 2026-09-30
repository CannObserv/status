#!/usr/bin/env bash
# Guarded dead-man's-timer sweep. Both sweep units ExecStart this, never an
# inline `uv run` line — one spelling, for the same reason serve.sh and
# dev_server.sh exist (notifier#22, notifier#24, notifier#43).
#
# Two callers, one difference. The production unit supplies DATABASE_URL and
# STATUS_ALLOW_PROD_DB through its own EnvironmentFile/Environment lines, so
# this script leaves the environment alone. The dev unit sets
# STATUS_SWEEP_DEV=1, and then this script loads the env files and swaps in
# DEV_DATABASE_URL exactly the way dev_server.sh does — including dropping the
# production opt-in, which the dev timer must never inherit.
set -euo pipefail

cd -P "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${STATUS_SWEEP_DEV:-0}" == "1" ]]; then
  # Resolved from this script's own location, not the cwd — so it does not
  # silently depend on the cd above.
  # shellcheck disable=SC1091
  . "$(dirname "${BASH_SOURCE[0]}")/load_env.sh"

  # Never inherit the production opt-in; it belongs to the production unit.
  unset STATUS_ALLOW_PROD_DB

  if [[ -z "${DEV_DATABASE_URL:-}" ]]; then
    cat >&2 <<'MSG'
sweep: DEV_DATABASE_URL is not set.

The dev sweep must not open the production database — it would alert on
production monitors and dispatch to production channels to do it. Point
DEV_DATABASE_URL at the dev database in the repo .env (git-ignored):

  echo 'DEV_DATABASE_URL=postgresql+asyncpg://USER@localhost/status_dev' >> .env
MSG
    exit 1
  fi
  export DATABASE_URL="$DEV_DATABASE_URL"
fi

# The notifier key (spec D13). Without it the sweep finds the overdue monitors
# and tells nobody — an outage detected and reported to no one. Refuse where
# the unit's status will show it. systemd puts the credential here; a
# hand-run sweep has no $CREDENTIALS_DIRECTORY and is refused the same way.
if [[ ! -s "${CREDENTIALS_DIRECTORY:-/nonexistent}/notifier-key" ]]; then
  echo "sweep: no notifier-key credential — every missing alert would go nowhere" >&2
  exit 1
fi

# The URL check lives in python only; bash never reimplements the parsing.
# --frozen --no-sync: run what scripts/deploy.sh built. A bare `uv run` synced
# the environment every pass, so a lock edit reached production (#9, R5).
uv run --frozen --no-sync python -m src.core.db_safety

exec uv run --frozen --no-sync python scripts/sweep_monitors.py
