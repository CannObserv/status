"""Unit tests for FastAPI dependencies."""

from unittest.mock import AsyncMock, MagicMock

from ulid import ULID

from src.api.deps import require_api_key
from src.core.api_keys import mint, revoke
from tests.api.conftest import PROBE_PATH


async def test_require_api_key_returns_str_tenant_id():
    """require_api_key returns str, not ULID — guards against the str() cast being removed."""
    ulid_obj = ULID.from_str("01H5K3G8V4HCQ2DXFE5FJNWQER")
    mock_api_key = MagicMock()
    mock_api_key.tenant_id = ulid_obj
    # Without this the attribute is a MagicMock, which is != "production", so
    # the test fell into the production-refusal branch and passed only because
    # the session's database name ends in _test. Pin it: this test is about
    # the str() cast, not about key environments (those live in
    # test_key_environment.py).
    mock_api_key.environment = "production"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_api_key

    session = AsyncMock()
    session.execute.return_value = mock_result

    tenant_id = await require_api_key(raw_key="any-key", session=session)

    assert isinstance(tenant_id, str)
    assert tenant_id == "01H5K3G8V4HCQ2DXFE5FJNWQER"


async def test_a_key_minted_by_core_authenticates(probe_client, db_session, tenant):
    """The end-to-end guard against the hash being spelled twice.

    ``src/core/api_keys.mint`` writes the digest and ``require_api_key`` reads
    it back. While those were separate spellings — one in a script, one here —
    nothing failed if they drifted; the symptom was a freshly minted
    credential that a consumer could not use, found by the consumer. This
    fails on the commit that separates them.
    """
    _key, raw = await mint(db_session, tenant.id, "minted-by-core", "production")

    response = await probe_client.get(PROBE_PATH, headers={"X-API-Key": raw})

    assert response.status_code == 200


async def test_a_revoked_key_stops_authenticating(probe_client, db_session, tenant):
    """Revocation is a DELETE, so it fails closed: the row is simply not
    found. Nothing downstream has to remember to filter on it (notifier#62)."""
    doomed, raw = await mint(db_session, tenant.id, "doomed", "production")
    await mint(db_session, tenant.id, "survivor", "production")
    assert (await probe_client.get(PROBE_PATH, headers={"X-API-Key": raw})).status_code == 200

    await revoke(db_session, tenant.id, doomed.id)

    response = await probe_client.get(PROBE_PATH, headers={"X-API-Key": raw})
    assert response.status_code == 401
