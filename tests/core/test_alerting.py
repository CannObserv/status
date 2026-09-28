"""The one module that talks to notifier (spec § ``alerting.py``).

Every call goes through the real ``notifier-client``, intercepted beneath its
retry transport by ``respx`` (spec D15) — so retries, error mapping and
idempotency behave exactly as they will against notifier.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from notifier_client import NotifierClient, RetryConfig

from src.core.alerting import (
    CREDENTIAL_NAME,
    NOTIFIER_URLS,
    REQUEST_BUDGET_SECONDS,
    Alerter,
    AlertNotAccepted,
    AlertRejected,
    Budget,
    BudgetExceeded,
    EndpointMismatch,
    NoDeliverableChannel,
    NotifierUnavailable,
    TemplateRejected,
    alerter_from_environment,
    missing_key,
    read_notifier_key,
    recovery_key,
    within_budget,
)
from src.core.models.monitor import Monitor
from src.core.monitors import MonitorState

BASE = NOTIFIER_URLS["development"]
NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
CHANNELS = ["01J00000000000000000000CH1", "01J00000000000000000000CH2"]


def _dispatch(status: str = "succeeded", **overrides) -> dict:
    body = {
        "id": "01J0000000000000000000DISP",
        "tenant_id": "01J0000000000000000000TENT",
        "template_id": None,
        "idempotency_key": None,
        "rendered_title": "t",
        "rendered_body": "b",
        "status": status,
        "metadata": {},
        "created_at": "2026-09-09T12:00:00Z",
        "attempts": [],
    }
    body.update(overrides)
    return body


def _monitor(**overrides) -> Monitor:
    fields = {
        "id": "01J000000000000000000MONTR",
        "tenant_id": "01J0000000000000000000TENT",
        "name": "co-broker",
        "enabled": True,
        "interval_seconds": 600,
        "grace_seconds": 1200,
        "renotify_seconds": None,
        "state": MonitorState.OK,
        "created_at": NOW - timedelta(days=1),
        "last_checkin_at": NOW - timedelta(hours=1),
        "last_alert_at": None,
    }
    fields.update(overrides)
    return Monitor(**fields)


@pytest.fixture
def client() -> NotifierClient:
    """A real SDK client with instant retries, so a 5xx test does not sleep."""
    return NotifierClient(
        base_url=BASE, api_key="nk_test", retry_config=RetryConfig(backoff_base=0)
    )


@pytest.fixture
def alerter(client) -> Alerter:
    return Alerter(client, environment="development")


@pytest.fixture
def notifier():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


class TestWhereNotifierIs:
    def test_each_environment_has_its_own_notifier(self):
        """Derived, never configured — the move broker#3 made for its check-in
        URL: a wrong destination that cannot be named cannot be typo'd."""
        assert NOTIFIER_URLS == {
            "production": "http://notifier:9000",
            "development": "http://notifier:9001",
        }


class TestReadNotifierKey:
    def test_reads_the_systemd_credential(self, tmp_path):
        (tmp_path / CREDENTIAL_NAME).write_text("nk_secret\n")
        assert read_notifier_key({"CREDENTIALS_DIRECTORY": str(tmp_path)}) == "nk_secret"

    def test_no_credentials_directory_is_no_key(self):
        """A hand-run process outside the unit has no credential; that is not
        a crash, it is an alerter that cannot alert (and says so)."""
        assert read_notifier_key({}) == ""

    def test_a_directory_without_the_file_is_no_key(self, tmp_path):
        assert read_notifier_key({"CREDENTIALS_DIRECTORY": str(tmp_path)}) == ""

    def test_never_reads_the_key_from_an_environment_variable(self, tmp_path):
        """D13: the key is a credential file, never an env var."""
        assert read_notifier_key({"NOTIFIER_API_KEY": "nk_leaked"}) == ""


