#!/usr/bin/env bash
# Deploy co-status: build a release from a pushed commit, migrate, switch,
# verify (#9; docs/specs/2026-09-30-deploy-releases-design.md, R1–R13).
#
#   scripts/deploy.sh [<ref>]            dev, then live; <ref> on origin/main
#   scripts/deploy.sh --dev [<ref>]      dev only; <ref> on any origin branch
#   scripts/deploy.sh --no-restart ...   first deploy only: build, migrate, link
#
# <ref> defaults to origin/main. A rollback is a deploy of the previous build.
#
# On 2026-09-29 the units ran the development checkout, and an unmigrated model
# edit crashed the production sweep for 40 minutes. Now each unit runs
# /srv/status/{live,dev}, a symlink into releases/<build>: a read-only
# `git archive` of one pushed commit, with its own venv. Nothing done in a
# checkout reaches a unit until this script puts it there.
#
# Per target, in this order, and dev first so it rehearses every migration:
#
#   1. migrate — skipped when the database is ahead of the release (a rollback;
#      migrations are expand-only, R7)
#   2. switch the symlink (rename(2), atomic)
#   3. restart the API; force one sweep pass (`systemctl start` on the oneshot
#      waits for it)
#   4. verify: the pass exited 0, /ready is 200, /health names this build
#   5. on failure, switch back, restart, and stop. The migration stays (R7).
#
# Runs as exedev, the units' user; sudo for systemctl only.
set -euo pipefail

ROOT="${STATUS_DEPLOY_ROOT:-/srv/status}"
ENV_DIR="${STATUS_DEPLOY_ENV_DIR:-/etc/status}"
KEEP="${STATUS_DEPLOY_KEEP:-5}"
VERIFY_SECONDS="${STATUS_DEPLOY_VERIFY_SECONDS:-60}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

note() { echo "deploy: $*" >&2; }
die() {
  note "$*"
  exit 1
}

usage() { sed -n '4,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

targets=(dev live)
restart=1
ref=""
while (($#)); do
  case "$1" in
    --dev) targets=(dev) ;;
    --no-restart) restart=0 ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*) die "unknown flag $1 (see --help)" ;;
    *)
      [[ -z "$ref" ]] || die "one ref at a time"
      ref="$1"
      ;;
  esac
  shift
done
ref="${ref:-origin/main}"

# Releases belong to exedev, the user every unit runs as. Built by root they
# would be root's, and the units could not read their own venvs.
[[ "$(id -u)" -eq 0 ]] && die "run as exedev, not root; the script sudoes for systemctl itself"

# A release has a copy of this script and no .git to build from (CR 11).
git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1 ||
  die "run from a checkout (/home/exedev/status/scripts/deploy.sh); $SRC is not one"

[[ -d "$ROOT" ]] || die "$ROOT does not exist. Once: sudo mkdir $ROOT && sudo chown exedev: $ROOT"

exec 9>"$ROOT/.deploy.lock"
flock -n 9 || die "another deploy is running (it holds $ROOT/.deploy.lock)"

# The sweeps resolve their link on every pass, so relinking a running target
# puts the build live within 60 s, unverified, whatever this flag says (CR 4).
if ((!restart)); then
  for target in "${targets[@]}"; do
    [[ ! -L "$ROOT/$target" ]] ||
      die "--no-restart is for the first deploy only: $target already runs $(readlink "$ROOT/$target")"
  done
fi

for target in "${targets[@]}"; do
  envfile="$ENV_DIR/$([[ "$target" == live ]] && echo .env || echo dev.env)"
  [[ -r "$envfile" ]] || die "$envfile is missing or unreadable; the $target units need it too"
done

# --- which commit ----------------------------------------------------------

git -C "$SRC" fetch --quiet --prune origin
sha="$(git -C "$SRC" rev-parse --verify --quiet "${ref}^{commit}")" || die "cannot resolve $ref"
if [[ " ${targets[*]} " == *" live "* ]]; then
  git -C "$SRC" merge-base --is-ancestor "$sha" origin/main ||
    die "$ref ($sha) is not on origin/main; only a pushed main commit goes live"
