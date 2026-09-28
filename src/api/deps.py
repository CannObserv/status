"""FastAPI dependencies — database session, API-key authentication, the alerter."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.alerting import Alerter, alerter_from_environment
from src.core.api_keys import hash_key
from src.core.database import get_session_factory
from src.core.db_safety import serving_production
from src.core.models.api_key import ApiKey


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Yield an async database session."""
    async with get_session_factory()() as session:
        yield session


_alerter: Alerter | None = None
_alerter_built = False


def get_alerter() -> Alerter | None:
    """The process's one :class:`~src.core.alerting.Alerter`, built on first use.

    ``None`` when the unit has no notifier key; the routes then record every
    check-in and send nothing.
    """
    global _alerter, _alerter_built
    if not _alerter_built:
        _alerter = alerter_from_environment()
        _alerter_built = True
    return _alerter


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    raw_key: str | None = Depends(api_key_header),
    session: AsyncSession = Depends(get_db_session),
) -> str:
    """Validate X-API-Key header; return ``tenant_id`` on success.

    Raises 403 when the header is absent, 401 when the key is invalid or not
    found, and 403 when a ``development`` key is presented to a production
    deployment. Updates ``last_used_at`` on each successful authentication —
    a refused key is never stamped, because it was never used.
    """
    if raw_key is None:
        raise HTTPException(status_code=403, detail="Not authenticated")
    key_hash = hash_key(raw_key)
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    # serving_production() classifies from DATABASE_URL in the environment,
    # while `session` comes from the engine memoized at first use. Today those
    # can only diverge under monkeypatch; if in-process URL swapping ever
    # becomes real, classify from the engine's URL instead.
    if api_key.environment != "production" and serving_production():
        raise HTTPException(
            status_code=403,
            detail=(
                f"This is a production deployment; the supplied key is marked "
                f"'{api_key.environment}'. Point non-production traffic at a "
                f"non-production co-status."
            ),
        )
    api_key.last_used_at = datetime.now(UTC)
    await session.commit()
    return str(api_key.tenant_id)
