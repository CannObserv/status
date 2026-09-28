"""Tests for src/core/database.py — env reading and lazy engine init."""

import pytest

from src.core import database, db_safety


@pytest.fixture(autouse=True)
def _reset_engine():
    """Each test starts with no cached engine/session_factory."""
    database.reset_engine()
    yield
    database.reset_engine()


@pytest.fixture
def _db_url(monkeypatch):
    """Set a syntactically valid non-production DATABASE_URL.

    Does not actually connect. The ``_test`` suffix matters — the
    db_safety guard in get_database_url() refuses production names.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@h/d_test")


def test_get_database_url_reads_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x@y/z_test")
    assert database.get_database_url() == "postgresql+asyncpg://x@y/z_test"


def test_get_database_url_raises_when_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        database.get_database_url()


def test_get_engine_lazy_and_memoized(_db_url):
    assert database._engine is None  # lazy: nothing created until first call
    e1 = database.get_engine()
    e2 = database.get_engine()
    assert e1 is e2


def test_reset_engine_releases_singleton(_db_url):
    e1 = database.get_engine()
    database.reset_engine()
    e2 = database.get_engine()
    assert e1 is not e2


def test_get_session_factory_lazy_and_memoized(_db_url):
    assert database._session_factory is None
    f1 = database.get_session_factory()
    f2 = database.get_session_factory()
    assert f1 is f2


def test_reset_engine_releases_session_factory(_db_url):
    f1 = database.get_session_factory()
    database.reset_engine()
    f2 = database.get_session_factory()
    assert f1 is not f2


def test_get_database_url_refuses_production(monkeypatch):
    """The guard sits on the one chokepoint every connection path crosses."""
    monkeypatch.delenv(db_safety.ALLOW_PROD_ENV_VAR, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@h/status")
    with pytest.raises(db_safety.ProductionDatabaseError):
        database.get_database_url()


def test_get_database_url_allows_production_with_flag(monkeypatch):
    monkeypatch.setenv(db_safety.ALLOW_PROD_ENV_VAR, "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@h/status")
    assert database.get_database_url() == "postgresql+asyncpg://u@h/status"
