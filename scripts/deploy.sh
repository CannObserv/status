#!/usr/bin/env bash
# Deploy co-status: build a release from a pushed commit, migrate, switch,
# verify (#9; docs/specs/2026-09-30-deploy-releases-design.md, R1–R13).
#
#   scripts/deploy.sh [<ref>]            dev, then live; <ref> on origin/main, CI green
#   scripts/deploy.sh --dev [<ref>]      dev only; <ref> on any origin branch
#   scripts/deploy.sh --skip-ci [<ref>]  live without asking CI: an emergency, logged
#   scripts/deploy.sh --no-restart ...   first deploy only: build, migrate, link
#
# <ref> defaults to origin/main. A rollback is a deploy of the previous build.
#
# Live only once the commit's CI passed: its push run on main and every job in
# it green, lint, test and migrations among them, asked of GitHub before anything
# is built (#11; docs/DEPLOYMENT.md § The CI gate). --dev alone is never gated.
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
#   2. switch the symlink (rename(2), atomic), then install the target's units
#      from the release, those that differ from their installed copies (#18)
#   3. restart the API; force one sweep pass (`systemctl start` on the oneshot
#      waits for it)
#   4. verify: the pass exited 0, /ready is 200, /health names this build
#   5. on failure, switch back, units too, restart, and stop. The migration
#      stays (R7).
#
# After a live deploy, the host configs under deploy/ (sysctl, earlyoom, the
# slice drop-ins, needrestart) are compared with their installed copies; a
# difference is a warning, never installed (#18).
#
# Runs as exedev, the units' user, and builds as exedev. Root owns the deploy
# root, releases/ and every finished release, so the links too (#14): sudo for
# every write under the root, for systemctl, and for unit files in
# /etc/systemd/system. exedev can still sudo anything; what root ownership buys
# is that changing what a unit runs takes sudo, which journals the command
# (a root shell, only as a shell: docs/DEPLOYMENT.md). Exits 0
# when every target verified, 4 when a target is left on a build that did not
# answer (no rollback possible, or the old build failed too), 1 otherwise.
set -euo pipefail

ROOT="${STATUS_DEPLOY_ROOT:-/srv/status}"
ENV_DIR="${STATUS_DEPLOY_ENV_DIR:-/etc/status}"
KEEP="${STATUS_DEPLOY_KEEP:-5}"
VERIFY_SECONDS="${STATUS_DEPLOY_VERIFY_SECONDS:-60}"
# Past the sweep units' TimeoutStartSec=120: a pass still running after this is stuck.
SWEEP_WAIT_SECONDS="${STATUS_DEPLOY_SWEEP_WAIT_SECONDS:-150}"
# CI takes about 2 minutes; a run still going after this is wedged or queued behind one.
CI_WAIT_SECONDS="${STATUS_DEPLOY_CI_WAIT_SECONDS:-600}"
CI_POLL_SECONDS="${STATUS_DEPLOY_CI_POLL_SECONDS:-30}"
# The jobs a run must have, as tests/ci holds ci.yml to; every job a run lists
# must pass too, named here or not (CR 6).
CI_JOBS=(lint test migrations)
GITHUB_API="https://api.github.com/repos/CannObserv/status"
# /etc: the units go in systemd/system/, and the host configs are compared
# where the RUNBOOK installs them (#18).
ETC="${STATUS_DEPLOY_ETC:-/etc}"
UNIT_DIR="$ETC/systemd/system"
# deploy/<file>=<path under /etc>. tests/deploy holds every file under deploy/
# to being a unit or one of these, and the RUNBOOK to installing it there.
HOST_CONFIGS=(
  "99-status-memory.conf=sysctl.d/99-status-memory.conf"
  "earlyoom.default=default/earlyoom"
  "needrestart.conf.d/status.conf=needrestart/conf.d/status.conf"
  "system.slice.d/10-memory-protection.conf=systemd/system/system.slice.d/10-memory-protection.conf"
  "system-postgresql.slice.d/10-memory-protection.conf=systemd/system/system-postgresql.slice.d/10-memory-protection.conf"
  "postgresql@16-main.service.d/10-memory.conf=systemd/system/postgresql@16-main.service.d/10-memory.conf"
)
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

