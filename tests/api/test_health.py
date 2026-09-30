"""Tests for src/api/routes/health.py.

Two things the probes must get right, and one of them is why the other
exists.

**Which deployment answered** (notifier#58). ``build`` alone cannot say: dev and
live may run the same release. ``database`` and ``environment`` can, and the
assertion that matters is that the two probes *agree* — ``/health`` classifies
the configured URL, ``/ready`` the live connection, so a match is what says
those have not diverged. Asserting only that ``environment`` holds one of its
two legal values passes for either and cannot fail on the bug the feature
exists to prevent.

**The build.** The release's ``REVISION`` (#9, R10), read by
:mod:`src.core.build` and tested there; here only that ``/health`` carries it.

**The schema** (#9, R8). ``/ready`` is 503 when the database is behind the
code, and says where it stands either way.
"""

import os
from collections.abc import AsyncGenerator
from typing import NoReturn

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from src.api.deps import get_db_session
from src.api.main import app
from src.api.routes import health as health_route
from src.api.routes.health import _resolve_database
from src.core import build, schema_state
from src.core.db_safety import database_name


@pytest.mark.asyncio
async def test_health_reports_the_release_build(client):
    assert (await client.get("/health")).json()["build"] == build.build_id()


def test_names_the_database_and_classifies_it(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/status_dev")
    assert _resolve_database() == ("status_dev", "development")


def test_classifies_production_by_the_absence_of_a_suffix(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/status")
    assert _resolve_database() == ("status", "production")


def test_a_third_database_is_named_even_though_it_classifies_as_production(monkeypatch):
    """The name is what makes a misconfiguration visible.

    ``serving_production()`` collapses every non-suffixed name onto
    ``production`` — correct, and it is why the classification alone cannot
    tell ``status`` from ``status_staging``.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/status_staging")
    assert _resolve_database() == ("status_staging", "production")


@pytest.mark.parametrize(
    "url", ["", "status", "postgresql+asyncpg://h/"], ids=["unset", "no-scheme", "no-name"]
)
def test_an_unreadable_url_fails_soft_to_unknown_production(monkeypatch, url):
    """Unlike the guard, this must not take liveness down.

    ``database_name()`` raises on any URL it cannot read. Letting that escape
    at import time means the app never starts, which is worse than the bug
    being fixed — so report ``unknown`` and, following ``serving_production()``,
    the conservative ``production``.
    """
    monkeypatch.setenv("DATABASE_URL", url)
    assert _resolve_database() == ("unknown", "production")


@pytest.mark.asyncio
async def test_health_payload_carries_the_endpoint_identity(client):
    """`build` agrees across both ports by design; `environment` is the signal."""
    body = (await client.get("/health")).json()
    assert set(body) == {"status", "build", "database", "environment"}


@pytest.mark.asyncio
async def test_ready_reports_the_database_actually_connected(client):
    """Ground truth: ``current_database()``, not what DATABASE_URL claims."""
    body = (await client.get("/ready")).json()
    assert body["database"] == database_name(os.environ["TEST_DATABASE_URL"])
    assert body["environment"] == "development"


@pytest.mark.asyncio
async def test_the_two_probes_agree_on_which_database_this_is(client):
    """The assertion a consumer's wiring-up check ultimately rests on.

    Asserting only that ``environment`` is one of its two legal values passes
    for either, so a ``/health`` claiming ``production`` while serving
    ``status_test`` would be green — the exact failure notifier#58 exists to make
    impossible. Agreement is the stronger claim, and it needs no literal
    database name: ``/health`` reads the configured URL and ``/ready`` reads
    the live connection, so the two matching is what says those have not
    diverged.
    """
    health = (await client.get("/health")).json()
    ready = (await client.get("/ready")).json()
    assert (health["database"], health["environment"]) == (
        ready["database"],
        ready["environment"],
    )


class _DeadSession:
    """A session that fails at query time, not at acquisition.

    Raising from the dependency itself would escape before the route's own
    ``try`` and never reach the branch under test. A connection that dies
    mid-query is also the truer shape of the outage: the pool handed one over
    and the far end was gone.
    """

    async def execute(self, *args: object, **kwargs: object) -> NoReturn:
        raise OperationalError("SELECT current_database()", {}, Exception("no connection"))


@pytest.mark.asyncio
async def test_ready_reports_503_without_naming_a_database(client):
    """The branch that only ever runs during an outage, so it is read once.

    Nothing covered it — ``grep "not_ready" tests/`` found nothing before this
    — and it is the branch notifier#58 came closest to reshaping. There is no
    connection to ask here, so it names no database at all rather than
    publishing two nulls a caller would have to check for.
    """

    async def failing_session() -> AsyncGenerator[_DeadSession]:
        yield _DeadSession()

    healthy_session = app.dependency_overrides[get_db_session]
    app.dependency_overrides[get_db_session] = failing_session
    try:
        response = await client.get("/ready")
    finally:
        # Restore, not pop: the client fixture installed the override that
        # binds every request to the savepointed session, and popping would
        # leave anything appended after this hitting the real factory.
        app.dependency_overrides[get_db_session] = healthy_session

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "db": False}


@pytest.mark.asyncio
async def test_ready_reports_a_current_schema(client):
    """What deploy.sh checks after a switch (#9, R6)."""
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["schema_state"] == "current"


@pytest.mark.asyncio
async def test_ready_is_503_when_the_database_is_behind_the_code(client, db_session):
    """2026-09-29's forgotten migration, as a probe answer (#9, R8).

    The database *was* reached, so the 503 names it: the fix is a migration
    against that database, and the payload says which one.
    """
    oldest = schema_state.script_directory().get_base()
    await db_session.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": oldest})

    response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "db": True,
        "database": database_name(os.environ["TEST_DATABASE_URL"]),
        "environment": "development",
        "schema_state": "behind",
    }


@pytest.mark.asyncio
async def test_ready_passes_a_database_ahead_of_the_code(client, db_session):
    """The old release mid-deploy, or after a rollback: ready, and saying so."""
    await db_session.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))

    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json()["schema_state"] == "ahead"


@pytest.mark.asyncio
async def test_two_stamped_heads_are_a_503_not_a_500(client, monkeypatch):
    """CR 8: a probe answers; it never raises."""

    async def two_heads(session):
        raise schema_state.MultipleHeads("the database is stamped at 2 heads")

    monkeypatch.setattr(health_route, "schema_state", two_heads)
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["schema_state"] == "unknown"


def test_the_503_is_one_model_so_generated_clients_keep_the_detail():
    """CR 8: anyOf[NotReady, SchemaBehind] with a superset second parses as the first."""
    spec = app.openapi()
    schema = spec["paths"]["/ready"]["get"]["responses"]["503"]["content"]["application/json"]
    assert schema["schema"] == {"$ref": "#/components/schemas/NotReadyResponse"}
    fields = spec["components"]["schemas"]["NotReadyResponse"]["properties"]
    assert {"status", "db", "database", "environment", "schema_state"} <= set(fields)
    # CR 18: omitted when unknown, never null, so no generated client types them nullable.
    for name in ("database", "environment", "schema_state"):
        assert fields[name].get("type") == "string", fields[name]
