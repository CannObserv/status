#!/usr/bin/env bash
# Print one variable's value from an env file, read as data, never executed (#92).
# Ported from CannObserv/processor#115 (9ff80046); retire with gregoryfoster/skills#576.
#
#   scripts/env-value.sh NAME [FILE]
#
# FILE defaults to the primary checkout's .env (the first `git worktree list`
# entry): .env is git-ignored, so a linked worktree has none of its own.
#
# `set -a; . .env` runs the file. This reads `NAME=value` and `export NAME=value`
# lines, strips one pair of matching surrounding quotes and a trailing CR, and the
# last match wins. Nothing is expanded. Exit 1, printing nothing, when the file or
# the name is absent. Follows gregoryfoster/skills#334's parse-as-data rule for one
# name, not its loader (system env file, then .env); retired when that ships.
set -euo pipefail

name=${1:?usage: env-value.sh NAME [FILE]}
file=${2:-}
if [ -z "$file" ]; then
  root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  primary=$(git -C "$root" worktree list --porcelain 2>/dev/null | head -n1) || primary=""
  primary=${primary#worktree }
  file="${primary:-$root}/.env"
fi
[ -f "$file" ] && [ -r "$file" ] || exit 1

found=0
value=""
while IFS= read -r line || [ -n "$line" ]; do
  line=${line%$'\r'}
  line=${line#export }
  case $line in
    "$name="*) value=${line#"$name="} found=1 ;;
  esac
done <"$file"
[ "$found" -eq 1 ] || exit 1

case $value in
  \"*\") value=${value#\"} value=${value%\"} ;;
  \'*\') value=${value#\'} value=${value%\'} ;;
esac
printf %s "$value"