note() { echo "deploy: $*" >&2; }
die() {
  note "$*"
  exit 1
}
# Exit 4: the target is left on a build that did not answer (CR 16, CR 25).
dead() {
  note "$*"
  exit 4
}

usage() { sed -n '4,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

targets=(dev live)
restart=1
skip_ci=0
ref=""
while (($#)); do
  case "$1" in
    --dev) targets=(dev) ;;
    --no-restart) restart=0 ;;
    --skip-ci) skip_ci=1 ;;
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
live=0
[[ " ${targets[*]} " == *" live "* ]] && live=1
((live || !skip_ci)) || die "--skip-ci is for live; dev is never gated"

# exedev builds, and root only takes the finished release (#14). Built by
# root, uv would use root's cache and interpreters, which the units cannot read.
[[ "$(id -u)" -eq 0 ]] && die "run as exedev, not root; the script sudoes where it needs to"

# A release has a copy of this script and no .git to build from (CR 11).
git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1 ||
  die "run from a checkout (/home/exedev/status/scripts/deploy.sh); $SRC is not one"

[[ -d "$ROOT" ]] || die "$ROOT does not exist. Once: sudo install -d -m 755 $ROOT"

# Root's, and writable by root alone: a link is only as safe as its directory,
# and exedev could otherwise repoint live with ln (#14).
roots_alone() { # <dir>
  # A link's own mode is 777, so find would call it writable (CR 3).
  [[ ! -L "$1" ]] || die "$1 is a link; it must be the directory itself (#14)"
  [[ "$(stat -c %u -- "$1")" == 0 ]] ||
    die "$1 is not root's, so exedev can change what the units run (#14)." \
      "Once: sudo chown root:root $1 (docs/DEPLOYMENT.md § Who owns a release)"
  [[ -z "$(find "$1" -maxdepth 0 -perm /022)" ]] ||
    die "$1 is writable by more than root (#14). Once: sudo chmod 755 $1"
}
roots_alone "$ROOT"
# -L too: -e is false for a link to nowhere, which install -d cannot make (CR 12).
[[ ! -e "$ROOT/releases" && ! -L "$ROOT/releases" ]] || roots_alone "$ROOT/releases"

# The root directory itself: exedev cannot create a lock file in it.
exec 9<"$ROOT"
flock -n 9 || die "another deploy is running (it holds the lock on $ROOT)"

# The installed units each target replaces, kept for a switch back (#18).
backup="$(mktemp -d "${TMPDIR:-/tmp}/status-deploy.XXXXXX")"
# `|| :`: under set -e a failing command in the trap would replace the exit
# status, turning a verified deploy into 1 or a dead target's 4 into 1.
trap 'rm -rf "$backup" || :' EXIT

# The sweeps resolve their link on every pass, so relinking a running target
# puts the build live within 60 s, unverified, whatever this flag says (CR 4).
if ((!restart)); then
  for target in "${targets[@]}"; do
    [[ ! -L "$ROOT/$target" ]] ||
      die "--no-restart is for the first deploy only: $target already runs $(readlink "$ROOT/$target")." \
        "If no unit runs $ROOT yet (a first deploy that failed half way): sudo rm $ROOT/$target, and retry (CR 17)."
  done
fi

for target in "${targets[@]}"; do
  envfile="$ENV_DIR/$([[ "$target" == live ]] && echo .env || echo dev.env)"
  [[ -r "$envfile" ]] || die "$envfile is missing or unreadable; the $target units need it too"
done

# The units bind the tailnet address alone, so that is where verification asks.
# Resolved before anything switches: without it no verification could pass, and
# the rollback would call a serving old build NOT answering (CR 34).
host=""
if ((restart)); then
  host="$(STATUS_TAILNET_WAIT_SECONDS=10 "$SRC/scripts/tailnet_bind.sh" 2>/dev/null)" ||
    die "no tailnet address to verify on (systemctl status tailscaled); nothing switched"
fi

# --- which commit ----------------------------------------------------------

git -C "$SRC" fetch --quiet --prune origin
sha="$(git -C "$SRC" rev-parse --verify --quiet "${ref}^{commit}")" || die "cannot resolve $ref"
if ((live)); then
  git -C "$SRC" merge-base --is-ancestor "$sha" origin/main ||
    die "$ref ($sha) is not on origin/main; only a pushed main commit goes live"
