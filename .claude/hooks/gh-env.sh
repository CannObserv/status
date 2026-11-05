#!/usr/bin/env bash
# SessionStart: export GH_TOKEN from .env to the session's Bash calls (#92).
# Ported from CannObserv/processor#115 (9ff80046); retire with gregoryfoster/skills#576.
#
# A local replica of gregoryfoster/skills#334's using-github-cli hook, until it
# ships; then this file becomes the vendored symlink. It:
#   - appends `export GH_TOKEN=<%q>` to $CLAUDE_ENV_FILE, read as data from the
#     primary checkout's .env by scripts/env-value.sh (never executed);
#   - leaves a GH_TOKEN that is already set alone;
#   - says so when .env has no GH_TOKEN, or when CLAUDE_ENV_FILE is unset
#     (anthropics/claude-code#15840), with the manual export;
#   - warns when gh is missing or older than 2.78.0 (cli/cli#11514).
# Only GH_TOKEN: cross-repo GH_TOKEN_<REPO> stay per call. It never prints the
# token and always exits 0. Other SessionStart hooks run in parallel and never
# see this export: the refresh hook reads the token in its own registration.
set -uo pipefail
trap 'exit 0' EXIT

root=${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}
# shellcheck disable=SC2016 # literal on purpose: the command to type, not its value
manual='export GH_TOKEN="$(bash scripts/env-value.sh GH_TOKEN)"'
FLOOR=2.78.0

gh_bin=${GH_ENV_GH:-gh}
if ! command -v -- "$gh_bin" >/dev/null 2>&1; then
  echo "gh-env: gh not found on PATH; install gh >= $FLOOR from GitHub's apt repository"
else
  have=$("$gh_bin" --version 2>/dev/null | sed -n '1s/^gh version \([0-9][0-9.]*\).*/\1/p')
  if [ -n "$have" ] && [ "$(printf '%s\n%s\n' "$have" "$FLOOR" | sort -V | head -n1)" != "$FLOOR" ]; then
    echo "gh-env: gh $have is older than $FLOOR (cli/cli#11514): gh issue/pr view can fail; use gh api"
  fi
fi

if [ -n "${GH_TOKEN:-}" ]; then
  exit 0
fi
if ! token=$(bash "$root/scripts/env-value.sh" GH_TOKEN); then
  echo "gh-env: no GH_TOKEN in the primary checkout's .env; gh calls will be unauthenticated"
  exit 0
fi
if [ -z "${CLAUDE_ENV_FILE:-}" ]; then
  echo "gh-env: CLAUDE_ENV_FILE is unset, so GH_TOKEN is not exported; per call: $manual"
  exit 0
fi
printf 'export GH_TOKEN=%q\n' "$token" >>"$CLAUDE_ENV_FILE" ||
  echo "gh-env: could not write CLAUDE_ENV_FILE; per call: $manual"
