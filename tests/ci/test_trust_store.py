"""Drift test for the CA bundle every outbound HTTPS call trusts (#58).

httpx reads certifi's bundle, not the system store, so a host-level fix never
reaches it: the healthchecks.io pings (``src/core/heartbeat.py``) and the GitHub
reads (``src/core/drift.py``) trust whatever the lock resolves certifi to.

``emSign ECC Root CA - G3`` is the root the leaked eMudhra intermediates chain
to (exe.dev's advisory, 2026-10-20; ``PYSEC-2026-4320``). certifi 2026.10.19
dropped it. A lock that resolves an older certifi, by a downgrade or a
constraint pulling it back, fails here rather than shipping.

The test is G3's absence, not "no emSign": certifi kept three emSign roots it
judged unaffected (G1, C1, ECC C3), so a fixed bundle still matches ``emSign``
(CannObserv/provisioner#77).
"""

from pathlib import Path

import certifi

DISTRUSTED_ROOTS = ("CN=emSign ECC Root CA - G3",)


def test_bundle_does_not_trust_distrusted_roots():
    bundle = Path(certifi.where()).read_text()
    assert [root for root in DISTRUSTED_ROOTS if root in bundle] == [], certifi.__version__