else
  [[ -n "$(git -C "$SRC" branch -r --contains "$sha")" ]] ||
    die "$ref ($sha) is on no origin branch; push it first"
fi
build="$(git -C "$SRC" rev-parse --short=12 "$sha")"
release="$ROOT/releases/$build"

# --- CI (#11) --------------------------------------------------------------

# Unauthenticated: the repo is public, and 60 requests an hour per address
# covers a deploy's 2 (22 waiting the full 600 s). No token, so none to store.
github() { # <path>: GitHub's answer as a JSON object, or a refusal, with GitHub's message when it sent one
  local out
  if out="$(curl -sS --fail-with-body --max-time 10 \
    -H 'Accept: application/vnd.github+json' "$GITHUB_API/$1")"; then
    # A JSON object, or refused: jq reads an empty body as no input at all,
    # prints nothing and exits 0, so an empty jobs answer listed no failed job
    # and passed (#21).
    jq -e 'type == "object"' <<<"$out" >/dev/null 2>&1 ||
      die "GitHub's answer about $build's CI is not the JSON expected; nothing was built." \
        "Deploy again later, or pass --skip-ci."
    printf '%s\n' "$out"
    return
  fi
  out="$(jq -r '.message // empty' <<<"$out" 2>/dev/null)" || out=""
  die "GitHub did not answer about $build's CI.${out:+ GitHub says: $out}" \
    "Nothing was built; deploy again later, or pass --skip-ci."
}

# The run that decides: the newest push run of ci.yml on main for exactly this
# commit. A commit can have several: 291604b also has a workflow_dispatch run on
# its branch, and check runs cannot tell the two apart. A re-run counts, since
# a run reports its latest attempt.
push_run() { # the run as JSON, or nothing
  jq -c --arg sha "$sha" '[.workflow_runs[]
    | select(.head_sha == $sha and .event == "push" and .head_branch == "main")]
    | max_by(.created_at) // empty' ||
    die "GitHub's answer about $build's CI runs is not the JSON expected; nothing was built." \
      "Deploy again later, or pass --skip-ci."
}

# Waits for the run, bounded. A commit behind origin/main's tip with no run is
# refused at once: GitHub runs CI on the newest commit of each push only, so it
# will not get one, unless it was pushed seconds ago with another push after it.
finished_run() {
  local deadline=$((SECONDS + CI_WAIT_SECONDS)) tip run state url left
  tip="$(git -C "$SRC" rev-parse origin/main)"
  while :; do
    run="$(github "actions/workflows/ci.yml/runs?head_sha=$sha&event=push&branch=main&per_page=100" | push_run)" ||
      exit 1
    left=$((deadline - SECONDS))
    if [[ -z "$run" && "$sha" != "$tip" ]]; then
      die "no CI run for $build as a push to main. GitHub runs CI on the newest commit of each push" \
        "only: deploy that one, or pass --skip-ci. Pushed in the last minute, with another push" \
        "after it? Its run may not be listed yet: deploy again shortly. Nothing was built."
    elif [[ -z "$run" ]]; then
      state="not queued yet" url=""
      ((left > 0)) ||
        die "no CI run for $build after ${CI_WAIT_SECONDS}s ([skip ci]?). Pass --skip-ci to deploy it" \
          "anyway. Nothing was built."
    else
      state="$(jq -r .status <<<"$run")"
      url="$(jq -r .html_url <<<"$run")"
      [[ "$state" == completed ]] && { printf '%s\n' "$run"; return; }
      ((left > 0)) ||
        die "CI for $build is still $state after ${CI_WAIT_SECONDS}s. Nothing was built; deploy again" \
          "when it finishes. Run: $url"
    fi
    note "waiting for CI on $build ($state)${url:+: $url}"
    sleep $((left < CI_POLL_SECONDS ? left : CI_POLL_SECONDS))
  done
}