else
  [[ -n "$(git -C "$SRC" branch -r --contains "$sha")" ]] ||
    die "$ref ($sha) is on no origin branch; push it first"
fi
build="$(git -C "$SRC" rev-parse --short=12 "$sha")"
release="$ROOT/releases/$build"

# --- the release -----------------------------------------------------------

# Commands inside the release run exactly what was built (R5).
in_release() { (cd "$release" && uv run --frozen --no-sync "$@"); }

make_writable() { chmod -R u+w "$1"; }

build_release() {
  if [[ -e "$release" ]]; then
    note "removing an interrupted or broken build of $build"
    make_writable "$release"
    rm -rf "$release"
  fi
  note "building $build"
  mkdir -p "$release"
  git -C "$SRC" archive "$sha" | tar -x -C "$release"
  # Built where it will run: a uv venv embeds its absolute path in its
  # scripts, so one built elsewhere and moved would not start.
  (cd "$release" && uv sync --locked --no-dev --compile-bytecode --quiet) ||
    die "uv sync failed for $build; nothing switched"
  in_release python -m compileall -q src scripts alembic >/dev/null ||
    die "compileall failed for $build; nothing switched"
  local heads
  heads="$(in_release alembic heads | grep -c .)" || true
  [[ "$heads" == 1 ]] || die "$build has $heads Alembic heads, not 1; nothing switched"
  # REVISION last: a release without one is an interrupted build (R4).
  echo "$build" >"$release/REVISION"
  chmod -R a-w "$release"
}

# REVISION says the build finished, not that its venv still runs: a
# uv-managed interpreter removed since would fail every rollback to it (CR 9).
if [[ -f "$release/REVISION" ]] && in_release python -c 'import sys' 2>/dev/null; then
  note "reusing release $build"
else
  build_release
fi
touch "$release" # prune by last deploy, not first build

# --- each target -----------------------------------------------------------

database_url() {
  (
    # The file's value or nothing: after `. scripts/load_env.sh` the caller's
    # shell exports both, and the unit reads only the file (CR 5).
    unset DATABASE_URL DEV_DATABASE_URL
    set -a
    # shellcheck disable=SC1090
    . "$ENV_DIR/$([[ "$1" == live ]] && echo .env || echo dev.env)"
    if [[ "$1" == live ]]; then printf '%s' "${DATABASE_URL:-}"; else printf '%s' "${DEV_DATABASE_URL:-}"; fi
  )
}

# Runs in the release against one target's database. Only live carries the
# production opt-in (src/core/db_safety.py); dev never inherits it.
against() {
  local target="$1" url
  shift
  url="$(database_url "$target")"
  [[ -n "$url" ]] || die "no database URL for $target in $ENV_DIR"
  if [[ "$target" == live ]]; then
    DATABASE_URL="$url" STATUS_ALLOW_PROD_DB=1 in_release "$@"
  else
    (unset STATUS_ALLOW_PROD_DB && DATABASE_URL="$url" in_release "$@")
  fi
}

migrate() {
  local target="$1" state rc=0
  state="$(against "$target" python -m src.core.schema_state)" || rc=$?
  # Only a state the check actually printed. Anything else (db_safety refusing
  # the URL, a crash) exits 2 or 1 with no state, and must never become an
  # upgrade: alembic/env.py has no guard of its own (CR 1).
  case "$rc:$state" in
    0:current | 3:behind | 3:unmigrated) ;;
    0:ahead)
      note "$target: database is ahead of $build; not migrating. Expected for a rollback."
      note "Otherwise it holds a branch migration that was never merged: downgrade it" \
        "(docs/DEPLOYMENT.md § Branch migrations), or dev stops rehearsing migrations."
      return
      ;;
    *) die "$target: cannot read the schema state (exit $rc); nothing switched" ;;
  esac
  against "$target" alembic upgrade head || die "$target: migration failed; nothing switched"
}

swap() { # <link> <target>: rename(2) over the old link, so there is no moment without one
  ln -sfn "$2" "$1.new"
  mv -Tf "$1.new" "$1"
}

