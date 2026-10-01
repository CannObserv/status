# Source this; do not execute it:  . scripts/load_env.sh
#
# Loads /etc/status/.env (production secrets) then the repo .env (dev/agent
# secrets), exporting every assignment. Both are optional, so a fresh clone
# works. Sourcing preserves values containing spaces or quotes, which
# word-splitting an env file through xargs corrupts.
#
# Resolves the repo .env from this script's own location, so it works when
# sourced from a subdirectory such as tests/.
#
# NOTE: this leaves DATABASE_URL pointing at PRODUCTION. src/core/db_safety.py
# refuses it to anything run without STATUS_ALLOW_PROD_DB=1, which a
# deliberate operation sets on its own command line (docs/RUNBOOK.md). Never
# for a migration: scripts/deploy.sh migrates production, and alembic is
# refused here too (#15). Use scripts/dev_server.sh for a server; pytest pins
# its own.
# Executing this instead of sourcing it exports into a subshell that exits
# immediately, leaving the caller with nothing and no error. Fail loudly.
# ${BASH_SOURCE[0]} equals $0 only when the script is executed, never sourced.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  echo "load_env.sh must be sourced, not executed:  . scripts/load_env.sh" >&2
  exit 1
fi

_status_env_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
[ -r /etc/status/.env ] && . /etc/status/.env
[ -r "$_status_env_root/.env" ] && . "$_status_env_root/.env"
set +a
unset _status_env_root
