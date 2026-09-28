"""API fixtures: a probe app for ``require_api_key``, and a fake notifier.

The probe app exercises ``require_api_key`` in isolation.

The dependency guards every ``/api/v1`` route, so it is tested once, here,
through a route that does nothing else — rather than through whichever real
route happens to be cheapest to call, which couples the auth tests to that
route's own behaviour.
"""

import json
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from typing import Annotated

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from notifier_client import NotifierClient, RetryConfig
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.deps import get_alerter, get_db_session, require_api_key
from src.api.main import app
from src.core.alerting import NOTIFIER_URLS, Alerter

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


# --- a fake notifier, for the routes that call one -------------------------

#: Two channels co-status's notifier tenant owns in every route test.
CHANNELS = ["01J00000000000000000000CH1", "01J00000000000000000000CH2"]


@dataclass
class FakeNotifier:
    """notifier as the routes see it: respx routes plus what they received."""

    mock: respx.MockRouter
    health: respx.Route
    channels: respx.Route
    preview: respx.Route
    dispatch: respx.Route

    def dispatched(self) -> list[dict]:
        """The JSON bodies POSTed to /dispatch, in order."""
        return [json.loads(call.request.content) for call in self.dispatch.calls]


def _echo_dispatch(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        202,
        json={
            "id": str(ULID()),
            "tenant_id": "01J0000000000000000000TENT",
            "template_id": None,
            "idempotency_key": body.get("idempotency_key"),
            # No Jinja here: echoing the template is enough to tell which
            # notice a dispatch carried.
            "rendered_title": body["title_template"],
            "rendered_body": body["body_template"],
            "status": "succeeded",
            "metadata": body.get("metadata", {}),
            "created_at": "2026-09-09T12:00:00Z",
            "attempts": [],
        },
    )


@pytest.fixture
def notifier() -> Iterator[FakeNotifier]:
    """A development notifier that accepts everything, until a test says not."""
    with respx.mock(base_url=NOTIFIER_URLS["development"], assert_all_called=False) as mock:
        yield FakeNotifier(
            mock=mock,
            health=mock.get("/health").respond(json={"status": "ok", "environment": "development"}),
            channels=mock.get("/api/v1/channels").respond(
                json=[
                    {
                        "id": cid,
                        "tenant_id": "01J0000000000000000000TENT",
                        "name": f"c{n}",
                        "channel_hint": "slack",
                        "apprise_url_masked": "slack://***",
                        "created_at": "2026-09-09T12:00:00Z",
                        "updated_at": "2026-09-09T12:00:00Z",
                    }
                    for n, cid in enumerate(CHANNELS)
                ]
            ),
            preview=mock.post("/api/v1/preview").respond(json={"title": "t", "body": "b"}),
            dispatch=mock.post("/api/v1/dispatch").mock(side_effect=_echo_dispatch),
        )


@pytest.fixture
def alerter(notifier) -> Alerter:
    return Alerter(
        NotifierClient(
            base_url=NOTIFIER_URLS["development"],
            api_key="nk_test",
            retry_config=RetryConfig(backoff_base=0),
        ),
        environment="development",
    )


@pytest.fixture
async def api(client, alerter) -> AsyncGenerator[AsyncClient]:
    """The app client, with ``get_alerter`` bound to the fake notifier."""
    app.dependency_overrides[get_alerter] = lambda: alerter
    yield client
    app.dependency_overrides.pop(get_alerter, None)