# Success only, from the run itself, every job it lists, and at least CI_JOBS.
# A skipped job leaves the run "success", so the jobs are read too. CI_JOBS is
# only the floor: it comes from this checkout, which may be older than ci.yml,
# and a job it does not name still counts (CR 6).
ci_gate() {
  local run url conclusion jobs problems
  run="$(finished_run)" || exit 1
  url="$(jq -r .html_url <<<"$run")"
  conclusion="$(jq -r '.conclusion // "nothing"' <<<"$run")"
  jobs="$(github "actions/runs/$(jq -r .id <<<"$run")/jobs?per_page=100")" || exit 1
  problems="$(jq -r --arg required "${CI_JOBS[*]}" '[
      (.jobs[] | select(.conclusion != "success") | "\(.name) (\(.conclusion // .status))"),
      (($required | split(" "))[] as $name | select(any(.jobs[]; .name == $name) | not)
        | "\($name) (not in the run)")
    ] | join(", ")' <<<"$jobs")" ||
    die "GitHub's answer about $build's CI jobs is not the JSON expected; nothing was built." \
      "Deploy again later, or pass --skip-ci."
  [[ "$conclusion" == success ]] || problems="run concluded $conclusion${problems:+; $problems}"
  # Cancelled is not a verdict: a newer push, or a dispatch on main sharing
  # ci.yml's concurrency group, cancels a queued run with nothing to fix (CR 12).
  local remedy="fix it on main, or put it on dev alone with --dev"
  [[ "$conclusion" == cancelled ]] && remedy="re-run it from its page (a re-run counts), or deploy a newer commit"
  [[ -z "$problems" ]] || die "CI did not pass for $build: $problems. Nothing was built; $remedy. Run: $url"
  note "CI passed for $build: $url"
  logger -t status-deploy "live: CI passed for $build ($url)" || true
}

if ((live && skip_ci)); then
  note "live: not asking CI about $build (--skip-ci)"
  logger -t status-deploy "live: CI not checked for $build (--skip-ci)" || true
elif ((live)); then
  ci_gate
fi

# --- the release -----------------------------------------------------------

# Commands inside the release run exactly what was built (R5).
in_release() { (cd "$release" && uv run --frozen --no-sync "$@"); }

build_release() {
  [[ ! -e "$release" ]] || sudo rm -rf "$release"
  note "building $build"
  [[ -d "$ROOT/releases" ]] || sudo install -d -m 755 "$ROOT/releases" ||
    die "cannot make $ROOT/releases; nothing switched"
  # Built where it will run, by exedev: a uv venv embeds its absolute path in
  # its scripts, so one built elsewhere and moved would not start.
  sudo install -d -m 755 -o "$(id -un)" -g "$(id -gn)" "$release" ||
    die "cannot make releases/$build for $(id -un) to build in; nothing switched"
  git -C "$SRC" archive "$sha" | tar -x -C "$release"
  # Copied, not hardlinked to the uv cache: the chown below would reach the
  # cache's inodes, and an edit in a release would edit every venv sharing the
  # file (#14).
  (cd "$release" && uv sync --locked --no-dev --compile-bytecode --link-mode copy --quiet) ||
    die "uv sync failed for $build; nothing switched"
  in_release python -m compileall -q src scripts alembic >/dev/null ||
    die "compileall failed for $build; nothing switched"
  local heads
  heads="$(in_release alembic heads | grep -c .)" || true
  [[ "$heads" == 1 ]] || die "$build has $heads Alembic heads, not 1; nothing switched"
  chmod -R a-w "$release" || die "cannot make $build read-only; nothing switched"
  sudo chown -R root:root "$release" || die "cannot hand $build to root; nothing switched"
  # REVISION last, by root: a release without one is an interrupted build (R4),
  # the chown included.
  { echo "$build" | sudo tee "$release/REVISION" >/dev/null && sudo chmod 444 "$release/REVISION"; } ||
    die "cannot write $build's REVISION; nothing switched"
}

# The build a target's link names, by its last component: a link made by hand
# during recovery may be absolute, and every comparison must still see it (CR 28).
release_of() {
  local link
  link="$(readlink "$ROOT/$1" 2>/dev/null)" || return 0
  basename "$link"
}

linked_by() { # the targets whose link names this release
  local target
  for target in live dev; do
    [[ "$(release_of "$target")" == "$build" ]] && echo "$target"
  done
  return 0
}

