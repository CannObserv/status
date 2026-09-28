"""Drift tests for scripts/tailnet_bind.sh — the tailnet address resolver.

Both units bind uvicorn to this host's tailnet address alone (notifier#43 D3), so the
API is unreachable from exe.dev's internal 10.42.0.0/16, from the exe.dev
HTTPS proxy, and from the internet. Defense in depth behind the Tailscale ACL.

That decision buys a boot race. ``After=tailscaled.service`` orders startup but
does not wait for tailscaled to *assign* an address, and unlike Redis 7 — whose
``-`` bind prefix makes an address optional — uvicorn has no way to bind an
address that is not there yet. It exits non-zero, ``Restart=on-failure`` fires,
and a tight ``StartLimitBurst`` turns a transient into a unit that stays failed
until a human runs ``systemctl reset-failed``.

That is not hypothetical: CannObserv/observo#473 is the same race against
redis-server, and it left that project's broker down for two weeks of silently
starved jobs. This module guards the two properties that incident turned up.
"""

import os
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tailnet_bind.sh"


def body() -> str:
    return SCRIPT.read_text()


def code() -> str:
    """The script's executable lines, with comments and blanks dropped.

    The script explains at length *why* it refuses to probe with ``ip``; a raw
    text search for that word would fail on the comment that documents it.
    """
    return "\n".join(
        line for line in body().splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR, "systemd ExecStart needs the exec bit"


def test_probe_reads_proc_not_a_netlink_client():
    """The observo#479 regression guard, ported.

    That project's wait probed with ``ip addr``. The stock redis-server unit's
    sandbox SIGSYS-kills ``ip`` and blocks ``AF_NETLINK`` — and does both
    *silently*, so the probe still exited 0. The wait detected nothing and
    every start paid a blind 30-second sleep, boot included, while appearing to
    work. ``/proc/net/fib_trie`` is readable under any sandbox we would plausibly
    apply and needs no netlink socket.

    Asserted on the executable lines so the comment explaining this rule does
    not trip the rule.
    """
    assert "/proc/net/fib_trie" in code()
    for netlink_client in (" ip ", "ip addr", "ip -4", "ifconfig", "tailscale ip"):
        assert netlink_client not in code(), (
            f"{netlink_client!r} is a netlink client; a sandbox can kill it silently "
            f"and leave this probe blind (observo#479)"
        )


def test_wait_is_bounded_and_fails_loudly():
    """A wait that never gives up is a hang; one that gives up quietly is worse.

    Redis could fall back to a local-only bind because a local-only broker
    still serves local pipelines. co-status has no such half-service: an
    unreachable API is the whole outage. So the timeout must exit non-zero and
    let ``Restart=on-failure`` retry, rather than binding something else.
    """
    assert "exit 1" in code(), "timeout must exit non-zero, never fall back to another bind"
    assert "100." in body(), "must document/match the CGNAT range it waits for"


def test_script_emits_only_the_address():
    """ExecStart captures stdout; diagnostics must not land in the bind argument."""
    assert ">&2" in code(), "diagnostics belong on stderr, leaving stdout for the address"
