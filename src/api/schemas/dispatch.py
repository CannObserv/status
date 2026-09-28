"""notifier's dispatch record, as co-status's check-in response carries it.

A check-in answers with the dispatches it caused, in exactly notifier's shape
(spec D6): ``tests/api/test_contract.py`` compares these two schemas with
notifier's own. Ported field for field from notifier's
``src/api/schemas/dispatch.py``; built from the record ``notifier-client``
returns, never from rows of our own.
"""

from datetime import datetime
from typing import Any, Literal

from notifier_client.types import DispatchOut as SdkDispatchOut
from pydantic import BaseModel


class DispatchAttemptOut(BaseModel):
    """Response body fragment representing one channel attempt."""

    channel_id: str
    # Literal rather than an enum, as in notifier: an enum emits a $ref schema
    # and a different generated class, so the wire shape would differ.
    status: Literal["succeeded", "failed"]
    reason: str
    attempt: int
    started_at: datetime
    finished_at: datetime | None = None


class DispatchOut(BaseModel):
    """Response body for POST /dispatch and GET /dispatch/{id}."""

    id: str
    tenant_id: str
    template_id: str | None
    idempotency_key: str | None
    rendered_title: str
    rendered_body: str
    status: Literal["succeeded", "partial", "failed"]
    metadata: dict[str, Any]
    created_at: datetime
    attempts: list[DispatchAttemptOut]

    @classmethod
    def from_sdk(cls, dispatch: SdkDispatchOut) -> "DispatchOut":
        """Re-shape the SDK's record for our own response."""
        return cls.model_validate(dispatch.to_dict())