class TestIdempotencyKeys:
    """Deterministic, so a sweep that dies between dispatch and commit re-sends
    the same key and notifier returns the record it already has."""

    def test_the_first_missing_alert_is_ordinal_zero(self):
        monitor = _monitor(last_checkin_at=NOW - timedelta(minutes=47))
        assert missing_key(monitor, NOW) == (
            "01J000000000000000000MONTR:missing:2026-09-09T11:43:00Z:0"
        )

    def test_is_stable_across_passes_within_one_period(self):
        monitor = _monitor(last_checkin_at=NOW - timedelta(minutes=47))
        assert missing_key(monitor, NOW) == missing_key(monitor, NOW + timedelta(minutes=5))

    def test_each_renotify_period_gets_its_own_key(self):
        monitor = _monitor(last_checkin_at=NOW - timedelta(hours=6), renotify_seconds=3600)
        first = missing_key(monitor, NOW)
        next_hour = missing_key(monitor, NOW + timedelta(hours=1))
        assert first != next_hour
        assert first.endswith(":5") and next_hour.endswith(":6")

    def test_a_new_outage_gets_a_new_key(self):
        """A check-in moves the deadline, so the next outage is a new alert."""
        before = _monitor(last_checkin_at=NOW - timedelta(hours=2))
        after = _monitor(last_checkin_at=NOW - timedelta(hours=1))
        assert missing_key(before, NOW) != missing_key(after, NOW)

    def test_recovery_is_keyed_on_the_outage_it_ends(self):
        monitor = _monitor(last_checkin_at=NOW - timedelta(hours=2))
        assert recovery_key(monitor) == "01J000000000000000000MONTR:recovered:2026-09-09T10:00:00Z"

    def test_recovery_before_any_checkin_is_keyed_on_creation(self):
        monitor = _monitor(last_checkin_at=None, created_at=NOW - timedelta(days=1))
        assert recovery_key(monitor).endswith(":recovered:2026-09-08T12:00:00Z")

    def test_keys_fit_notifiers_limit(self):
        """notifier caps idempotency_key at 200 characters."""
        monitor = _monitor(last_checkin_at=NOW - timedelta(days=400), renotify_seconds=60)
        assert len(missing_key(monitor, NOW)) <= 200


class TestCheckEndpoint:
    """A co-status pointed at the wrong notifier inverts, like a consumer
    does (notifier docs/reference/monitors.md § Check the endpoint)."""

    async def test_passes_when_the_environments_agree(self, alerter, notifier):
        notifier.get("/health").respond(json={"status": "ok", "environment": "development"})
        await alerter.check_endpoint()

    async def test_refuses_a_notifier_in_the_other_environment(self, alerter, notifier):
        notifier.get("/health").respond(json={"status": "ok", "environment": "production"})
        with pytest.raises(EndpointMismatch, match="production"):
            await alerter.check_endpoint()

    async def test_refuses_a_health_body_that_names_no_environment(self, alerter, notifier):
        notifier.get("/health").respond(json={"status": "ok"})
        with pytest.raises(EndpointMismatch):
            await alerter.check_endpoint()

    async def test_an_unreachable_notifier_is_unavailable_not_mismatched(self, alerter, notifier):
        notifier.get("/health").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NotifierUnavailable):
            await alerter.check_endpoint()


