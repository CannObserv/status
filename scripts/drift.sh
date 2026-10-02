#!/usr/bin/env bash
# The hourly drift check (#12): does live lag origin/main in code that runs?
# status-drift.service ExecStarts this, never an inline `uv run` line, for the
# reason sweep.sh gives: one spelling.
#
# No database and no notifier key: GitHub and healthchecks.io only. The ping
# key reaches it as a credential, and scripts/check_drift.py runs without one.
set -euo pipefail

cd -P "$(dirname "${BASH_SOURCE[0]}")/.."

# --frozen --no-sync: run what scripts/deploy.sh built (#9, R5).
exec uv run --frozen --no-sync python scripts/check_drift.py
