"""The one module that talks to notifier (spec § ``alerting.py``, D5).

co-status originates alerts and delivers none itself: every one goes to
notifier's ``POST /api/v1/dispatch`` as an ordinary tenant, through the
``notifier-client`` SDK. Nothing above this module knows about HTTP.

What callers need from it is one fact — **did notifier accept the alert?** —
because the sweep's owed-alert rule turns on it (``last_alert_at`` is set only
on acceptance). So every way of not getting a dispatch record raises one
family, :class:`AlertNotAccepted`, and a record comes back as a
:class:`Delivery` whatever its delivery ``status``: a 202 says notifier took
the alert, not that it reached anyone (notifier#70).
"""

import asyncio
import os
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from notifier_client import NotifierClient, NotifierError, RateLimited, ServerError
from notifier_client.types import DispatchOut

from src.core.credentials import read_credential
from src.core.db_safety import database_name, environment_label
from src.core.logging import get_logger
from src.core.models.monitor import Monitor
from src.core.monitors import _as_utc, deadline_for
from src.core.utils import format_utc_iso

logger = get_logger(__name__)

#: Where each co-status environment sends its alerts. Derived from co-status's
#: own environment, never configured: a destination that cannot be named
#: cannot be typo'd — broker#3's reasoning for its check-in URL. A co-status
#: pointed at the wrong notifier inverts the way a consumer does, and
#: :meth:`Alerter.check_endpoint` is the second line against it.
NOTIFIER_URLS = {
    "production": "http://notifier:9000",
    "development": "http://notifier:9001",
}

#: The ``LoadCredential=`` name both units use (spec D13).
CREDENTIAL_NAME = "notifier-key"

#: Every notifier call made while answering a check-in shares this budget.
#: broker and watcher give a check-in 10 seconds; answering later makes them
#: retry, and a report goes out twice.
REQUEST_BUDGET_SECONDS = 8.0


class AlertNotAccepted(Exception):
    """notifier did not return a dispatch record for this alert."""


class NotifierUnavailable(AlertNotAccepted):
    """A network failure, or a 5xx/429 after the SDK's retries."""


