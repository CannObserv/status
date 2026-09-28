"""Authenticated routes must declare their auth failure modes.

A failure mode absent from ``/openapi.json`` gives a generated client no typed
handle on it — including the 403 a production deployment returns for a
development key, which fires exactly when a consumer is misconfigured
(issue notifier#22).
"""

import pytest

from src.api.main import app

V1_PATHS = [p for p in app.openapi()["paths"] if p.startswith("/api/v1/")]


@pytest.mark.parametrize("status", ["401", "403"])
def test_v1_routes_declare_auth_failures(status):
    spec = app.openapi()
    assert V1_PATHS, "expected at least one /api/v1 path"
    for path in V1_PATHS:
        for method, operation in spec["paths"][path].items():
            assert status in operation["responses"], (
                f"{method.upper()} {path} does not document {status}"
            )


def test_403_description_names_the_development_key_case():
    spec = app.openapi()
    described = spec["paths"][V1_PATHS[0]]
    operation = next(iter(described.values()))
    assert "development" in operation["responses"]["403"]["description"].lower()


def test_unauthenticated_routes_do_not_declare_auth_failures():
    """/health and /ready are root-level and unguarded."""
    spec = app.openapi()
    for path in ("/health", "/ready"):
        if path not in spec["paths"]:
            continue
        for operation in spec["paths"][path].values():
            assert "401" not in operation["responses"]
            assert "403" not in operation["responses"]
