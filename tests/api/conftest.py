"""API fixtures: a probe app for ``require_api_key``, and the app bound to the
fake notifier (``tests/conftest.py``).

The probe app exercises ``require_api_key`` in isolation.

The dependency guards every ``/api/v1`` route, so it is tested once, here,
through a route that does nothing else — rather than through whichever real
route happens to be cheapest to call, which couples the auth tests to that
route's own behaviour.
"""

from collections.abc import AsyncGenerator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_alerter, get_db_session, require_api_key
from src.api.main import app

PROBE_PATH = "/probe"


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get(PROBE_PATH)
    async def probe(tenant_id: Annotated[str, Depends(require_api_key)]) -> dict[str, str]:
        return {"tenant_id": tenant_id}

    return app


@pytest.fixture
async def probe_client(db_session) -> AsyncGenerator[AsyncClient]:
    """An AsyncClient for the probe app, bound to the savepointed session."""
    app = _probe_app()

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def api(client, alerter) -> AsyncGenerator[AsyncClient]:
    """The app client, with ``get_alerter`` bound to the fake notifier."""
    app.dependency_overrides[get_alerter] = lambda: alerter
    yield client
    app.dependency_overrides.pop(get_alerter, None)
