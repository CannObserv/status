"""Async database engine and session factory."""

import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.db_safety import assert_safe_database_url
from src.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_database_url() -> str:
    """Read and guard DATABASE_URL from the environment.

    Raises RuntimeError if not set — requires explicit configuration via
    /etc/status/.env (production) or repo .env (development).

    Raises ProductionDatabaseError if the URL names the production database
    without an explicit STATUS_ALLOW_PROD_DB=1 opt-in. This is the single
    chokepoint every connection path crosses; see src/core/db_safety.py.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Load env: set -a; . /etc/status/.env; . .env; set +a"
        )
    assert_safe_database_url(url)
    return url


def get_engine() -> AsyncEngine:
    """Return the shared async engine, creating it on first call."""
    global _engine
    if _engine is None:
        url = get_database_url()
        _engine = create_async_engine(url, echo=False)
        logger.info("database engine created", extra={"url": url.split("@")[-1]})
    return _engine


def reset_engine() -> None:
    """Reset the shared engine and session factory. For testing only."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the shared session factory, creating it on first call."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory
