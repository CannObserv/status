"""Pydantic response schemas for the root-level health endpoints.

Typed rather than a bare ``dict`` because consumers are told to *assert* on
``environment``: the generated SDK model for an untyped route is a free-form
``additional_properties`` bag, so a new key there regenerates byte-identically
and stays invisible to the SDK. A field the wiring-up contract depends on
belongs in ``/openapi.json``.
"""

from pydantic import BaseModel
from pydantic.json_schema import SkipJsonSchema


class HealthResponse(BaseModel):
    """Liveness payload, including which deployment answered.

    ``build`` cannot distinguish the two endpoints: dev and live may run the
    same release (#9), so the SHA agreeing is no signal either way
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
    is a misconfiguration no other check would surface. ``schema_state`` is
    ``current``, or ``ahead`` while an older release runs against a newer
    schema (#9, :mod:`src.core.schema_state`).

    Every field is required. Making them optional so one model could also
    describe the 503 would publish them as nullable on the success path, where
    they are always present, and hand every generated client a null check it
    can never need.
    """

    status: str
    db: bool
    database: str
    environment: str
    schema_state: str


class NotReadyResponse(BaseModel):
    """The 503 payload: one model, so a generated client keeps every detail (CR 8).

    Database unreachable: ``status`` and ``db`` alone, since there was no
    connection to name. Reached but behind the code (#9): the database, its
    environment and ``schema_state`` too, because the fix is a migration
    against exactly that one. The response omits the fields it cannot fill
    rather than publishing nulls a caller would have to check for.
    """

    status: str
    db: bool
    # SkipJsonSchema: absent when unknown, never null, so the published schema
    # is a plain string and no generated client types these nullable (CR 18).
    database: str | SkipJsonSchema[None] = None
    environment: str | SkipJsonSchema[None] = None
    schema_state: str | SkipJsonSchema[None] = None