class TestSend:
    async def test_sends_the_templates_inline_with_the_variables(self, alerter, notifier):
        route = notifier.post("/api/v1/dispatch").respond(202, json=_dispatch())
        delivery = await alerter.send(
            title_template="T {{ x }}",
            body_template="B",
            variables={"x": "1"},
            channel_ids=CHANNELS,
            idempotency_key="k",
            metadata={"monitor_id": "m", "reason": "missing"},
        )
        sent = route.calls.last.request
        body = httpx.Response(200, content=sent.content).json()
        assert body["title_template"] == "T {{ x }}"
        assert body["variables"] == {"x": "1"}
        assert body["channel_ids"] == CHANNELS
        assert body["idempotency_key"] == "k"
        assert body["metadata"] == {"monitor_id": "m", "reason": "missing"}
        assert "template_id" not in body
        assert delivery.dispatch_id == "01J0000000000000000000DISP"
        assert delivery.status == "succeeded"

    @pytest.mark.parametrize("status", ["failed", "partial"])
    async def test_a_202_is_not_a_delivery(self, alerter, notifier, status, caplog):
        """202 means accepted, not delivered (notifier#70): read ``status``."""
        notifier.post("/api/v1/dispatch").respond(202, json=_dispatch(status=status))
        delivery = await alerter.send(
            title_template="t", body_template="b", variables={}, channel_ids=CHANNELS
        )
        assert delivery.status == status
        assert any(status in r.getMessage() for r in caplog.records)

    async def test_retries_once_without_channels_notifier_no_longer_has(
        self, alerter, notifier, caplog
    ):
        """One deleted channel must not silence a monitor."""
        route = notifier.post("/api/v1/dispatch")
        route.side_effect = [
            httpx.Response(
                404,
                json={
                    "detail": {
                        "message": "channels not found or not owned by tenant",
                        "channel_ids": [CHANNELS[0]],
                    }
                },
            ),
            httpx.Response(202, json=_dispatch()),
        ]
        delivery = await alerter.send(
            title_template="t", body_template="b", variables={}, channel_ids=CHANNELS
        )
        retried = httpx.Response(200, content=route.calls.last.request.content).json()
        assert retried["channel_ids"] == [CHANNELS[1]]
        assert delivery.dropped_channel_ids == (CHANNELS[0],)
        assert any(CHANNELS[0] in r.getMessage() for r in caplog.records)

    async def test_a_second_unknown_channel_on_the_retry_gives_up(self, alerter, notifier):
        """A channel deleted between the two calls: one retry, not a loop."""
        route = notifier.post("/api/v1/dispatch")
        route.side_effect = [
            httpx.Response(404, json={"detail": {"message": "x", "channel_ids": [CHANNELS[0]]}}),
            httpx.Response(404, json={"detail": {"message": "x", "channel_ids": [CHANNELS[1]]}}),
        ]
        with pytest.raises(NoDeliverableChannel):
            await alerter.send(
                title_template="t", body_template="b", variables={}, channel_ids=CHANNELS
            )
        assert route.call_count == 2

    async def test_a_404_that_names_no_channels_is_rejected(self, alerter, notifier):
        notifier.post("/api/v1/dispatch").respond(404, text="not json")
        with pytest.raises(AlertRejected):
            await alerter.send(
                title_template="t", body_template="b", variables={}, channel_ids=CHANNELS
            )

    async def test_no_channel_left_is_not_accepted(self, alerter, notifier):
        notifier.post("/api/v1/dispatch").respond(
            404, json={"detail": {"message": "gone", "channel_ids": CHANNELS}}
        )
        with pytest.raises(NoDeliverableChannel):
            await alerter.send(
                title_template="t", body_template="b", variables={}, channel_ids=CHANNELS
            )

    async def test_no_channels_at_all_is_refused_before_calling(self, alerter, notifier):
        route = notifier.post("/api/v1/dispatch")
        with pytest.raises(NoDeliverableChannel):
            await alerter.send(title_template="t", body_template="b", variables={}, channel_ids=[])
        assert not route.called

    async def test_a_network_failure_is_unavailable(self, alerter, notifier):
        notifier.post("/api/v1/dispatch").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NotifierUnavailable):
            await alerter.send(
                title_template="t",
                body_template="b",
                variables={},
                channel_ids=CHANNELS,
                idempotency_key="k",
            )

    async def test_a_5xx_after_the_sdks_retries_is_unavailable(self, alerter, notifier):
        route = notifier.post("/api/v1/dispatch").respond(503)
        with pytest.raises(NotifierUnavailable):
            await alerter.send(
                title_template="t",
                body_template="b",
                variables={},
                channel_ids=CHANNELS,
                idempotency_key="k",
            )
        assert route.call_count == 3  # the SDK retries only because a key is present

    async def test_without_a_key_nothing_is_retried(self, alerter, notifier):
        """A report has no idempotency key, so the SDK must not replay it."""
        route = notifier.post("/api/v1/dispatch").respond(503)
        with pytest.raises(NotifierUnavailable):
            await alerter.send(
                title_template="t", body_template="b", variables={}, channel_ids=CHANNELS
            )
        assert route.call_count == 1

    @pytest.mark.parametrize("code", [401, 403, 422])
    async def test_a_refusal_is_rejected(self, alerter, notifier, code):
        notifier.post("/api/v1/dispatch").respond(code, json={"detail": "no"})
        with pytest.raises(AlertRejected) as caught:
            await alerter.send(
                title_template="t", body_template="b", variables={}, channel_ids=CHANNELS
            )
        assert caught.value.status_code == code

    def test_every_way_of_not_getting_a_record_is_one_family(self):
        """The sweep's rule turns on one fact — did notifier accept it — so
        callers catch one base class."""
        for cls in (NotifierUnavailable, AlertRejected, NoDeliverableChannel, BudgetExceeded):
            assert issubclass(cls, AlertNotAccepted)


