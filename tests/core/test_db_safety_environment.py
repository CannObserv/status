"""Tests for the running process's environment classification.

A database guard protects a process's own database. It says nothing about a
sibling's dev process calling this service's production API over HTTP — which
is how ~1289 fixture dispatches reached production (issue notifier#22, finding 3).
The API-key environment check needs to know which environment *this* process
is serving; that answer lives here, next to the URL parsing it reuses.
"""

import pytest

from src.core import db_safety


@pytest.mark.parametrize(
    "url,is_prod",
    [
        ("postgresql+asyncpg://u@h/status", True),
        ("postgresql+asyncpg://u@h/status_test", False),
        ("postgresql+asyncpg://u@h/status_dev", False),
        ("postgresql+asyncpg://u@h/test_status", True),
    ],
)
def test_serving_production_follows_the_database_name(monkeypatch, url, is_prod):
    monkeypatch.setenv("DATABASE_URL", url)
    assert db_safety.serving_production() is is_prod


def test_serving_production_is_true_when_url_is_unreadable(monkeypatch):
    """Fail safe: an unparseable URL is treated as production."""
    monkeypatch.setenv("DATABASE_URL", "not-a-url")
    assert db_safety.serving_production() is True


def test_serving_production_is_true_when_url_is_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db_safety.serving_production() is True


def test_serving_production_ignores_the_escape_flag(monkeypatch):
    """The opt-in permits opening prod; it does not make a dev DB prod."""
    monkeypatch.setenv(db_safety.ALLOW_PROD_ENV_VAR, "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@h/status_dev")
    assert db_safety.serving_production() is False
