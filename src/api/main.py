"""FastAPI application entry point."""

from typing import Any

from fastapi import APIRouter, Depends, FastAPI

from src.api.deps import require_api_key
from src.api.routes.health import router as health_router
from src.api.schemas.errors import AuthErrorDetail
from src.core.logging import configure_audit_logging, configure_logging, get_logger

configure_logging()
# Every entry point able to mint or revoke a key opens the audit channel, not
# just the credential scripts. This process mints nothing — src/api/deps.py
# imports hash_key alone — but a route that ever did would otherwise put its
# record untagged in this unit's own journal, where `journalctl -t
# status-keys` would miss it silently (notifier#67).
configure_audit_logging()
logger = get_logger(__name__)


app = FastAPI(title="co-status", version="0.1.0")

# Every /api/v1 route inherits require_api_key, so every one of them can fail
# these two ways. Declared here so both appear in the OpenAPI spec with a
# model: an undescribed response widens a generated client's return type to
# ``Any`` (notifier#22).
AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": AuthErrorDetail, "description": "Invalid API key"},
    403: {
        "model": AuthErrorDetail,
        "description": (
            "No API key supplied, or a key marked 'development' was presented "
            "to a production deployment"
        ),
    },
}

v1_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_key)],
    responses=AUTH_RESPONSES,
)

app.include_router(v1_router)
app.include_router(health_router)
