#!/usr/bin/env bash
# Print this host's Tailscale address, waiting for tailscaled to assign one.
#
# Both units bind uvicorn to the tailnet address alone (notifier#43 D3), so the API is
# unreachable from exe.dev's internal 10.42.0.0/16, from the exe.dev HTTPS
# proxy, and from the internet — defense in depth behind the Tailscale ACL.
#
# That buys a boot race. `After=tailscaled.service` orders startup but does not
# wait for tailscaled to *assign* an address, and uvicorn cannot bind an
# address that is not there yet: it exits non-zero, `Restart=on-failure` fires,
# and a tight StartLimitBurst turns a transient into a unit that stays failed
# until someone runs `systemctl reset-failed`. CannObserv/observo#473 is this
# exact race against redis-server; it cost that project two weeks of silently
# starved jobs.
#
# Two rules this script exists to hold, both learned there:
#
#   1. Probe /proc/net/fib_trie, never a netlink client. observo#479 found
#      their wait used `ip addr`, which the stock unit's sandbox SIGSYS-kills
#      while blocking AF_NETLINK — silently, so the probe still exited 0. The
#      wait detected nothing and every start paid a blind 30s sleep, boot
#      included, while looking like it worked. Reading a proc file needs no
#      socket and survives any sandbox we would plausibly apply.
#
#   2. Time out loudly. Redis could fall back to a local-only bind because a
#      local-only broker still serves local pipelines. co-status has no half
#      service — an unreachable API is the entire outage — so exhausting the
#      wait exits non-zero and lets Restart=on-failure retry, rather than
#      quietly binding something reachable from somewhere else.
#
# Usage:  HOST="$(scripts/tailnet_bind.sh)"  — address on stdout, notes on stderr.
set -euo pipefail

# Escape hatch for tests and diagnosis only. CI has no tailnet, so the probe
# there would burn the full wait and then fail every dev-server test. Set
# explicitly or not at all — there is deliberately no default, so an unset
# variable still means "resolve the real tailnet address".
#
# Never put this in an env file or a unit, for the reason STATUS_ALLOW_PROD_DB
# is not in one either: env files are sourced by every hand-run process, and a
# value here silently moves the bind off the tailnet.
# tests/deploy/test_systemd_unit.py asserts it appears in neither.
if [[ -n "${STATUS_BIND_HOST:-}" ]]; then
  echo "tailnet_bind: STATUS_BIND_HOST override in effect: ${STATUS_BIND_HOST}" >&2
  echo "$STATUS_BIND_HOST"
  exit 0
fi

WAIT_SECONDS="${STATUS_TAILNET_WAIT_SECONDS:-60}"
POLL_SECONDS=1

# The Tailscale CGNAT range is 100.64.0.0/10 — second octet 64 through 127.
# Matched on the address the kernel calls LOCAL, so a *peer* route through the
# tailnet can never be mistaken for this host's own address.
read_tailnet_address() {
  awk '
    /^Local:/                                  { in_local = 1 }
    in_local && $1 == "|--" && $2 ~ /^100\./   { candidate = $2; next }
    in_local && candidate != "" {
      if ($0 ~ /\/32 host LOCAL/) { print candidate; exit }
      candidate = ""
    }
  ' /proc/net/fib_trie |
  awk -F. '$2 >= 64 && $2 <= 127 { print; exit }'
}

deadline=$(( SECONDS + WAIT_SECONDS ))
announced=0
while :; do
  if address="$(read_tailnet_address)" && [[ -n "$address" ]]; then
    (( announced )) && echo "tailnet_bind: address ready: $address" >&2
    echo "$address"
    exit 0
  fi
  if (( SECONDS >= deadline )); then
    cat >&2 <<MSG
tailnet_bind: no Tailscale address after ${WAIT_SECONDS}s.

The service binds its tailnet address alone, so there is nothing safe to fall
back to — refusing to start rather than binding an address reachable from
somewhere it should not be.

  systemctl status tailscaled
  tailscale status
MSG
    exit 1
  fi
  if (( ! announced )); then
    echo "tailnet_bind: waiting up to ${WAIT_SECONDS}s for tailscaled to assign an address" >&2
    announced=1
  fi
  sleep "$POLL_SECONDS"
done
