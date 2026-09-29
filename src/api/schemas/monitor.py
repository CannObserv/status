"""Pydantic schemas for the monitor (dead-man's timer) endpoints.

Consumer-agnostic by construction. A check-in carries a two-valued ``status``
and an opaque ``variables`` bag, so a broker's ``finding_count``/``findings``
report travels through verbatim without any of that vocabulary entering
the schema or the OpenAPI document (AGENTS.md, API Boundary Principles).

``CheckinRequest`` and ``CheckinResponse`` are notifier's, unchanged (spec D6;
``tests/api/test_contract.py``). The CRUD models differ in one way: no
``template_id``, and an unknown field is a 422 rather than ignored, so a
caller still sending one hears about it (spec D5).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.dispatch import DispatchOut
from src.api.schemas.types import ULIDStr

# Literal rather than the MonitorState/CheckinStatus StrEnums, for the same
# reason DispatchOut.status is a Literal: Pydantic emits a $ref schema for an
# enum, which openapi-python-client turns into a differently-named generated
# class. tests/api/test_monitors_route.py and tests/core/test_monitors.py
# cross-check that these strings stay the enum's values.
MonitorStateLiteral = Literal["pending", "ok", "missing"]
CheckinStatusLiteral = Literal["ok", "alert"]


class MonitorCreate(BaseModel):
    """Request body for POST /monitors.

    ``interval_seconds`` is the cadence the consumer promises; a check-in is
    only late once ``grace_seconds`` on top of it has also passed. Both
    templates are required: a monitor that cannot render a report is one
    whose report fails at exactly the moment it matters.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    interval_seconds: int = Field(gt=0)
    grace_seconds: int = Field(default=0, ge=0)
    renotify_seconds: int | None = Field(default=None, gt=0)
    channel_ids: list[ULIDStr] = Field(default_factory=list)
    title_template: str = Field(min_length=1)
    body_template: str = Field(min_length=1)
    enabled: bool = True


class MonitorUpdate(BaseModel):
    """Request body for PATCH /monitors/{id}. Every field optional."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    interval_seconds: int | None = Field(default=None, gt=0)
    grace_seconds: int | None = Field(default=None, ge=0)
    renotify_seconds: int | None = Field(default=None, gt=0)
    channel_ids: list[ULIDStr] | None = None
    title_template: str | None = Field(default=None, min_length=1)
    body_template: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class MonitorOut(BaseModel):
    """Response body for the monitor CRUD endpoints."""

    id: str
    tenant_id: str
    name: str
    enabled: bool
    interval_seconds: int
    grace_seconds: int
    renotify_seconds: int | None
    channel_ids: list[str]
    title_template: str | None
    body_template: str | None
    state: MonitorStateLiteral
    last_checkin_at: datetime | None
    last_status: str | None
    #: The last report, verbatim. Never interpreted here.
    last_variables: dict[str, Any]
    last_alert_at: datetime | None
    #: notifier's delivery ``status`` for that alert: ``succeeded``,
    #: ``partial`` or ``failed``. Accepted is not delivered (#6).
    last_alert_status: str | None
    #: When silence becomes an alert. Served so a consumer never has to
    #: re-derive interval + grace to know where it stands.
    next_deadline_at: datetime
    created_at: datetime
    updated_at: datetime


class CheckinRequest(BaseModel):
    """Request body for POST /monitors/{id}/checkin.

    Send one **every tick, regardless of findings** — the arrival is the
    signal. ``status`` is the consumer's own judgement about the contents:
    ``alert`` renders and dispatches the monitor's template, ``ok`` records
    the check-in and sends nothing.
    """

    status: CheckinStatusLiteral = "ok"
    variables: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckinResponse(BaseModel):
    """Response body for POST /monitors/{id}/checkin."""

    monitor_id: str
    previous_state: MonitorStateLiteral
    state: MonitorStateLiteral
    last_checkin_at: datetime
    next_deadline_at: datetime
    #: Zero, one, or two dispatches: a recovery notice when this check-in
    #: ended an outage, and the rendered report when ``status`` was ``alert``.
    dispatches: list[DispatchOut]
