"""Drift test for the CA bundle every outbound HTTPS call trusts (#58, #125).

httpx2, unlike httpx, defaults to the OS store (truststore), so certifi is
read only because co-status says so: every httpx2 client or request under
``src/`` and ``scripts/`` passes ``verify=trusted()`` (``src/core/tls.py``), a
context built from certifi's bundle alone. The healthchecks.io pings
(``src/core/heartbeat.py``) and the GitHub reads (``src/core/drift.py``) trust
whatever the lock resolves certifi to, and a host-level fix never reaches them.

Why not the OS store: on this host it still trusts emSign C1 and ECC C3, which
certifi 2026.10.23 dropped (CannObserv/provisioner#77). Moving would widen
trust silently, and this file would go on passing over a bundle nobody read.
So the checks are three: the bundle lacks the distrusted roots, ``trusted()``
holds that bundle and nothing else, and no call site goes without it.

``emSign ECC Root CA - G3`` is the root the leaked eMudhra intermediates chain
to (exe.dev's advisory, 2026-10-20; ``PYSEC-2026-4320``). certifi 2026.10.19
dropped it. A lock that resolves an older certifi, by a downgrade or a
constraint pulling it back, fails here rather than shipping.

The test is G3's absence, not "no emSign": certifi 2026.10.19 kept three emSign
roots it judged unaffected (G1, C1, ECC C3), and 2026.10.23 keeps G1 alone
(CannObserv/provisioner#77).

The roots are read as certificates, loaded the way the clients load them, not
matched in the bundle's ``# Subject:`` comments: a comment can change without
the trust changing, and a test reading only comments would then pass on nothing.
"""

import ast
import ssl
from pathlib import Path

import certifi
import pytest

from src.core.tls import trusted

REPO_ROOT = Path(__file__).resolve().parents[2]

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


def test_our_clients_trust_certifi_and_nothing_else():
    """``trusted()`` holds exactly certifi's roots: no OS store, no default paths."""
    certifi_roots = ssl.create_default_context(cafile=certifi.where())
    ours = trusted().get_ca_certs(binary_form=True)
    assert ours, "trusted() loaded no roots"
    assert sorted(ours) == sorted(certifi_roots.get_ca_certs(binary_form=True))


def test_our_clients_verify_hostnames():
    context = trusted()
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname


#: httpx2's names that open a connection, each taking ``verify=``.
HTTPX2_CALLS = {
    "Client",
    "AsyncClient",
    "request",
    "stream",
    "get",
    "options",
    "head",
    "post",
    "put",
    "patch",
    "delete",
}


def _call_sites() -> list[tuple[str, ast.Call]]:
    """Every ``httpx2.<name>(...)`` call under src/ and scripts/ that opens a connection."""
    sites = []
    for area in ("src", "scripts"):
        for path in sorted((REPO_ROOT / area).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "httpx2"
                    and node.func.attr in HTTPX2_CALLS
                ):
                    sites.append((f"{path.relative_to(REPO_ROOT)}:{node.lineno}", node))
    return sites


def _is_trusted_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "trusted")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "trusted")
    )


def test_every_client_was_found():
    """Heartbeat's two, drift's and rotate_key's: a walk finding none passes vacuously."""
    assert len(_call_sites()) >= 4


@pytest.mark.parametrize("site", _call_sites(), ids=lambda site: site[0])
def test_every_client_verifies_against_certifi(site):
    where, call = site
    verify = [kw.value for kw in call.keywords if kw.arg == "verify"]
    assert verify and _is_trusted_call(verify[0]), (
        f"{where} opens an httpx2 connection without verify=trusted(), so it "
        f"trusts the OS store rather than certifi's bundle (#125)."
    )


@pytest.mark.parametrize("area", ["src", "scripts"])
def test_httpx2_is_reached_through_its_module(area):
    """``from httpx2 import AsyncClient`` would hide a call site from the walk above."""
    for path in sorted((REPO_ROOT / area).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "httpx2":
                names = {alias.name for alias in node.names} & HTTPX2_CALLS
                assert not names, f"{path.relative_to(REPO_ROOT)} imports {names} from httpx2"