class AlertRejected(AlertNotAccepted):
    """notifier refused the request (a 4xx other than unknown channels)."""

    def __init__(self, message: str, status_code: int | None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NoDeliverableChannel(AlertNotAccepted):
    """No channel id left that notifier recognises — or none configured."""


class BudgetExceeded(AlertNotAccepted):
    """The request-path budget ran out before notifier answered."""


class EndpointMismatch(RuntimeError):
    """The notifier on the other end serves a different environment."""


class TemplateRejected(Exception):
    """notifier's preview could not render a consumer's templates."""

    def __init__(self, section: str | None, message: str) -> None:
        super().__init__(f"{section}: {message}")
        self.section = section
        self.message = message


@dataclass(frozen=True)
class Delivery:
    """A dispatch notifier accepted, and any channel ids it no longer has."""

    dispatch: DispatchOut
    dropped_channel_ids: tuple[str, ...] = ()

    @property
    def dispatch_id(self) -> str:
        return str(self.dispatch.id)

    @property
    def status(self) -> str:
        return str(self.dispatch.status)


def read_notifier_key(environ: Mapping[str, str] = os.environ) -> str:
    """The notifier API key from the systemd credential, or ``""``.

    Never from an environment variable (D13). Outside the unit there is no
    ``$CREDENTIALS_DIRECTORY``, and that is an alerter that cannot alert —
    the caller logs it — not a crash.
    """
    return read_credential(CREDENTIAL_NAME, environ)


def missing_key(monitor: Monitor, now: datetime) -> str:
    """The idempotency key for a missing alert, or its renotify.

    Deterministic, so a sweep that dies after sending and before committing
    sends the same key next pass and notifier hands back the record it
    already has. ``n`` counts whole ``renotify_seconds`` periods since the
    deadline: 0 for the first alert, and one new key per renotify period.
    """
    deadline = deadline_for(monitor)
    n = 0
    if monitor.renotify_seconds:
        elapsed = max((now - deadline).total_seconds(), 0)
        n = int(elapsed // monitor.renotify_seconds)
    return f"{monitor.id}:missing:{format_utc_iso(deadline)}:{n}"


def recovery_key(monitor: Monitor) -> str:
    """The idempotency key for a recovery notice: one per outage ended.

    Keyed on the check-in *before* the one that ended the outage, so it must
    be taken before ``last_checkin_at`` moves.
    """
    anchor = _as_utc(monitor.last_checkin_at or monitor.created_at)
    return f"{monitor.id}:recovered:{format_utc_iso(anchor)}"


async def within_budget[T](awaitable: Awaitable[T], *, budget: float = REQUEST_BUDGET_SECONDS) -> T:
    """Await *awaitable*, or raise :class:`BudgetExceeded` once *budget* runs out."""
    try:
        async with asyncio.timeout(budget):
            return await awaitable
    except TimeoutError as exc:
        raise BudgetExceeded(f"notifier did not answer within {budget}s") from exc


class Budget:
    """A deadline shared by every notifier call one check-in makes.

    ``within_budget`` bounds one call; a check-in can make up to four
    (endpoint check, preview, recovery, report), and it is their *sum* that
    has to land inside the consumer's timeout.
    """

    def __init__(self, seconds: float = REQUEST_BUDGET_SECONDS) -> None:
        self._deadline = asyncio.get_running_loop().time() + seconds

    def remaining(self) -> float:
        return max(self._deadline - asyncio.get_running_loop().time(), 0.0)

    async def run[T](self, awaitable: Awaitable[T]) -> T:
        """Await *awaitable* within what is left of the budget."""
        return await within_budget(awaitable, budget=self.remaining())


def _unknown_channel_ids(error: NotifierError) -> list[str]:
    """The ids a 404 from ``/dispatch`` names, or ``[]`` for any other 404."""
    if error.status_code != 404 or error.response is None:
        return []
    try:
        detail = error.response.json().get("detail", {})
    except ValueError:
        return []
    ids = detail.get("channel_ids") if isinstance(detail, dict) else None
    return [str(i) for i in ids] if isinstance(ids, list) else []


class Alerter:
    """co-status's handle on notifier, for one environment."""

    def __init__(self, client: NotifierClient, *, environment: str) -> None:
        self._client = client
        self.environment = environment

    async def check_endpoint(self) -> None:
        """Raise unless notifier's ``/health`` names co-status's environment."""
        try:
            health = await self._client.health()
        except (httpx.TransportError, ServerError, RateLimited) as exc:
            raise NotifierUnavailable(f"notifier /health unreachable: {exc!r}") from exc
        theirs = health.get("environment")
        if theirs != self.environment:
            raise EndpointMismatch(
                f"notifier reports environment {theirs!r}; this co-status serves "
                f"{self.environment!r}. Refusing to send alerts across environments."
            )

    async def check_template(
        self, title_template: str, body_template: str, variables: dict[str, Any]
    ) -> None:
        """Raise :class:`TemplateRejected` unless notifier can render these."""
        try:
            preview = await self._client.preview(
                title_template=title_template, body_template=body_template, variables=variables
            )
        except (httpx.TransportError, ServerError, RateLimited) as exc:
            raise NotifierUnavailable(f"notifier preview unreachable: {exc!r}") from exc
        error = preview.error if isinstance(preview.error, str) else None
        if error:
            section = preview.error_section if isinstance(preview.error_section, str) else None
            raise TemplateRejected(section, error)

    async def known_channel_ids(self) -> set[str]:
        """The channel ids co-status's own notifier tenant owns."""
        try:
            channels = await self._client.channels.list()
        except (httpx.TransportError, ServerError, RateLimited) as exc:
            raise NotifierUnavailable(f"notifier channels unreachable: {exc!r}") from exc
        return {str(c.id) for c in channels}

    async def send(
        self,
        *,
        title_template: str,
        body_template: str,
        variables: dict[str, Any],
        channel_ids: list[str],
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Delivery:
        """Dispatch through notifier; return the record it accepted.

        Retries once without any channel notifier reports as unknown, because
        one deleted channel must not silence a monitor.
        """
        if not channel_ids:
            raise NoDeliverableChannel("no channels configured")
        try:
            dispatch = await self._dispatch(
                title_template, body_template, variables, channel_ids, idempotency_key, metadata
            )
            dropped: tuple[str, ...] = ()
        except _UnknownChannels as unknown:
            remaining = [c for c in channel_ids if c not in unknown.ids]
            dropped = tuple(c for c in channel_ids if c in unknown.ids)
            logger.error(
                f"notifier no longer has channel(s) {', '.join(dropped)}; retrying without",
                extra={"dropped_channel_ids": list(dropped)},
            )
            if not remaining:
                raise NoDeliverableChannel(
                    f"notifier recognises none of {channel_ids}"
                ) from unknown
            try:
                dispatch = await self._dispatch(
                    title_template, body_template, variables, remaining, idempotency_key, metadata
                )
            except _UnknownChannels as again:
                raise NoDeliverableChannel(f"notifier rejected channels {again.ids}") from again
        delivery = Delivery(dispatch=dispatch, dropped_channel_ids=dropped)
        if delivery.status != "succeeded":
            logger.warning(
                f"notifier accepted dispatch {delivery.dispatch_id} with status {delivery.status}",
                extra={"dispatch_id": delivery.dispatch_id, "status": delivery.status},
            )
        return delivery

    async def _dispatch(
        self,
        title_template: str,
        body_template: str,
        variables: dict[str, Any],
        channel_ids: list[str],
        idempotency_key: str | None,
        metadata: dict[str, Any] | None,
    ) -> DispatchOut:
        try:
            return await self._client.dispatch(
                title_template=title_template,
                body_template=body_template,
                variables=variables,
                channel_ids=channel_ids,
                idempotency_key=idempotency_key,
                metadata=metadata,
            )
        except (httpx.TransportError, ServerError, RateLimited) as exc:
            raise NotifierUnavailable(f"notifier dispatch unreachable: {exc!r}") from exc
        except NotifierError as exc:
            unknown = _unknown_channel_ids(exc)
            if unknown:
                raise _UnknownChannels(unknown) from exc
            raise AlertRejected(str(exc), exc.status_code) from exc


class _UnknownChannels(Exception):
    """Internal: a 404 naming channel ids, caught by :meth:`Alerter.send`."""

    def __init__(self, ids: list[str]) -> None:
        super().__init__(ids)
        self.ids = ids


def alerter_from_environment(environ: Mapping[str, str] = os.environ) -> Alerter | None:
    """The alerter this process should use, or ``None`` if it cannot alert.

    The environment is read from ``DATABASE_URL`` the way ``/health`` reads
    it, so co-status's notion of which notifier to call cannot disagree with
    what it reports about itself. Without a key there is nothing to send
    with: logged as an error, and every check-in is still recorded.
    """
    try:
        environment = environment_label(database_name(environ.get("DATABASE_URL", "")))
    except ValueError:
        environment = "production"
    key = read_notifier_key(environ)
    if not key:
        logger.error(
            "no notifier key under $CREDENTIALS_DIRECTORY; this process will record "
            "check-ins but send no alerts",
            extra={"environment": environment},
        )
        return None
    return Alerter(
        NotifierClient(base_url=NOTIFIER_URLS[environment], api_key=key),
        environment=environment,
    )
