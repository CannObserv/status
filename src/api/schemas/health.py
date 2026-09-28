"""Pydantic response schemas for the root-level health endpoints.

Typed rather than a bare ``dict`` because consumers are told to *assert* on
``environment``: the generated SDK model for an untyped route is a free-form
``additional_properties`` bag, so a new key there regenerates byte-identically
and stays invisible to the SDK. A field the wiring-up contract depends on
belongs in ``/openapi.json``.
"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness payload, including which deployment answered.

    ``build`` cannot distinguish the two endpoints: both units serve one
    working tree, so the SHA agreeing is correct and will keep agreeing
    (notifier#58). ``environment`` is the field to assert on, and it carries the same
    vocabulary as an API key's own marking — so a consumer sees the mismatch
    here before the 403 in ``require_api_key`` tells it the same thing.
    """

    status: str
    build: str
    database: str
    environment: str


class ReadyResponse(BaseModel):
    """Readiness payload, naming the database actually connected.

    ``HealthResponse.database`` is derived from ``DATABASE_URL``; this one
    comes from ``current_database()`` on the live session. The two disagreeing
    is a misconfiguration no other check would surface.

    Every field is required. Making them optional so one model could also
    describe the 503 would publish them as nullable on the success path, where
    they are always present, and hand every generated client a null check it
    can never need.
    """

    status: str
    db: bool
    database: str
    environment: str


class NotReadyResponse(BaseModel):
    """The 503 payload — no connection, so nothing to name.

    A separate model rather than a loosened ``ReadyResponse``: the 503 says
    only that the database could not be reached, and the shape it has carried
    since before the probes learned to name a database is the honest one.
    """

    status: str
    db: bool
