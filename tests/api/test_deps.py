"""Unit tests for FastAPI dependencies."""

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from ulid import ULID

from src.api import deps
from src.api.deps import get_db_session, require_api_key
from src.core import database
from src.core.api_keys import mint, revoke
from tests.api.conftest import PROBE_PATH

BLOCK_PATH = "/block"


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


def test_the_alerter_is_built_once_per_process(monkeypatch):
    """One NotifierClient, one connection pool, however many requests."""
    built = []
    monkeypatch.setattr(deps, "_alerter", None)
    monkeypatch.setattr(deps, "_alerter_built", False)
    monkeypatch.setattr(deps, "alerter_from_environment", lambda: built.append(1) or None)

    assert deps.get_alerter() is None
    assert deps.get_alerter() is None
    assert built == [1]


@pytest.fixture
async def real_engine(monkeypatch) -> AsyncGenerator[AsyncEngine]:
    """The app's own engine on the test database, as ``get_db_session`` uses it.

    The ``client`` fixtures override ``get_db_session`` with a savepointed
    session, so nothing through them exercises the real dependency (#122).
    Reset on both sides so no other test inherits this engine.
    """
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    database.reset_engine()
    engine = database.get_engine()
    yield engine
    await engine.dispose()
    database.reset_engine()


def _blocking_app(inside: asyncio.Event, release: asyncio.Event) -> FastAPI:
    """An app whose one route holds a real ``get_db_session`` connection until released."""
    app = FastAPI()

    @app.get(BLOCK_PATH)
    async def block(session: Annotated[AsyncSession, Depends(get_db_session)]) -> dict[str, int]:
        await session.execute(text("SELECT 1"))  # a session checks out lazily
        inside.set()
        await release.wait()
        return {"ok": 1}

    return app


async def test_a_cancelled_request_releases_its_session(real_engine):
    """fastapi 0.162.7 changed async yield-dependency cleanup on cancellation
    (fastapi/fastapi#16739): a cancelled request must still return its
    connection, or enough of them starve the pool and check-ins stop being
    recorded.

    Under ``ASGITransport`` the app runs in the client's task, so this is a
    task cancellation, not an ``http.disconnect``.
    """
    inside, release = asyncio.Event(), asyncio.Event()
    baseline = real_engine.pool.checkedout()
    async with AsyncClient(
        transport=ASGITransport(app=_blocking_app(inside, release)), base_url="http://test"
    ) as c:
        request = asyncio.create_task(c.get(BLOCK_PATH))
        await asyncio.wait_for(inside.wait(), timeout=5)
        # Not vacuous: the handler really holds a connection when cancelled.
        assert real_engine.pool.checkedout() == baseline + 1

        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    assert real_engine.pool.checkedout() == baseline


async def test_a_request_whose_client_disconnected_releases_its_session(real_engine):
    """A client disconnect as uvicorn delivers it: the app task is not cancelled;
    ``receive`` answers ``http.disconnect`` and ``send`` drops the response
    (uvicorn's h11 cycle). The handler runs to the end and must still return
    its connection.
    """
    inside, release = asyncio.Event(), asyncio.Event()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": BLOCK_PATH,
        "raw_path": BLOCK_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    pending = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> dict:
        return pending.pop() if pending else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        """The client is gone: drop it, as uvicorn does."""

    baseline = real_engine.pool.checkedout()
    app = asyncio.create_task(_blocking_app(inside, release)(scope, receive, send))
    await asyncio.wait_for(inside.wait(), timeout=5)
    assert real_engine.pool.checkedout() == baseline + 1

    release.set()  # the client has already gone
    await asyncio.wait_for(app, timeout=5)

    assert real_engine.pool.checkedout() == baseline
