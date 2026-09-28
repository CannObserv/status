"""Tests for src/api/routes/health.py.

Two things the probes must get right, and one of them is why the other
exists.

**Which deployment answered** (notifier#58). ``build`` cannot say: the two units
serve one working tree, so the commit agrees on both ports and always will.
``database`` and ``environment`` can, and the assertion that matters is that
the two probes *agree* — ``/health`` classifies the configured URL, ``/ready``
the live connection, so a match is what says those have not diverged.
Asserting only that ``environment`` holds one of its two legal values passes
for either and cannot fail on the bug the feature exists to prevent.

**The build stamp.** Both systemd units write it with

    echo BUILD_ID=$(git rev-parse --short HEAD) > /run/status/build-id…

and `echo` exits 0 even when the command substitution comes back empty — a
failing `git` is not a failing ExecStartPre. So the app must treat an empty
BUILD_ID exactly like a missing one; `os.environ.get(name, default)` does not,
because the default fires only on absence.
"""

import os
from collections.abc import AsyncGenerator
from typing import NoReturn

import pytest
from sqlalchemy.exc import OperationalError

from src.api.deps import get_db_session
from src.api.main import app
from src.api.routes.health import _resolve_build_id, _resolve_database
from src.core.db_safety import database_name


def test_uses_the_stamp_when_one_is_written(monkeypatch):
    monkeypatch.setenv("BUILD_ID", "6c760bf")
    assert _resolve_build_id() == "6c760bf"


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "whitespace"])
def test_falls_back_when_the_stamp_is_blank(monkeypatch, value):
    """A blank stamp reads as a broken health endpoint, not a missing build."""
    monkeypatch.setenv("BUILD_ID", value)
    assert _resolve_build_id() == "dev"


def test_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("BUILD_ID", raising=False)
    assert _resolve_build_id() == "dev"


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
