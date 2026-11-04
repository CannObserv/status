#!/usr/bin/env bash
# Node.js from NodeSource's apt repo: agent tooling only (SocratiCode, using-mayfly-chat);
# no co-status unit runs it. docs/DEPLOYMENT.md -> Node.js; #76. Copied from
# CannObserv/processor's deploy/nodesource.sh (processor#6) at fd2b896: here the script is in
# scripts/, since deploy/ holds only units and deploy.sh's HOST_CONFIGS.
#
# Usage:
#   sudo bash scripts/nodesource.sh install   # key (fingerprint-checked), source, pin, nodejs
#   bash scripts/nodesource.sh check          # the host matches deploy/apt/ and the pinned line
#
# Exit codes:
#   0  ok
#   1  install refused: the downloaded key is not NodeSource's
#   2  usage or tooling failure
#   3  check found drift (a missing or differing file, key, candidate or node)
set -euo pipefail

FINGERPRINT="6F71F525282841EEDAF851B42F59B5F99B1BE0B4"
KEY_URL="https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key"
KEYRING="/etc/apt/keyrings/nodesource.gpg"
SOURCES="/etc/apt/sources.list.d/nodesource.sources"
PREF="/etc/apt/preferences.d/nodesource.pref"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SOURCES="$HERE/../deploy/apt/nodesource.sources"
REPO_PREF="$HERE/../deploy/apt/nodesource.pref"

# The header's Usage block, found by pattern, so editing the header above it can't shift it.
usage() { sed -n '/^# Usage:$/,/^#$/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }

# The major line, from the versioned source's URIs (node_<N>.x): one place to bump it.
major() {
  sed -n 's#^URIs: https://deb\.nodesource\.com/node_\([0-9][0-9]*\)\.x$#\1#p' "$REPO_SOURCES"
}

# Primary-key fingerprints in a key file, armored or binary, one per line. A throwaway
# GNUPGHOME, so nothing lands in the caller's keyring.
fingerprints() {
  local home rc=0
  home="$(mktemp -d)"
  GNUPGHOME="$home" gpg --batch --quiet --show-keys --with-colons "$1" 2>/dev/null |
    awk -F: '$1 == "pub" { want = 1 } $1 == "fpr" && want { print $10; want = 0 }' || rc=$?
  rm -rf "$home"
  return "$rc"
}

install_nodesource() {
  [ "$(id -u)" -eq 0 ] || { echo "install needs root: sudo bash $0 install" >&2; exit 2; }
  local tmp got
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064 # expanded now: $tmp is local, and gone by the time EXIT fires
  trap "rm -rf '$tmp'" EXIT

  curl -fsSL "$KEY_URL" -o "$tmp/key.asc" || { echo "could not fetch $KEY_URL" >&2; exit 2; }
  # `|| true`: a download that is no key at all is a refusal, not a silent set -e exit.
  got="$(fingerprints "$tmp/key.asc" || true)"
  if [ "$got" != "$FINGERPRINT" ]; then
    echo "REFUSED: $KEY_URL is not NodeSource's key" >&2
    echo "  expected $FINGERPRINT" >&2
    echo "  got      ${got:-<no key>}" >&2
    exit 1
  fi
  GNUPGHOME="$tmp" gpg --batch --yes --dearmor -o "$tmp/nodesource.gpg" "$tmp/key.asc"

  install -d -m 0755 /etc/apt/keyrings
  install -m 0644 "$tmp/nodesource.gpg" "$KEYRING"
  install -m 0644 "$REPO_SOURCES" "$SOURCES"
  install -m 0644 "$REPO_PREF" "$PREF"

  # As patching-hosts' run.md applies: needrestart mode l lists what would restart and
  # restarts nothing; choom -n 0 so a session started at -1000 (some exe-init builds) can't
  # make dpkg unkillable beside the production service.
  choom -n 0 -- apt-get update -qq || { echo "apt-get update failed" >&2; exit 2; }
  NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive choom -n 0 -- apt-get install -y -qq nodejs ||
    { echo "apt-get install nodejs failed" >&2; exit 2; }
  check_nodesource
}

check_nodesource() {
  local rc=0 want cand madison installed node_path version
  drift() { echo "DRIFT: $*" >&2; rc=3; }
  want="$(major)"
  [ -n "$want" ] || { echo "no node_<N>.x line in $REPO_SOURCES" >&2; exit 2; }

  if [ -f "$KEYRING" ]; then
    [ "$(fingerprints "$KEYRING")" = "$FINGERPRINT" ] || drift "$KEYRING is not NodeSource's key"
  else
    drift "no keyring at $KEYRING"
  fi
  cmp -s "$REPO_SOURCES" "$SOURCES" || drift "$SOURCES differs from deploy/apt/ (or is absent)"
  cmp -s "$REPO_PREF" "$PREF" || drift "$PREF differs from deploy/apt/ (or is absent)"

  cand="$(apt-cache policy nodejs 2>/dev/null | awk '/Candidate:/ { print $2 }')"
  case "$cand" in
    "$want".*) ;;
    *) drift "nodejs candidate is '${cand:-none}', not $want.x (apt-get update?)" ;;
  esac
  # One awk over captured output: under pipefail, `| grep -q` can SIGPIPE its writer and
  # turn a match into a failure.
  madison="$(apt-cache madison nodejs 2>/dev/null || true)"
  if [ -n "$cand" ] && ! awk -F' [|] ' -v v="$cand" \
    '$2 == v && $3 ~ /deb[.]nodesource[.]com/ { found = 1 } END { exit !found }' <<<"$madison"; then
    drift "nodejs candidate $cand is not from deb.nodesource.com"
  fi

  # Resolved, not compared as text: on merged-/usr Ubuntu a PATH with /bin ahead of
  # /usr/bin finds the same file as /bin/node.
  node_path="$(command -v node || true)"
  if [ -z "$node_path" ] || [ "$(readlink -f "$node_path")" != /usr/bin/node ]; then
    drift "node resolves to '${node_path:-nothing}', not the package's /usr/bin/node"
  elif [[ "$(dpkg -S /usr/bin/node 2>/dev/null)" != nodejs:* ]]; then
    drift "/usr/bin/node does not belong to the nodejs package"
  else
    version="$(node --version)"
    [[ "$version" == "v$want".* ]] || drift "node is $version, not $want.x"
  fi

  # Informational, never drift: nothing on this host updates Node on its own (apt's timers
  # and unattended-upgrades are masked), so this is where the owner sees a pending release.
  # As fresh as the lists.
  installed="$(dpkg-query -W -f='${Version}' nodejs 2>/dev/null || true)"
  if [ -n "$installed" ] && [ -n "$cand" ] && dpkg --compare-versions "$installed" lt "$cand"; then
    echo "pending: nodejs $installed -> $cand (sudo bash $0 install)"
  fi

  [ "$rc" -eq 0 ] && echo "nodesource: ok (node $version, candidate $cand)"
  return "$rc"
}

case "${1:-}" in
  install) install_nodesource ;;
  check) check_nodesource ;;
  *) usage ;;
esac
