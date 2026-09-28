"""A non-production API key must be refused by a production deployment.

This is the layer the four-layer DB guard cannot provide: it catches a dev
process on a sibling VM calling production co-status over HTTP with a valid
key (issue notifier#22, finding 3).
"""

import hashlib
import secrets

import pytest
from sqlalchemy import select

from src.core.models import ApiKey, Tenant
from tests.api.conftest import PROBE_PATH


async def _key(db_session, environment: str) -> str:
    tenant = Tenant(name=f"t-{secrets.token_hex(4)}")
    db_session.add(tenant)
    await db_session.flush()
    raw = "csk_" + secrets.token_urlsafe(16)
    db_session.add(
        ApiKey(
            tenant_id=tenant.id,
            label=f"{environment}-key",
            key_prefix=raw[:8],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            environment=environment,
        )
    )
    await db_session.flush()
    return raw


async def test_api_keys_default_to_production(db_session):
    raw = "csk_" + secrets.token_urlsafe(16)
    tenant = Tenant(name=f"t-{secrets.token_hex(4)}")
    db_session.add(tenant)
    await db_session.flush()
    key = ApiKey(
        tenant_id=tenant.id,
        label="unspecified",
        key_prefix=raw[:8],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
    )
    db_session.add(key)
    await db_session.flush()
    assert key.environment == "production"


async def test_production_deployment_refuses_a_development_key(
    probe_client, db_session, monkeypatch
):
    monkeypatch.setattr("src.api.deps.serving_production", lambda: True)
    raw = await _key(db_session, "development")
    response = await probe_client.get(PROBE_PATH, headers={"X-API-Key": raw})
    assert response.status_code == 403
    assert "development" in response.json()["detail"]


async def test_production_deployment_accepts_a_production_key(
    probe_client, db_session, monkeypatch
):
    monkeypatch.setattr("src.api.deps.serving_production", lambda: True)
    raw = await _key(db_session, "production")
    response = await probe_client.get(PROBE_PATH, headers={"X-API-Key": raw})
    assert response.status_code == 200


@pytest.mark.parametrize("environment", ["production", "development"])
async def test_non_production_deployment_accepts_either_key(
    probe_client, db_session, monkeypatch, environment
):
    """A dev deployment is where development keys are supposed to work."""
    monkeypatch.setattr("src.api.deps.serving_production", lambda: False)
    raw = await _key(db_session, environment)
    response = await probe_client.get(PROBE_PATH, headers={"X-API-Key": raw})
    assert response.status_code == 200


async def test_refusal_does_not_stamp_last_used_at(probe_client, db_session, monkeypatch):
    """A refused key was never successfully used."""
    monkeypatch.setattr("src.api.deps.serving_production", lambda: True)
    raw = await _key(db_session, "development")
    await probe_client.get(PROBE_PATH, headers={"X-API-Key": raw})
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    result = await db_session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    assert result.scalar_one().last_used_at is None
