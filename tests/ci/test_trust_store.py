"""Drift test for the CA bundle every outbound HTTPS call trusts (#58).

httpx reads certifi's bundle, not the system store, so a host-level fix never
reaches it: the healthchecks.io pings (``src/core/heartbeat.py``) and the GitHub
reads (``src/core/drift.py``) trust whatever the lock resolves certifi to.

``emSign ECC Root CA - G3`` is the root the leaked eMudhra intermediates chain
to (exe.dev's advisory, 2026-10-20; ``PYSEC-2026-4320``). certifi 2026.10.19
dropped it. A lock that resolves an older certifi, by a downgrade or a
constraint pulling it back, fails here rather than shipping.

The test is G3's absence, not "no emSign": certifi 2026.10.19 kept three emSign
roots it judged unaffected (G1, C1, ECC C3), and 2026.10.23 keeps G1 alone
(CannObserv/provisioner#77).

The roots are read as certificates, loaded the way httpx loads them, not
matched in the bundle's ``# Subject:`` comments: a comment can change without
the trust changing, and a test reading only comments would then pass on nothing.
"""

import ssl

import certifi

DISTRUSTED_ROOTS = {"emSign ECC Root CA - G3"}


def trusted_common_names() -> set[str]:
    """Subject commonName of every root in certifi's bundle."""
    context = ssl.create_default_context(cafile=certifi.where())
    return {
        value
        for cert in context.get_ca_certs()
        for rdn in cert["subject"]
        for key, value in rdn
        if key == "commonName"
    }


def test_bundle_does_not_trust_distrusted_roots():
    """No distrusted root is among the bundle's roots, and roots were read at all."""
    names = trusted_common_names()
    assert names, f"no roots read from {certifi.where()}"
    assert names & DISTRUSTED_ROOTS == set(), certifi.__version__
