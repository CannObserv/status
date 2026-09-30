"""Health and readiness check endpoints."""

import os
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.schemas.health import (
    HealthResponse,
    NotReadyResponse,
    ReadyResponse,
    SchemaBehindResponse,
)
from src.core import build
from src.core.db_safety import database_name, environment_label
from src.core.schema_state import FAILING, schema_state

router = APIRouter(tags=["health"])


def _resolve_database() -> tuple[str, str]:
    """The database this process serves, and how it classifies.

    Fails soft, unlike :mod:`src.core.db_safety` itself. ``database_name()``
    raises on any URL it cannot read, and letting that escape at import time
    would mean the app never starts — a worse outcome than the ambiguity this
    reports. An unreadable URL is therefore ``"unknown"``, which carries no
    non-production suffix and so classifies as the conservative
    ``"production"`` — the same way round as ``serving_production()``.

    The name is not redundant with the classification: every non-suffixed name
    classifies as ``production``, so ``status`` and ``status_staging`` are
    one value there and two here.
    """
    try:
        name = database_name(os.environ.get("DATABASE_URL", ""))
    except ValueError:
        name = "unknown"
    return name, environment_label(name)


BUILD_ID = build.build_id()
DATABASE, ENVIRONMENT = _resolve_database()


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness probe — confirms the app process is running. No DB call.

    Unauthenticated, so a consumer can establish which deployment it is
    talking to before it has a key that works. ``build`` cannot answer that:
    dev and live may run the same release, so the commit can agree on either.

    ``database`` and ``environment`` are read from the configured URL, which
    is what keeps this a no-DB probe; ``/ready`` reports the database actually
    connected.
    """
    # Why it is safe for this to be unauthenticated: docs/DEPLOYMENT.md
    # § Health checks. Kept out of the docstring, which becomes the OpenAPI
    # description and ships into the generated SDK, where a repo path is a
    # signpost the reader cannot follow.
    return HealthResponse(status="ok", build=BUILD_ID, database=DATABASE, environment=ENVIRONMENT)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": NotReadyResponse | SchemaBehindResponse}},
)
async def ready(session: Annotated[AsyncSession, Depends(get_db_session)]) -> JSONResponse:
    """Readiness probe — the database, and its schema against this code. 503 on failure.

    ``current_database()`` rather than ``SELECT 1``: same round trip, and it
    names the database on the other end of the connection instead of the one
    ``DATABASE_URL`` claims. ``/health`` and ``/ready`` disagreeing is the
    only signal that those two have diverged.

    A database behind the code is not ready: the routes that touch a missing
    column fail. One ahead of it is, since migrations are expand-only.
    """
    try:
        result = await session.execute(text("SELECT current_database()"))
        name = str(result.scalar_one())
        state = await schema_state(session)
    except SQLAlchemyError:
        not_ready = NotReadyResponse(status="not_ready", db=False)
        return JSONResponse(status_code=503, content=not_ready.model_dump())
    if state in FAILING:
        behind = SchemaBehindResponse(
            status="not_ready",
            db=True,
            database=name,
            environment=environment_label(name),
            schema_state=state,
        )
        return JSONResponse(status_code=503, content=behind.model_dump())
    payload = ReadyResponse(
        status="ready",
        db=True,
        database=name,
        environment=environment_label(name),
        schema_state=state,
    )
    return JSONResponse(status_code=200, content=payload.model_dump())