# REVISION says the build finished, not that its venv still runs: a
# uv-managed interpreter removed since would fail every rollback to it (CR 9).
# A release a target runs is never rebuilt in place: that pulls the code out
# from under its sweep and API, unverified, and a failed sync leaves the target
# with no release at all (CR 15).
# Why this release cannot be reused as it stands; nothing when it can.
#   - No REVISION: an interrupted build (R4), or half-pruned (CR 26).
#   - Not root's: built before #14, or handed back by hand, so exedev may have
#     changed it. REVISION follows the chmod and the chown, so this also covers
#     CR 26's release cut short before its chmod, which uv would quietly
#     rebuild empty rather than fail the probe.
#   - The probe fails: its venv no longer runs, say a uv-managed interpreter
#     since removed (CR 9). It imports dependencies, as `import sys` passes on
#     an empty venv.
unusable() {
  local out
  if [[ ! -e "$release" ]]; then
    echo "not built"
  elif [[ ! -f "$release/REVISION" ]]; then
    echo "an interrupted build"
  elif [[ "$(stat -c %u -- "$release")" != 0 ]]; then
    echo "not root's (#14)"
  elif ! out="$(in_release python -c 'import fastapi, sqlalchemy, alembic' 2>&1)"; then
    echo "a venv that no longer runs (${out:-no output})"
  fi
}

# A release a target runs is never rebuilt in place, whatever is wrong with it:
# that pulls the code out from under its sweep and API, unverified, and a
# failed sync leaves the target with no release at all (CR 15, CR 27).
why="$(unusable)"
if [[ -z "$why" ]]; then
  note "reusing release $build"
else
  running="$(linked_by | paste -sd' ')"
  # Unless it is gone altogether: nothing runs from a missing directory, and
  # building it is the repair (CR 32).
  [[ -z "$running" || "$why" == "not built" ]] ||
    die "release $build is $why, and $running runs it." \
      "Deploy another build to $running first; this one is then rebuilt."
  [[ "$why" == "not built" ]] || note "release $build is $why; rebuilding"
  build_release
fi
sudo touch "$release" # prune by last deploy, not first build

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
  # upgrade (CR 1). alembic/env.py now refuses what db_safety refuses (#15),
  # but a crash says nothing about the schema.
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

# What the tree a link names reports as its build: its REVISION, else "dev",
# the rule src/core/build.py applies. A link restored by hand may name a
# checkout, which answers "dev", not its directory's name (CR 36).
served_build() {
  local dir rev
  dir="$(cd "$ROOT" && cd -P "$1" 2>/dev/null && pwd)" || { echo dev; return 0; }
  rev="$(cat "$dir/REVISION" 2>/dev/null)" || rev=""
  echo "${rev:-dev}"
}

swap() { # <link> <target>: rename(2) over the old link, so there is no moment without one
  sudo ln -sfn "$2" "$1.new"
  sudo mv -Tf "$1.new" "$1"
}

