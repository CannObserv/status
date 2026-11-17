"""The CA roots co-status's own HTTPS calls trust: certifi's bundle, alone (#58, #125).

httpx2 trusts the OS store by default (truststore), where httpx read certifi.
co-status keeps certifi, because the two disagree: on this host the OS store
(ca-certificates 20260816ubuntu0.24.04.2) still trusts emSign C1 and ECC C3,
which certifi 2026.10.23 dropped (CannObserv/provisioner#77). The lock decides
the bundle, and ``tests/ci/test_trust_store.py`` checks it.

Every httpx2 client or request under ``src/`` and ``scripts/`` passes
``verify=trusted()``; the same test file holds them to it. ``SSL_CERT_FILE``
does not apply: an explicit context overrides it, as it did under httpx.
notifier is reached over plain ``http://`` on the tailnet, so the SDK's own
client, which co-status cannot configure, opens no TLS at all.
"""

import ssl
from functools import cache

import certifi


@cache
def trusted() -> ssl.SSLContext:
    """A verifying client context holding certifi's roots and nothing else.

    One per process: building it parses the whole bundle, and a context is safe
    to share between clients.
    """
    return ssl.create_default_context(cafile=certifi.where())