verify_http() { # <port> <build>: /ready 200 and /health naming <build>
  local port="$1" want="$2" host health="" deadline=$((SECONDS + VERIFY_SECONDS))
  host="$(STATUS_TAILNET_WAIT_SECONDS=10 "$SRC/scripts/tailnet_bind.sh" 2>/dev/null)" ||
    { note "no tailnet address to verify on"; return 1; }
  while ((SECONDS < deadline)); do
    if curl -fsS --max-time 5 "http://$host:$port/ready" >/dev/null 2>&1 &&
      health="$(curl -fsS --max-time 5 "http://$host:$port/health" 2>/dev/null)" &&
      [[ "$health" == *"\"build\":\"$want\""* ]]; then
      return 0
    fi
    sleep 1
  done
  note ":$port did not report ready on $want within ${VERIFY_SECONDS}s (last /health: ${health:-none})"
  return 1
}

# `systemctl start` on a oneshot mid-pass merges into that pass instead of
# running another, and a pass already running started on the old release. Wait
# it out, so the forced pass is one that started on this one (CR 2). Bounded
# past the units' TimeoutStartSec=120.
wait_for_idle_sweep() {
  local sweep="$1" deadline=$((SECONDS + 150))
  while [[ "$(systemctl show -p ActiveState --value "$sweep")" == activating ]]; do
    ((SECONDS < deadline)) || { note "$sweep has been running for 150s"; return 1; }
    sleep 1
  done
}

restart_and_verify() {
  local api="$1" sweep="$2" port="$3"
  sudo systemctl restart "$api" || { note "systemctl restart $api failed"; return 1; }
  wait_for_idle_sweep "$sweep" || return 1
  sudo systemctl start "$sweep" ||
    { note "the $sweep pass failed on $build: journalctl -u ${sweep%.service} -n 50"; return 1; }
  verify_http "$port" "$build"
}

deploy_target() {
  local target="$1" api sweep port link previous
  case "$target" in
    live) api=status sweep=status-sweep.service port=9000 ;;
    dev) api=status-dev sweep=status-sweep-dev.service port=9001 ;;
  esac
  link="$ROOT/$target"
  previous="$(readlink "$link" 2>/dev/null || true)"

  migrate "$target"
  swap "$link" "releases/$build"
  logger -t status-deploy "$target -> $build (was ${previous:-nothing})" || true

  if ((!restart)); then
    note "$target linked to $build; units not restarted (--no-restart)"
    return
  fi
  if restart_and_verify "$api" "$sweep" "$port"; then
    note "$target is on $build"
    return
  fi
  [[ -n "$previous" ]] || die "$target failed on $build, and there is no previous release to return to"
  swap "$link" "$previous"
  # A crash-looping release can exhaust the unit's StartLimitBurst, and systemd
  # then refuses this restart too. Clear it, and prove the old build answers
  # before saying it does (CR 3).
  sudo systemctl reset-failed "$api" || true
  sudo systemctl restart "$api" || true
  logger -t status-deploy "$target rolled back to ${previous#releases/} after $build failed" || true
  verify_http "$port" "${previous#releases/}" ||
    die "$target failed on $build; switched back to ${previous#releases/}, which is NOT answering: journalctl -u $api -n 50"
  die "$target failed on $build; switched back to ${previous#releases/}, which is answering"
}

for target in "${targets[@]}"; do
  deploy_target "$target"
done

# --- prune -----------------------------------------------------------------

linked=" $(readlink "$ROOT/live" 2>/dev/null || true) $(readlink "$ROOT/dev" 2>/dev/null || true) "
# shellcheck disable=SC2012 # names are 12 hex characters
ls -1t "$ROOT/releases" | tail -n +$((KEEP + 1)) | while read -r old; do
  [[ "$linked" == *" releases/$old "* ]] && continue
  note "pruning release $old"
  # Both targets are verified by now: a failed prune is a note, never a failed
  # deploy (CR 10).
  { make_writable "$ROOT/releases/$old" && rm -rf "${ROOT:?}/releases/$old"; } ||
    note "prune failed for $old; remove it by hand (chmod -R u+w first)"
done