verify_http() { # <port> <build>: /ready 200 and /health naming <build>
  local port="$1" want="$2" health="" deadline=$((SECONDS + VERIFY_SECONDS))
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
# it out, so the forced pass is one that started on this one (CR 2).
wait_for_idle_sweep() {
  local sweep="$1" deadline=$((SECONDS + SWEEP_WAIT_SECONDS))
  while [[ "$(systemctl show -p ActiveState --value "$sweep")" == activating ]]; do
    ((SECONDS < deadline)) || { note "$sweep has been running for ${SWEEP_WAIT_SECONDS}s"; return 1; }
    sleep 1
  done
}

# --- units (#18) -------------------------------------------------------------

# A target's units in the release, by name: <name>-dev.{service,timer} are
# dev's, every other deploy/*.{service,timer} is live's. tests/deploy holds the
# repo to the rule: a unit's release root is /srv/status/dev exactly when its
# name ends -dev.
units_of() { # <target>
  local path name
  for path in "$release"/deploy/*.service "$release"/deploy/*.timer; do
    [[ -f "$path" ]] || continue
    name="$(basename "$path")"
    if [[ "${name%.*}" == *-dev ]]; then
      [[ "$1" == dev ]] || continue
    else
      [[ "$1" == live ]] || continue
    fi
    echo "$name"
  done
  return 0
}

# Installs the target's units that differ from their installed copies, after
# copying each copy aside for restore_units. One daemon-reload, and a changed
# timer is restarted so it re-arms on its new schedule. Units the release
# lacks stay as they are; new ones are installed, never enabled: enabling is a
# decision (#12 needed its healthchecks.io check first).
# Called as `install_units || ...`, so set -e is off in here: every step that
# can fail says so itself.
install_units() { # <target>
  local target="$1" name unit changed=() added=() timers=()
  { mkdir -p "$backup/$target" && : >"$backup/$target.added"; } ||
    { note "$target: cannot keep the installed units aside in $backup"; return 1; }
  while read -r name; do
    unit="$UNIT_DIR/$name"
    cmp -s "$release/deploy/$name" "$unit" && continue
    if [[ -e "$unit" ]]; then
      cp "$unit" "$backup/$target/$name" ||
        { note "$target: cannot keep $unit aside; not installing it"; return 1; }
      changed+=("$name")
    else
      echo "$name" >>"$backup/$target.added"
      added+=("$name")
    fi
    [[ "$name" != *.timer ]] || timers+=("$name")
    sudo install -m 644 "$release/deploy/$name" "$unit" ||
      { note "$target: installing $name in $UNIT_DIR failed"; return 1; }
  done < <(units_of "$target")
  ((${#changed[@]} + ${#added[@]})) || return 0
  sudo systemctl daemon-reload || { note "$target: systemctl daemon-reload failed"; return 1; }
  # A timer that will not restart is a schedule that no longer runs, and
  # status-sweep.timer is the only thing watching for silence: fail the target,
  # never verify past it (CR 1).
  for name in "${timers[@]}"; do
    sudo systemctl try-restart "$name" ||
      { note "$target: systemctl try-restart $name failed: systemctl status $name"; return 1; }
  done
  local list="${changed[*]}"
  for name in "${added[@]}"; do list+=" $name (new)"; done
  note "$target units installed from $build: ${list# }"
  logger -t status-deploy "$target units from $build: ${list# }" || true
  for name in "${added[@]}"; do
    note "$name is new here and not enabled. If it should run: sudo systemctl enable --now $name"
  done
}

# Whether install_units replaced or added any of the target's units.
units_replaced() { # <target>
  compgen -G "$backup/$1/*" >/dev/null || [[ -s "$backup/$1.added" ]]
}

# Switch back, for units: the copies install_units replaced go back, and what
# it added is removed. A failure is a note: the rollback still proves the old
# build, and says whether it answers.
restore_units() { # <target>
  local target="$1" path name timers=() any=0
  for path in "$backup/$target"/*; do
    [[ -f "$path" ]] || continue
    name="$(basename "$path")"
    sudo install -m 644 "$path" "$UNIT_DIR/$name" || note "$target: restoring $name failed: $path"
    [[ "$name" != *.timer ]] || timers+=("$name")
    any=1
  done
  # Missing when install_units could not even start; set -e is on here, and a
  # failed redirect would end the rollback before it proves the old build (CR 4).
  if [[ -f "$backup/$target.added" ]]; then
    while read -r name; do
      sudo rm -f "$UNIT_DIR/$name" || note "$target: removing $UNIT_DIR/$name failed"
      any=1
    done <"$backup/$target.added"
  fi
  ((any)) || return 0
  sudo systemctl daemon-reload || note "$target: systemctl daemon-reload failed"
  for name in "${timers[@]}"; do
    sudo systemctl try-restart "$name" || note "$target: systemctl try-restart $name failed"
  done
  logger -t status-deploy "$target units restored" || true
}

# Host configs change rarely, and installing one means sysctl -p, an earlyoom
# restart or a slice reload: by hand. A difference is a warning (#18).
compare_host_configs() {
  local entry rel dest
  for entry in "${HOST_CONFIGS[@]}"; do
    rel="${entry%%=*}" dest="$ETC/${entry#*=}"
    [[ -f "$release/deploy/$rel" ]] || continue
    if [[ ! -e "$dest" ]]; then
      note "host config deploy/$rel is not installed at $dest; install it by hand (docs/RUNBOOK.md § First-time setup)"
    elif ! cmp -s "$release/deploy/$rel" "$dest"; then
      note "host config deploy/$rel differs from $dest; install it by hand (docs/RUNBOOK.md § First-time setup)"
    fi
  done
  return 0
}

restart_and_verify() {
  local api="$1" sweep="$2" port="$3"
  # The deploy that fixes a crash loop is the one that finds the unit past its
  # StartLimitBurst, and systemd refuses manual starts there too (CR 23).
  sudo systemctl reset-failed "$api" || true
  sudo systemctl restart "$api" || { note "systemctl restart $api failed"; return 1; }
  wait_for_idle_sweep "$sweep" || return 1
  sudo systemctl start "$sweep" ||
    { note "the $sweep pass failed on $build: journalctl -u ${sweep%.service} -n 50"; return 1; }
  verify_http "$port" "$build"
}

deploy_target() {
  local target="$1" api sweep port link previous previous_link
  case "$target" in
    live) api=status sweep=status-sweep.service port=9000 ;;
    dev) api=status-dev sweep=status-sweep-dev.service port=9001 ;;
  esac
  link="$ROOT/$target"
  # The link exactly as it was, to put back on failure: one made by hand during
  # a recovery may point anywhere, not only into releases/ (CR 31). Its name is
  # for comparing and for messages.
  previous_link="$(readlink "$link" 2>/dev/null || true)"
  previous="$(release_of "$target")"

  migrate "$target"
  swap "$link" "releases/$build"
  logger -t status-deploy "$target -> $build (was ${previous_link:-nothing})" || true
  local units_ok=1
  install_units "$target" || units_ok=0

  if ((!restart)); then
    # The copies kept aside go when the deploy does: put them back first (CR 3).
    ((units_ok)) || { restore_units "$target"; die "$target linked to $build, but its units" \
      "did not install (above); those it replaced are back. Fix that, rm $link, and retry (CR 17)."; }
    note "$target linked to $build; units not restarted (--no-restart)"
    return
  fi
  if ((units_ok)) && restart_and_verify "$api" "$sweep" "$port"; then
    note "$target is on $build"
    return
  fi
  [[ -n "$previous" ]] || dead "$target failed on $build, and there is no previous release to return to"
  local old back
  if [[ "$previous" == "$build" ]]; then
    # The link never moved, so the units are all that changed. Replacing a
    # hand-fixed unit is the likeliest way here, and its copy kept aside is
    # the only one left: put it back and prove the build on it (CR 10).
    units_replaced "$target" ||
      dead "$target failed on $build, which it was already running; there is nothing to switch back to"
    old="$build" back="put back the units it replaced, on $build"
  else
    old="$(served_build "$previous_link")" back="switched back to $old"
    swap "$link" "$previous_link"
  fi
  restore_units "$target"
  # A crash-looping release can exhaust the unit's StartLimitBurst, and systemd
  # then refuses this restart too. Clear it, then prove the old build as the new
  # one was proved: its API and a sweep pass, since a migration that was not
  # really expand-only breaks the old sweep before anything else (CR 3, CR 16).
  sudo systemctl reset-failed "$api" || true
  sudo systemctl restart "$api" || true
  if wait_for_idle_sweep "$sweep" && sudo systemctl start "$sweep" && verify_http "$port" "$old"; then
    logger -t status-deploy "$target rolled back to $old after $build failed ($back)" || true
    die "$target failed on $build; $back, which is answering"
  fi
  logger -t status-deploy "$target failed on $build; $back, which is NOT answering" || true
  dead "$target failed on $build; $back, which is NOT answering:" \
    "journalctl -u $api -u ${sweep%.service} -n 50"
}

for target in "${targets[@]}"; do
  deploy_target "$target"
done
if ((live)); then
  compare_host_configs
fi

# --- prune -----------------------------------------------------------------

linked=" $(release_of live) $(release_of dev) "
# shellcheck disable=SC2012 # names are 12 hex characters
ls -1t "$ROOT/releases" | tail -n +$((KEEP + 1)) | while read -r old; do
  [[ "$linked" == *" $old "* ]] && continue
  note "pruning release $old"
  # Both targets are verified by now: a failed prune is a note, never a failed
  # deploy (CR 10).
  # REVISION first: a prune cut short leaves an interrupted build, never a
  # "complete" one with half its files (CR 26).
  { sudo rm -f "$ROOT/releases/$old/REVISION" && sudo rm -rf "${ROOT:?}/releases/$old"; } ||
    note "prune failed for $old; remove it by hand: sudo rm -rf $ROOT/releases/$old"
done