class TestCheckTemplate:
    async def test_a_template_that_renders_passes(self, alerter, notifier):
        notifier.post("/api/v1/preview").respond(json={"title": "t", "body": "b"})
        await alerter.check_template("T", "B", {"x": 1})

    async def test_a_render_error_names_its_section(self, alerter, notifier):
        notifier.post("/api/v1/preview").respond(
            json={"error": "'x' is undefined", "error_section": "body"}
        )
        with pytest.raises(TemplateRejected) as caught:
            await alerter.check_template("T", "{{ x }}", {})
        assert caught.value.section == "body"
        assert "undefined" in caught.value.message

    async def test_an_unreachable_preview_is_unavailable(self, alerter, notifier):
        notifier.post("/api/v1/preview").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NotifierUnavailable):
            await alerter.check_template("T", "B", {})


class TestKnownChannelIds:
    async def test_an_unreachable_notifier_is_unavailable(self, alerter, notifier):
        notifier.get("/api/v1/channels").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NotifierUnavailable):
            await alerter.known_channel_ids()

    async def test_lists_co_status_own_channels(self, alerter, notifier):
        notifier.get("/api/v1/channels").respond(
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
        )
        assert await alerter.known_channel_ids() == set(CHANNELS)


class TestWithinBudget:
    async def test_returns_what_finishes_in_time(self):
        async def quick():
            return 42

        assert await within_budget(quick(), budget=1.0) == 42

    async def test_gives_up_past_the_budget(self):
        async def slow():
            await asyncio.sleep(5)

        with pytest.raises(BudgetExceeded):
            await within_budget(slow(), budget=0.01)

    def test_the_request_budget_undercuts_the_consumers_timeouts(self):
        """broker and watcher give a check-in 10 seconds; co-status must answer
        inside that or the consumer retries and a report is sent twice."""
        assert REQUEST_BUDGET_SECONDS < 10


class TestBudget:
    """One deadline shared by every notifier call a single check-in makes."""

    async def test_calls_share_one_deadline(self):
        budget = Budget(0.2)

        async def nap():
            await asyncio.sleep(0.15)

        await budget.run(nap())
        with pytest.raises(BudgetExceeded):
            await budget.run(nap())

    async def test_a_spent_budget_refuses_without_waiting(self):
        budget = Budget(0)

        async def never_awaited_long():
            await asyncio.sleep(5)

        with pytest.raises(BudgetExceeded):
            await budget.run(never_awaited_long())


class TestAlerterFromEnvironment:
    def _environ(self, tmp_path, database: str, key: str | None = "nk_secret") -> dict:
        if key is not None:
            (tmp_path / CREDENTIAL_NAME).write_text(key)
        return {
            "DATABASE_URL": f"postgresql+asyncpg://u@h/{database}",
            "CREDENTIALS_DIRECTORY": str(tmp_path),
        }

    @pytest.mark.parametrize(
        ("database", "environment"), [("status", "production"), ("status_dev", "development")]
    )
    def test_the_database_decides_which_notifier(self, tmp_path, database, environment):
        alerter = alerter_from_environment(self._environ(tmp_path, database))
        assert alerter is not None
        assert alerter.environment == environment

    def test_no_key_is_no_alerter_and_says_so(self, tmp_path, caplog):
        assert alerter_from_environment(self._environ(tmp_path, "status", key=None)) is None
        assert any("no notifier key" in r.getMessage() for r in caplog.records)

    def test_an_unreadable_url_is_treated_as_production(self, tmp_path):
        """Fails safe, the same way round as serving_production()."""
        environ = self._environ(tmp_path, "status")
        environ["DATABASE_URL"] = "nonsense"
        assert alerter_from_environment(environ).environment == "production"
