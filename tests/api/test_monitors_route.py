"""Route-level tests for /api/v1/monitors and its check-in (spec § The check-in).

Ported from notifier's tests where the behaviour is notifier's; new where
co-status differs: alerts leave through a fake notifier (``notifier``
fixture) and every dispatch it received is asserted on, and each state change
leaves a ``monitor_events`` row.
"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.dialects import postgresql
from ulid import ULID

from src.api.deps import get_alerter
from src.api.main import app
from src.api.routes.monitors import _fault_report_query, _with_notices
from src.core import alerting
from src.core.alerting import cleared_key, recovery_key
from src.core.api_keys import mint
from src.core.models import MonitorEvent, Tenant
from src.core.models.monitor import Monitor
from src.core.monitors import CLEARED_TITLE, RECOVERY_TITLE
from tests.conftest import CHANNELS

HEADER = "X-API-Key"


@pytest.fixture
def headers(api_key) -> dict[str, str]:
    raw_key, _ = api_key
    return {HEADER: raw_key}


async def _create(api, headers, **overrides) -> dict:
    payload = {
        "name": f"m-{secrets.token_hex(4)}",
        "interval_seconds": 600,
        "grace_seconds": 1200,
        "channel_ids": CHANNELS,
        "title_template": "T {{ source }}",
        "body_template": "B",
    }
    payload.update(overrides)
    response = await api.post("/api/v1/monitors", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def monitor(api, headers) -> dict:
    return await _create(api, headers)


async def _row(db_session, monitor_id: str) -> Monitor:
    result = await db_session.execute(select(Monitor).where(Monitor.id == monitor_id))
    row = result.scalar_one()
    await db_session.refresh(row)
    return row


async def _events(db_session, monitor_id: str) -> list[MonitorEvent]:
    result = await db_session.execute(
        select(MonitorEvent).where(MonitorEvent.monitor_id == monitor_id).order_by(MonitorEvent.at)
    )
    return list(result.scalars().all())


async def _make_missing(db_session, monitor_id: str) -> None:
    row = await _row(db_session, monitor_id)
    row.state = "missing"
    row.last_alert_at = datetime.now(UTC) - timedelta(hours=2)
    row.last_checkin_at = datetime.now(UTC) - timedelta(hours=3)
    await db_session.flush()


def _checkin(api, headers, monitor, **body):
    return api.post(f"/api/v1/monitors/{monitor['id']}/checkin", headers=headers, json=body)


class TestCreate:
    async def test_creates_a_monitor_in_the_pending_state(self, api, headers):
        created = await _create(api, headers)
        assert created["state"] == "pending"
        assert created["channel_ids"] == CHANNELS
        assert "template_id" not in created

    async def test_reports_the_deadline_it_will_be_judged_against(self, api, headers):
        created = await _create(api, headers)
        deadline = datetime.fromisoformat(created["next_deadline_at"])
        born = datetime.fromisoformat(created["created_at"])
        assert deadline - born == timedelta(seconds=1800)

    async def test_rejects_a_channel_notifier_does_not_have(self, api, headers):
        """co-status cannot hold a foreign key to notifier's channels, so it
        asks notifier on every write (spec § Data model)."""
        stranger = str(ULID())
        response = await api.post(
            "/api/v1/monitors",
            headers=headers,
            json={
                "name": "m",
                "interval_seconds": 600,
                "channel_ids": [CHANNELS[0], stranger],
                "title_template": "T",
                "body_template": "B",
            },
        )
        assert response.status_code == 422
        assert stranger in response.text

    async def test_cannot_check_channels_while_notifier_is_down(self, api, headers, notifier):
        notifier.channels.mock(side_effect=httpx.ConnectError("refused"))
        response = await api.post(
            "/api/v1/monitors",
            headers=headers,
            json={
                "name": "m",
                "interval_seconds": 600,
                "channel_ids": CHANNELS,
                "title_template": "T",
                "body_template": "B",
            },
        )
        assert response.status_code == 503

    async def test_cannot_check_channels_without_a_notifier_key(self, client, headers):
        app.dependency_overrides[get_alerter] = lambda: None
        try:
            response = await client.post(
                "/api/v1/monitors",
                headers=headers,
                json={
                    "name": "m",
                    "interval_seconds": 600,
                    "channel_ids": CHANNELS,
                    "title_template": "T",
                    "body_template": "B",
                },
            )
        finally:
            app.dependency_overrides.pop(get_alerter, None)
        assert response.status_code == 503

    async def test_refuses_a_template_id(self, api, headers):
        """co-status stores no templates (spec D5)."""
        response = await api.post(
            "/api/v1/monitors",
            headers=headers,
            json={
                "name": "m",
                "interval_seconds": 600,
                "template_id": str(ULID()),
                "title_template": "T",
                "body_template": "B",
            },
        )
        assert response.status_code == 422
        assert "template_id" in response.text

    @pytest.mark.parametrize("missing", ["title_template", "body_template"])
    async def test_rejects_a_monitor_with_no_way_to_render_a_report(self, api, headers, missing):
        payload = {
            "name": "m",
            "interval_seconds": 600,
            "title_template": "T",
            "body_template": "B",
        }
        payload.pop(missing)
        response = await api.post("/api/v1/monitors", headers=headers, json=payload)
        assert response.status_code == 422

    async def test_rejects_a_non_positive_interval(self, api, headers):
        response = await api.post(
            "/api/v1/monitors",
            headers=headers,
            json={"name": "m", "interval_seconds": 0, "title_template": "T", "body_template": "B"},
        )
        assert response.status_code == 422

    async def test_rejects_a_duplicate_name_within_the_tenant(self, api, headers):
        await _create(api, headers, name="twice")
        response = await api.post(
            "/api/v1/monitors",
            headers=headers,
            json={
                "name": "twice",
                "interval_seconds": 60,
                "title_template": "T",
                "body_template": "B",
            },
        )
        assert response.status_code == 409


class TestReadUpdateDelete:
    async def test_lists_only_the_calling_tenants_monitors(self, api, headers, monitor):
        listed = (await api.get("/api/v1/monitors", headers=headers)).json()
        assert [m["id"] for m in listed] == [monitor["id"]]

    async def test_fetches_one(self, api, headers, monitor):
        response = await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)
        assert response.json()["name"] == monitor["name"]

    async def test_says_whether_its_last_alert_was_delivered(
        self, api, headers, monitor, db_session
    ):
        """Accepted is not delivered (#6): the owner can see it without the journal."""
        row = await _row(db_session, monitor["id"])
        row.last_alert_status = "partial"
        await db_session.flush()
        response = await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)
        assert response.json()["last_alert_status"] == "partial"

    async def test_unknown_id_is_404(self, api, headers):
        assert (await api.get(f"/api/v1/monitors/{ULID()}", headers=headers)).status_code == 404

    async def test_malformed_id_is_422(self, api, headers):
        assert (await api.get("/api/v1/monitors/not-a-ulid", headers=headers)).status_code == 422

    async def test_pausing_and_resuming_are_recorded(self, api, headers, monitor, db_session):
        """Planned downtime must not read as an outage in future uptime
        figures, and cannot be reconstructed later (spec § Data model)."""
        url = f"/api/v1/monitors/{monitor['id']}"
        assert (await api.patch(url, headers=headers, json={"enabled": False})).json()[
            "enabled"
        ] is False
        await api.patch(url, headers=headers, json={"enabled": False})  # no change, no event
        await api.patch(url, headers=headers, json={"enabled": True})
        kinds = [e.kind for e in await _events(db_session, monitor["id"])]
        assert kinds == ["paused", "resumed"]

    async def test_update_rejects_a_channel_notifier_does_not_have(self, api, headers, monitor):
        response = await api.patch(
            f"/api/v1/monitors/{monitor['id']}",
            headers=headers,
            json={"channel_ids": [str(ULID())]},
        )
        assert response.status_code == 422

    async def test_update_refuses_a_template_id(self, api, headers, monitor):
        response = await api.patch(
            f"/api/v1/monitors/{monitor['id']}", headers=headers, json={"template_id": str(ULID())}
        )
        assert response.status_code == 422

    async def test_renaming_onto_a_taken_name_is_409(self, api, headers, monitor):
        other = await _create(api, headers)
        response = await api.patch(
            f"/api/v1/monitors/{other['id']}", headers=headers, json={"name": monitor["name"]}
        )
        assert response.status_code == 409

    async def test_deletes(self, api, headers, monitor):
        url = f"/api/v1/monitors/{monitor['id']}"
        assert (await api.delete(url, headers=headers)).status_code == 204
        assert (await api.get(url, headers=headers)).status_code == 404


class TestCheckin:
    """Every tick, findings or not — the arrival is the signal."""

    async def test_a_clean_report_resets_the_timer_and_sends_nothing(
        self, api, headers, monitor, notifier
    ):
        response = await _checkin(api, headers, monitor, status="ok", variables={"n": 0})
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "ok"
        assert response.json()["dispatches"] == []
        assert not notifier.dispatch.called
        assert not notifier.preview.called  # an ok heartbeat is never checked

    async def test_status_defaults_to_ok(self, api, headers, monitor):
        response = await _checkin(api, headers, monitor)
        assert response.status_code == 202
        assert response.json()["state"] == "ok"

    async def test_the_checkin_advances_the_deadline(self, api, headers, monitor):
        body = (await _checkin(api, headers, monitor)).json()
        checked_in = datetime.fromisoformat(body["last_checkin_at"])
        deadline = datetime.fromisoformat(body["next_deadline_at"])
        assert deadline - checked_in == timedelta(seconds=1800)

    async def test_the_first_checkin_is_recorded_once(self, api, headers, monitor, db_session):
        await _checkin(api, headers, monitor)
        await _checkin(api, headers, monitor)
        assert [e.kind for e in await _events(db_session, monitor["id"])] == ["first_checkin"]

    async def test_an_alert_report_goes_to_notifier_inline(
        self, api, headers, monitor, notifier, db_session
    ):
        """co-status never renders Jinja: the consumer's templates and its
        variables go to /dispatch as they are (spec D5)."""
        response = await _checkin(
            api,
            headers,
            monitor,
            status="alert",
            variables={"source": "co-broker"},
            metadata={"run": "7"},
        )
        assert response.status_code == 202, response.text
        (sent,) = notifier.dispatched()
        assert sent["title_template"] == "T {{ source }}"
        assert sent["variables"] == {"source": "co-broker"}
        assert sent["channel_ids"] == CHANNELS
        assert "idempotency_key" not in sent  # a report is never replayed
        assert sent["metadata"] == {"run": "7", "monitor_id": monitor["id"], "reason": "report"}
        (dispatch,) = response.json()["dispatches"]
        assert dispatch["rendered_title"] == "T {{ source }}"
        events = await _events(db_session, monitor["id"])
        assert [e.kind for e in events] == ["first_checkin", "alert"]
        assert events[-1].dispatch_id == dispatch["id"]

    async def test_an_alert_still_counts_as_a_checkin(self, api, headers, monitor):
        body = (
            await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        ).json()
        assert body["state"] == "ok"
        assert body["last_checkin_at"] is not None

    async def test_the_whole_report_is_kept_verbatim(self, api, headers, monitor):
        report = {
            "source": "co-broker",
            "finding_count": 1,
            "findings": [{"check": "dlq", "subject": "content.fetch.dlq", "message": "depth 12"}],
        }
        await _checkin(api, headers, monitor, status="alert", variables=report)
        fetched = await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)
        assert fetched.json()["last_variables"] == report

    async def test_a_report_notifier_cannot_render_is_422_and_changes_nothing(
        self, api, headers, monitor, notifier, db_session
    ):
        """Today's behaviour, moved: the render check is notifier's preview."""
        notifier.preview.respond(json={"error": "'source' is undefined", "error_section": "title"})
        response = await _checkin(api, headers, monitor, status="alert", variables={})
        assert response.status_code == 422
        assert "title" in response.text
        row = await _row(db_session, monitor["id"])
        assert row.state == "pending"
        assert row.last_checkin_at is None
        assert (row.fault_since, row.fault_key) == (None, None)
        assert not notifier.dispatch.called

    async def test_checkin_on_an_unknown_monitor_is_404(self, api, headers):
        response = await api.post(f"/api/v1/monitors/{ULID()}/checkin", headers=headers, json={})
        assert response.status_code == 404

    async def test_rejects_an_unknown_status(self, api, headers, monitor):
        response = await _checkin(api, headers, monitor, status="degraded")
        assert response.status_code == 422

    async def test_a_disabled_monitor_still_records_and_reports(
        self, api, headers, monitor, notifier, db_session
    ):
        """``enabled`` gates the sweep only. The cutover imports a monitor
        disabled and switches the consumer onto it, so its check-ins and
        reports must land (spec § Cutover)."""
        await api.patch(
            f"/api/v1/monitors/{monitor['id']}", headers=headers, json={"enabled": False}
        )
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        assert response.status_code == 202
        assert len(notifier.dispatched()) == 1
        assert (await _row(db_session, monitor["id"])).last_checkin_at is not None


class TestWhenNotifierFails:
    """The check-in is always recorded and always answered (spec § alerting.py)."""

    async def test_an_unreachable_notifier_still_records_the_checkin(
        self, api, headers, monitor, notifier, db_session
    ):
        notifier.dispatch.mock(side_effect=httpx.ConnectError("refused"))
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        assert response.status_code == 202
        assert response.json()["dispatches"] == []
        row = await _row(db_session, monitor["id"])
        assert row.last_checkin_at is not None
        alert = (await _events(db_session, monitor["id"]))[-1]
        assert (alert.kind, alert.dispatch_id, alert.dispatch_status) == (
            "alert",
            None,
            "not_accepted",
        )

    async def test_a_preview_that_cannot_reach_notifier_sends_no_report(
        self, api, headers, monitor, notifier, db_session
    ):
        """Unchecked is not rejected: the check-in lands, the report does not,
        and its event says so (#19)."""
        notifier.preview.mock(side_effect=httpx.ConnectError("refused"))
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        assert response.status_code == 202
        assert not notifier.dispatch.called
        alert = (await _events(db_session, monitor["id"]))[-1]
        assert (alert.kind, alert.dispatch_id, alert.dispatch_status) == (
            "alert",
            None,
            "not_accepted",
        )

    async def test_a_notifier_in_the_wrong_environment_is_sent_nothing(
        self, api, headers, monitor, notifier, db_session
    ):
        notifier.health.respond(json={"status": "ok", "environment": "production"})
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        assert response.status_code == 202
        assert not notifier.dispatch.called
        assert (await _row(db_session, monitor["id"])).last_checkin_at is not None

    async def test_a_slow_notifier_is_cut_off_and_the_checkin_still_lands(
        self, api, headers, monitor, notifier, db_session, monkeypatch
    ):
        monkeypatch.setattr(alerting, "REQUEST_BUDGET_SECONDS", 0.05)

        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(202)

        notifier.dispatch.mock(side_effect=slow)
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        assert response.status_code == 202
        assert response.json()["dispatches"] == []
        assert (await _row(db_session, monitor["id"])).last_checkin_at is not None
        alert = (await _events(db_session, monitor["id"]))[-1]
        assert (alert.dispatch_id, alert.dispatch_status) == (None, "not_accepted")

    async def test_without_a_notifier_key_the_checkin_still_lands(
        self, client, headers, alerter, db_session
    ):
        """A unit missing its credential cannot start (D13); a hand-run
        process can, and must still record what arrives. The report it could
        not send is lost too (#19)."""
        app.dependency_overrides[get_alerter] = lambda: alerter
        created = await _create(client, headers)
        app.dependency_overrides[get_alerter] = lambda: None
        try:
            response = await _checkin(client, headers, created, status="alert", variables={"a": 1})
        finally:
            app.dependency_overrides.pop(get_alerter, None)
        assert response.status_code == 202
        assert (await _row(db_session, created["id"])).last_checkin_at is not None
        alert = (await _events(db_session, created["id"]))[-1]
        assert (alert.kind, alert.dispatch_status) == ("alert", "not_accepted")


class TestRecovery:
    async def test_a_missing_monitor_that_reports_again_announces_it(
        self, api, headers, monitor, notifier, db_session
    ):
        await _make_missing(db_session, monitor["id"])
        expected_key = recovery_key(await _row(db_session, monitor["id"]))

        body = (await _checkin(api, headers, monitor)).json()

        assert body["previous_state"] == "missing"
        assert body["state"] == "ok"
        (sent,) = notifier.dispatched()
        assert sent["title_template"] == RECOVERY_TITLE
        assert sent["variables"]["name"] == monitor["name"]
        assert sent["idempotency_key"] == expected_key
        assert sent["metadata"] == {"monitor_id": monitor["id"], "reason": "recovered"}
        recovered = (await _events(db_session, monitor["id"]))[-1]
        assert recovered.kind == "recovered"
        assert recovered.dispatch_id == body["dispatches"][0]["id"]

    async def test_recovery_and_findings_are_two_separate_notifications(
        self, api, headers, monitor, notifier, db_session
    ):
        """'It is back' and 'here is what it found' are different facts."""
        await _make_missing(db_session, monitor["id"])
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        reasons = [d["metadata"]["reason"] for d in notifier.dispatched()]
        assert reasons == ["recovered", "report"]
        assert len(response.json()["dispatches"]) == 2

    async def test_a_due_redelivery_is_left_to_the_sweep(
        self, api, headers, monitor, notifier, db_session
    ):
        """One redelivery can take ~8 s per failed channel: past a check-in's budget (#10)."""
        await _make_missing(db_session, monitor["id"])
        row = await _row(db_session, monitor["id"])
        row.last_alert_status = "failed"
        row.last_alert_dispatch_id = "01J0000000000000000000DISP"
        row.last_alert_redeliver_at = datetime.now(UTC) - timedelta(minutes=1)
        await db_session.flush()

        await _checkin(api, headers, monitor)

        assert notifier.redelivered() == []


class TestDeliveryStatus:
    """Accepted is not delivered (#8): each notice's event keeps notifier's
    delivery status beside its dispatch id, for the next sweep to surface."""

    @pytest.mark.parametrize("status", ["succeeded", "failed", "partial"])
    async def test_a_report_keeps_its_delivery_status(
        self, api, headers, monitor, notifier, db_session, status
    ):
        notifier.delivering(status)
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        assert response.status_code == 202
        (sent,) = response.json()["dispatches"]
        assert sent["status"] == status
        alert = (await _events(db_session, monitor["id"]))[-1]
        assert (alert.kind, alert.dispatch_id, alert.dispatch_status) == (
            "alert",
            sent["id"],
            status,
        )

    @pytest.mark.parametrize("status", ["succeeded", "failed", "partial"])
    async def test_a_recovery_keeps_its_delivery_status(
        self, api, headers, monitor, notifier, db_session, status
    ):
        await _make_missing(db_session, monitor["id"])
        notifier.delivering(status)
        response = await _checkin(api, headers, monitor)
        (sent,) = response.json()["dispatches"]
        recovered = (await _events(db_session, monitor["id"]))[-1]
        assert (recovered.kind, recovered.dispatch_id, recovered.dispatch_status) == (
            "recovered",
            sent["id"],
            status,
        )

    async def test_recovery_and_report_each_keep_their_own(
        self, api, headers, monitor, notifier, db_session
    ):
        """The one request that writes two events with dispatches (CR 3)."""
        await _make_missing(db_session, monitor["id"])
        notifier.delivering(recovered="succeeded", report="failed")
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        recovery, report = response.json()["dispatches"]
        events = {e.kind: e for e in await _events(db_session, monitor["id"])}
        assert (events["recovered"].dispatch_id, events["recovered"].dispatch_status) == (
            recovery["id"],
            "succeeded",
        )
        assert (events["alert"].dispatch_id, events["alert"].dispatch_status) == (
            report["id"],
            "failed",
        )


async def _open_fault(
    db_session, monitor_id: str, *, report: tuple[timedelta, str] | None, key=None
) -> None:
    """A fault open since a day ago, its latest report *report* = (age, status) ago."""
    now = datetime.now(UTC)
    row = await _row(db_session, monitor_id)
    row.state = "ok"
    row.last_status = "alert"
    row.last_checkin_at = now - timedelta(minutes=5)
    row.fault_since = now - timedelta(days=1)
    row.fault_key = key
    if report is not None:
        age, status = report
        db_session.add(
            MonitorEvent(monitor_id=monitor_id, kind="alert", at=now - age, dispatch_status=status)
        )
    await db_session.flush()


def _calls(notifier) -> int:
    """Every request notifier received, of any kind."""
    return len(notifier.mock.calls)


class TestFaults:
    """One report per fault, a reminder per renotify, a notice when it clears
    (#28). A suppressed ``alert`` is still recorded, and costs notifier nothing."""

    async def test_a_repeat_inside_a_fault_is_recorded_and_suppressed(
        self, api, headers, monitor, notifier, db_session
    ):
        await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        before = _calls(notifier)

        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "y"})

        assert response.status_code == 202
        assert response.json()["dispatches"] == []
        assert _calls(notifier) == before  # no health, preview or dispatch
        events = await _events(db_session, monitor["id"])
        assert [(e.kind, e.dispatch_status) for e in events] == [
            ("first_checkin", None),
            ("alert", "succeeded"),
            ("alert", None),
        ]
        row = await _row(db_session, monitor["id"])
        assert row.last_variables == {"source": "y"}  # recorded all the same

    async def test_the_fault_opens_with_its_first_alert(self, api, headers, monitor, db_session):
        body = (
            await _checkin(
                api, headers, monitor, status="alert", metadata={"fault": {"kind": "lag"}}
            )
        ).json()
        row = await _row(db_session, monitor["id"])
        assert row.fault_since == datetime.fromisoformat(body["last_checkin_at"])
        assert row.fault_key == {"kind": "lag"}

    async def test_another_fault_key_reports_at_once(
        self, api, headers, monitor, notifier, db_session
    ):
        """The consumer's opt-in (F2): processor's ``lag`` → ``off_main``."""
        await _checkin(api, headers, monitor, status="alert", metadata={"fault": "lag"})
        first = (await _row(db_session, monitor["id"])).fault_since
        response = await _checkin(
            api, headers, monitor, status="alert", metadata={"fault": "off_main"}
        )
        assert len(response.json()["dispatches"]) == 1
        assert [d["metadata"]["fault"] for d in notifier.dispatched()] == ["lag", "off_main"]
        row = await _row(db_session, monitor["id"])
        assert row.fault_key == "off_main"
        assert row.fault_since > first

    async def test_the_same_fault_key_is_the_same_fault(self, api, headers, monitor, notifier):
        for _ in range(3):
            await _checkin(api, headers, monitor, status="alert", metadata={"fault": "lag"})
        assert len(notifier.dispatched()) == 1

    @pytest.mark.parametrize(("ago", "sent"), [(58, True), (57, False)], ids=["due", "early"])
    async def test_a_reminder_goes_on_the_checkin_nearest_renotify(
        self, api, headers, notifier, db_session, ago, sent
    ):
        """co-observo-live: 300 s ticks, hourly renotify. Due from 3450 s."""
        created = await _create(api, headers, interval_seconds=300, renotify_seconds=3600)
        await _open_fault(db_session, created["id"], report=(timedelta(minutes=ago), "succeeded"))
        response = await _checkin(api, headers, created, status="alert")
        assert len(response.json()["dispatches"]) == int(sent)
        assert len(notifier.dispatched()) == int(sent)

    @pytest.mark.parametrize("status", ["not_accepted", "failed"])
    async def test_a_report_nobody_heard_is_resent(
        self, api, headers, monitor, notifier, db_session, status
    ):
        """Late, never lost (F4), with the variables of the check-in that sends it."""
        await _open_fault(db_session, monitor["id"], report=(timedelta(minutes=10), status))
        response = await _checkin(
            api, headers, monitor, status="alert", variables={"source": "now"}
        )
        (sent,) = notifier.dispatched()
        assert sent["variables"] == {"source": "now"}
        assert len(response.json()["dispatches"]) == 1

    async def test_a_partial_report_was_heard(self, api, headers, monitor, notifier, db_session):
        await _open_fault(db_session, monitor["id"], report=(timedelta(minutes=10), "partial"))
        await _checkin(api, headers, monitor, status="alert")
        assert not notifier.dispatch.called

    async def test_a_preview_failure_at_the_crossing_is_resent_next_tick(
        self, api, headers, monitor, notifier, db_session
    ):
        notifier.preview.mock(side_effect=httpx.ConnectError("refused"))
        await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        notifier.preview.mock(side_effect=None)
        notifier.preview.respond(json={"title": "t", "body": "b"})

        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})

        assert len(response.json()["dispatches"]) == 1
        events = await _events(db_session, monitor["id"])
        assert [e.dispatch_status for e in events if e.kind == "alert"] == [
            "not_accepted",
            "succeeded",
        ]

    async def test_an_ok_clears_the_fault_and_says_so(
        self, api, headers, monitor, notifier, db_session
    ):
        await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        expected_key = cleared_key(await _row(db_session, monitor["id"]))

        response = await _checkin(api, headers, monitor, status="ok")

        (dispatch,) = response.json()["dispatches"]
        sent = notifier.dispatched()[-1]
        assert sent["title_template"] == CLEARED_TITLE
        assert sent["variables"]["name"] == monitor["name"]
        assert sent["idempotency_key"] == expected_key
        assert sent["metadata"] == {"monitor_id": monitor["id"], "reason": "cleared"}
        cleared = (await _events(db_session, monitor["id"]))[-1]
        assert (cleared.kind, cleared.dispatch_id, cleared.dispatch_status) == (
            "cleared",
            dispatch["id"],
            "succeeded",
        )
        row = await _row(db_session, monitor["id"])
        assert (row.fault_since, row.fault_key) == (None, None)

    async def test_the_next_alert_after_a_clear_is_a_new_fault(
        self, api, headers, monitor, notifier
    ):
        for status in ("alert", "ok", "alert"):
            await _checkin(api, headers, monitor, status=status)
        reasons = [d["metadata"]["reason"] for d in notifier.dispatched()]
        assert reasons == ["report", "cleared", "report"]

    async def test_a_cleared_notice_notifier_refuses_is_not_accepted(
        self, api, headers, monitor, notifier, db_session
    ):
        await _checkin(api, headers, monitor, status="alert")
        notifier.refusing("cleared")
        response = await _checkin(api, headers, monitor, status="ok")
        assert response.json()["dispatches"] == []
        cleared = (await _events(db_session, monitor["id"]))[-1]
        assert (cleared.kind, cleared.dispatch_status) == ("cleared", "not_accepted")
        assert (await _row(db_session, monitor["id"])).fault_since is None

    async def test_an_ok_with_no_fault_open_sends_nothing(self, api, headers, monitor, notifier):
        await _checkin(api, headers, monitor, status="ok")
        await _checkin(api, headers, monitor, status="ok")
        assert _calls(notifier) == 1  # creation's channel check only

    async def test_a_monitor_with_no_channels_tracks_the_fault_and_owes_nothing(
        self, api, headers, notifier, db_session
    ):
        created = await _create(api, headers, channel_ids=[])
        await _checkin(api, headers, created, status="alert")
        assert (await _row(db_session, created["id"])).fault_since is not None
        await _checkin(api, headers, created, status="ok")
        assert _calls(notifier) == 0
        events = await _events(db_session, created["id"])
        assert [(e.kind, e.dispatch_status) for e in events] == [
            ("first_checkin", None),
            ("alert", None),
            ("cleared", None),
        ]

    async def test_a_disabled_monitor_is_suppressed_alike(self, api, headers, monitor, notifier):
        await api.patch(
            f"/api/v1/monitors/{monitor['id']}", headers=headers, json={"enabled": False}
        )
        for _ in range(2):
            await _checkin(api, headers, monitor, status="alert")
        assert len(notifier.dispatched()) == 1


class TestFaultsAndOutages:
    """Silence never ends a fault (F7): every fault reported gets its end."""

    async def _alert_then_silence(self, api, headers, monitor, db_session) -> None:
        await _checkin(api, headers, monitor, status="alert", metadata={"fault": "lag"})
        await _make_missing(db_session, monitor["id"])

    async def test_back_with_ok_is_recovered_and_cleared(
        self, api, headers, monitor, notifier, db_session
    ):
        await self._alert_then_silence(api, headers, monitor, db_session)
        response = await _checkin(api, headers, monitor, status="ok")
        reasons = [d["metadata"]["reason"] for d in notifier.dispatched()]
        assert reasons == ["report", "recovered", "cleared"]
        assert len(response.json()["dispatches"]) == 2

    async def test_back_in_the_same_fault_is_recovered_only(
        self, api, headers, monitor, notifier, db_session
    ):
        await self._alert_then_silence(api, headers, monitor, db_session)
        response = await _checkin(api, headers, monitor, status="alert", metadata={"fault": "lag"})
        reasons = [d["metadata"]["reason"] for d in notifier.dispatched()]
        assert reasons == ["report", "recovered"]
        assert len(response.json()["dispatches"]) == 1
        assert (await _row(db_session, monitor["id"])).fault_since is not None

    async def test_back_with_another_fault_is_recovered_and_reported(
        self, api, headers, monitor, notifier, db_session
    ):
        await self._alert_then_silence(api, headers, monitor, db_session)
        response = await _checkin(
            api, headers, monitor, status="alert", metadata={"fault": "off_main"}
        )
        reasons = [d["metadata"]["reason"] for d in notifier.dispatched()]
        assert reasons == ["report", "recovered", "report"]
        assert len(response.json()["dispatches"]) == 2


def _post_raw(api, headers, url: str, body: str, method: str = "POST"):
    """Send *body* as written: httpx's own encoder refuses ``NaN``, a client's may not."""
    return api.request(
        method, url, headers={**headers, "Content-Type": "application/json"}, content=body
    )


class TestNonFiniteNumbers:
    """``NaN``, ``Infinity`` and ``-Infinity`` are not JSON (RFC 8259 § 6), but
    Python's parser takes them. Each is a 422 naming the field, before anything
    is written or sent (#31): Postgres ``jsonb`` and httpx's encoder both refuse
    them, which was a 500 and a check-in never recorded."""

    async def _assert_untouched(self, db_session, monitor, notifier) -> None:
        row = await _row(db_session, monitor["id"])
        assert (row.state, row.last_checkin_at, row.last_status) == ("pending", None, None)
        assert row.last_variables == {}
        assert (row.fault_since, row.fault_key) == (None, None)
        assert await _events(db_session, monitor["id"]) == []
        assert not notifier.preview.called
        assert not notifier.dispatch.called

    async def test_a_nan_fault_key_is_422_and_records_nothing(
        self, api, headers, monitor, notifier, db_session
    ):
        """The issue's first body: was httpx's ValueError on the way to preview."""
        response = await _post_raw(
            api,
            headers,
            f"/api/v1/monitors/{monitor['id']}/checkin",
            '{"status":"alert","metadata":{"fault":NaN}}',
        )
        assert response.status_code == 422, response.text
        (error,) = response.json()["detail"]
        assert error["loc"] == ["body", "metadata"]
        assert "metadata.fault" in error["msg"]
        await self._assert_untouched(db_session, monitor, notifier)

    async def test_a_nan_variable_is_422_and_records_nothing(
        self, api, headers, monitor, notifier, db_session
    ):
        """The issue's second body: was Postgres refusing ``last_variables``."""
        response = await _post_raw(
            api,
            headers,
            f"/api/v1/monitors/{monitor['id']}/checkin",
            '{"status":"ok","variables":{"x":NaN}}',
        )
        assert response.status_code == 422, response.text
        (error,) = response.json()["detail"]
        assert error["loc"] == ["body", "variables"]
        assert "variables.x" in error["msg"]
        await self._assert_untouched(db_session, monitor, notifier)

    @pytest.mark.parametrize(
        ("body", "field", "path"),
        [
            # An alert on a new monitor reaches every sink: preview and dispatch
            # (variables, metadata), the row (last_variables, fault_key) and the
            # first_checkin and alert events.
            ('{"status":"alert","variables":{"source":NaN}}', "variables", "variables.source"),
            (
                '{"status":"alert","variables":{"findings":[{"depth":Infinity}]}}',
                "variables",
                "variables.findings[0].depth",
            ),
            ('{"status":"alert","metadata":{"run":-Infinity}}', "metadata", "metadata.run"),
            ('{"status":"alert","metadata":{"fault":[1,NaN]}}', "metadata", "metadata.fault[1]"),
            # Valid JSON, but past a double's range: Python reads it as inf.
            ('{"status":"alert","variables":{"big":1e400}}', "variables", "variables.big"),
        ],
    )
    async def test_reaches_no_sink(
        self, api, headers, monitor, notifier, db_session, body, field, path
    ):
        response = await _post_raw(api, headers, f"/api/v1/monitors/{monitor['id']}/checkin", body)
        assert response.status_code == 422, response.text
        (error,) = response.json()["detail"]
        assert error["loc"] == ["body", field]
        assert path in error["msg"]
        await self._assert_untouched(db_session, monitor, notifier)

    async def test_a_monitor_keeps_its_last_good_report(
        self, api, headers, monitor, notifier, db_session
    ):
        await _checkin(api, headers, monitor, status="alert", variables={"n": 1})
        before = _calls(notifier)
        response = await _post_raw(
            api,
            headers,
            f"/api/v1/monitors/{monitor['id']}/checkin",
            '{"status":"ok","variables":{"n":NaN}}',
        )
        assert response.status_code == 422
        assert _calls(notifier) == before
        row = await _row(db_session, monitor["id"])
        assert (row.last_status, row.last_variables) == ("alert", {"n": 1})
        assert row.fault_since is not None  # the 422 cleared nothing

    async def test_a_finite_float_is_recorded(self, api, headers, monitor, db_session):
        response = await _post_raw(
            api,
            headers,
            f"/api/v1/monitors/{monitor['id']}/checkin",
            '{"variables":{"a":1.5,"b":-0.0,"c":1e308}}',
        )
        assert response.status_code == 202, response.text
        row = await _row(db_session, monitor["id"])
        # jsonb keeps numbers as numeric: 1e308 comes back an int of equal value.
        assert {k: float(v) for k, v in row.last_variables.items()} == {
            "a": 1.5,
            "b": -0.0,
            "c": 1e308,
        }

    @pytest.mark.parametrize(
        "body",
        [
            '{"status":NaN}',
            "NaN",
            '{"variables":Infinity}',
        ],
    )
    async def test_a_non_finite_input_still_gets_its_422(
        self, api, headers, monitor, notifier, db_session, body
    ):
        """FastAPI's 422 echoes the input, and its JSON response refuses ``NaN``:
        rejected anywhere else in the body, it was a 500 too."""
        response = await _post_raw(api, headers, f"/api/v1/monitors/{monitor['id']}/checkin", body)
        assert response.status_code == 422, response.text
        await self._assert_untouched(db_session, monitor, notifier)

    @pytest.mark.parametrize(
        "variables",
        [
            '{"x":' + "[" * 990 + "NaN" + "]" * 990 + "}",
            "[" * 990 + '"not an object"' + "]" * 990,
        ],
        ids=["nan", "wrong-type"],
    )
    async def test_a_422_too_deep_to_echo_is_still_a_422(
        self, api, headers, monitor, notifier, db_session, variables
    ):
        """Nested deeper than FastAPI's encoder can recurse from a handler, the
        422 leaves the input out rather than become a 500.

        990 sits in a window: under the C recursion limit ``json.loads`` parses
        within (about 10,000 on CPython 3.12), over the Python one
        ``jsonable_encoder`` recurses within (1,000). Should a Python upgrade
        move either, ``tests/api/test_validation.py`` still holds the fallback.
        """
        response = await _post_raw(
            api,
            headers,
            f"/api/v1/monitors/{monitor['id']}/checkin",
            '{"variables":' + variables + "}",
        )
        assert response.status_code == 422, response.text
        (error,) = response.json()["detail"]
        assert error["loc"] == ["body", "variables"]
        assert "input" not in error
        await self._assert_untouched(db_session, monitor, notifier)

    @pytest.mark.parametrize(
        "body",
        [
            '{"name":"m","interval_seconds":NaN,"title_template":"T","body_template":"B"}',
            '{"name":"m","interval_seconds":60,"title_template":"T","body_template":"B",'
            '"extra":Infinity}',
        ],
    )
    async def test_create_refuses_one(self, api, headers, body):
        """No create field takes free-form JSON; a ``NaN`` is a 422 all the same."""
        response = await _post_raw(api, headers, "/api/v1/monitors", body)
        assert response.status_code == 422, response.text

    @pytest.mark.parametrize("body", ['{"grace_seconds":-Infinity}', '{"enabled":NaN}'])
    async def test_update_refuses_one(self, api, headers, monitor, body):
        response = await _post_raw(
            api, headers, f"/api/v1/monitors/{monitor['id']}", body, method="PATCH"
        )
        assert response.status_code == 422, response.text


def _unreachable(notifier) -> None:
    notifier.dispatch.mock(side_effect=httpx.ConnectError("refused"))


def _health_unreachable(notifier) -> None:
    notifier.health.mock(side_effect=httpx.ConnectError("refused"))


def _wrong_environment(notifier) -> None:
    notifier.health.respond(json={"status": "ok", "environment": "production"})


def _key_revoked(notifier) -> None:
    notifier.dispatch.respond(401, json={"detail": "invalid key"})


def _refused(notifier) -> None:
    notifier.dispatch.respond(422, json={"detail": "no"})


def _channels_deleted(notifier) -> None:
    notifier.dispatch.respond(404, json={"detail": {"message": "gone", "channel_ids": CHANNELS}})


#: Every way a check-in's notice can go without a dispatch record (#19).
NOT_TAKEN = [
    _unreachable,
    _health_unreachable,
    _wrong_environment,
    _key_revoked,
    _refused,
    _channels_deleted,
]


class TestNotAccepted:
    """A notice notifier never took is ``not_accepted`` on its event (#19):
    should have sent, did not, so the sweep surfaces it beside #8's."""

    @pytest.mark.parametrize("fail", NOT_TAKEN)
    async def test_a_report_notifier_never_took(
        self, api, headers, monitor, notifier, db_session, fail
    ):
        fail(notifier)
        response = await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        assert response.status_code == 202
        assert response.json()["dispatches"] == []
        alert = (await _events(db_session, monitor["id"]))[-1]
        assert (alert.kind, alert.dispatch_id, alert.dispatch_status) == (
            "alert",
            None,
            "not_accepted",
        )

    @pytest.mark.parametrize("fail", NOT_TAKEN)
    async def test_a_recovery_notifier_never_took(
        self, api, headers, monitor, notifier, db_session, fail
    ):
        await _make_missing(db_session, monitor["id"])
        fail(notifier)
        response = await _checkin(api, headers, monitor)
        assert response.status_code == 202
        assert response.json()["state"] == "ok"
        recovered = (await _events(db_session, monitor["id"]))[-1]
        assert (recovered.kind, recovered.dispatch_id, recovered.dispatch_status) == (
            "recovered",
            None,
            "not_accepted",
        )

    async def test_a_lost_recovery_does_not_mark_the_delivered_report(
        self, api, headers, monitor, notifier, db_session
    ):
        await _make_missing(db_session, monitor["id"])
        notifier.refusing("recovered")
        await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        events = {e.kind: e for e in await _events(db_session, monitor["id"])}
        assert events["recovered"].dispatch_status == "not_accepted"
        assert events["alert"].dispatch_status == "succeeded"

    async def test_a_monitor_with_no_channels_had_nothing_to_send(
        self, api, headers, notifier, db_session
    ):
        """Nothing owed, so nothing lost: as the sweep's ``undeliverable``."""
        created = await _create(api, headers, channel_ids=[])
        await _make_missing(db_session, created["id"])
        await _checkin(api, headers, created, status="alert", variables={"source": "x"})
        events = {e.kind: e for e in await _events(db_session, created["id"])}
        assert (events["recovered"].dispatch_id, events["recovered"].dispatch_status) == (
            None,
            None,
        )
        assert (events["alert"].dispatch_id, events["alert"].dispatch_status) == (None, None)

    async def test_a_plain_checkin_records_no_status(
        self, api, headers, monitor, notifier, db_session
    ):
        """Nothing warranted, so nothing to mark, even with notifier down."""
        _health_unreachable(notifier)
        await _checkin(api, headers, monitor)
        (first,) = await _events(db_session, monitor["id"])
        assert (first.kind, first.dispatch_status) == ("first_checkin", None)


async def _notice(db_session, monitor_id: str, kind: str, status: str | None, ago: timedelta):
    """A check-in notice's event, *ago* in the past, as the route writes it."""
    at = datetime.now(UTC) - ago
    db_session.add(MonitorEvent(monitor_id=monitor_id, kind=kind, at=at, dispatch_status=status))
    await db_session.flush()
    return at


def _served(body: dict, notice: str) -> tuple[datetime | None, str | None]:
    at = body[f"last_{notice}_at"]
    return (datetime.fromisoformat(at) if at else None, body[f"last_{notice}_status"])


class TestNoticeStatus:
    """The owner sees what became of its last recovery and report (#20),
    as it sees ``last_alert_status`` for the missing alert (#6)."""

    async def test_a_new_monitor_has_sent_none(self, api, headers, monitor):
        for notice in ("report", "recovery", "cleared"):
            assert _served(monitor, notice) == (None, None)
        assert (monitor["fault_since"], monitor["fault_key"]) == (None, None)

    async def test_an_open_fault_is_served_on_every_route(self, api, headers, monitor, db_session):
        """#28: the owner can see a fault is open, and under which key."""
        body = (
            await _checkin(api, headers, monitor, status="alert", metadata={"fault": ["lag", 1]})
        ).json()
        url = f"/api/v1/monitors/{monitor['id']}"
        served = [
            (await api.get(url, headers=headers)).json(),
            (await api.get("/api/v1/monitors", headers=headers)).json()[0],
            (await api.patch(url, headers=headers, json={"grace_seconds": 60})).json(),
        ]
        for one in served:
            assert datetime.fromisoformat(one["fault_since"]) == datetime.fromisoformat(
                body["last_checkin_at"]
            )
            assert one["fault_key"] == ["lag", 1]

    async def test_a_cleared_notice_and_its_fate(self, api, headers, monitor, notifier, db_session):
        await _checkin(api, headers, monitor, status="alert")
        notifier.delivering(cleared="partial")
        await _checkin(api, headers, monitor, status="ok")
        body = (await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)).json()
        cleared = (await _events(db_session, monitor["id"]))[-1]
        assert _served(body, "cleared") == (cleared.at, "partial")
        assert (body["fault_since"], body["fault_key"]) == (None, None)
        assert _served(body, "report")[1] == "succeeded"  # each kind its own

    async def test_a_report_that_failed(self, api, headers, monitor, notifier, db_session):
        notifier.delivering("failed")
        await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        body = (await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)).json()
        (alert,) = [e for e in await _events(db_session, monitor["id"]) if e.kind == "alert"]
        assert _served(body, "report") == (alert.at, "failed")
        assert _served(body, "recovery") == (None, None)

    @pytest.mark.parametrize("fail", NOT_TAKEN)
    async def test_a_report_notifier_never_took(self, api, headers, monitor, notifier, fail):
        """#19's own value, served as is."""
        fail(notifier)
        await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        body = (await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)).json()
        assert _served(body, "report")[1] == "not_accepted"

    async def test_a_recovery_and_a_report_each_their_own(
        self, api, headers, monitor, notifier, db_session
    ):
        await _make_missing(db_session, monitor["id"])
        notifier.delivering(recovered="partial", report="succeeded")
        await _checkin(api, headers, monitor, status="alert", variables={"source": "x"})
        body = (await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)).json()
        assert _served(body, "recovery")[1] == "partial"
        assert _served(body, "report")[1] == "succeeded"

    async def test_the_latest_of_each_kind(self, api, headers, monitor, db_session):
        mid = monitor["id"]
        await _notice(db_session, mid, "alert", "failed", timedelta(hours=2))
        report_at = await _notice(db_session, mid, "alert", "succeeded", timedelta(hours=1))
        recovery_at = await _notice(db_session, mid, "recovered", "failed", timedelta(hours=3))
        await _notice(db_session, mid, "recovered", "succeeded", timedelta(hours=4))
        body = (await api.get(f"/api/v1/monitors/{mid}", headers=headers)).json()
        assert _served(body, "report") == (report_at, "succeeded")
        assert _served(body, "recovery") == (recovery_at, "failed")

    async def test_a_later_notice_with_no_status_does_not_hide_one(
        self, api, headers, monitor, db_session
    ):
        """Null owed nothing (no channels) or predates #8: the sweep's rule."""
        mid = monitor["id"]
        failed_at = await _notice(db_session, mid, "alert", "failed", timedelta(hours=2))
        await _notice(db_session, mid, "alert", None, timedelta(hours=1))
        body = (await api.get(f"/api/v1/monitors/{mid}", headers=headers)).json()
        assert _served(body, "report") == (failed_at, "failed")

    async def test_whatever_its_age(self, api, headers, monitor, db_session):
        """No 24-hour window: that bounds the operator's check, not the owner's view."""
        at = await _notice(db_session, monitor["id"], "alert", "failed", timedelta(days=30))
        body = (await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)).json()
        assert _served(body, "report") == (at, "failed")

    async def test_a_missing_alert_is_not_a_notice(self, api, headers, monitor, db_session):
        """The sweep's ``missing`` events carry a status too; that is ``last_alert_status``."""
        await _notice(db_session, monitor["id"], "missing", "failed", timedelta(hours=1))
        body = (await api.get(f"/api/v1/monitors/{monitor['id']}", headers=headers)).json()
        assert _served(body, "report") == (None, None)
        assert _served(body, "recovery") == (None, None)
        assert _served(body, "cleared") == (None, None)

    async def test_listed_each_with_its_own(self, api, headers, monitor, db_session):
        other = await _create(api, headers)
        at = await _notice(db_session, monitor["id"], "alert", "partial", timedelta(hours=1))
        listed = {m["id"]: m for m in (await api.get("/api/v1/monitors", headers=headers)).json()}
        assert _served(listed[monitor["id"]], "report") == (at, "partial")
        assert _served(listed[other["id"]], "report") == (None, None)

    async def test_the_owner_is_told_which_last_is_which(self):
        """The owner reads OpenAPI, not our comments: ``last_report_*`` is not
        the latest check-in, and the statuses are named there (CR 7)."""
        described = app.openapi()["components"]["schemas"]["MonitorOut"]["description"]
        for name in ("last_status", "last_alert_status", "last_report_", "not_accepted"):
            assert name in described

    async def test_each_notice_is_one_probe_of_its_index(self, db_session):
        """The query must match ``ix_monitor_events_notice``'s predicate, or
        Postgres falls back to walking each monitor's history and every
        other test here still passes (CR 4)."""
        stmt = _with_notices().where(Monitor.tenant_id == "01J0000000000000000000TENT")
        sql = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        await db_session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(row[0] for row in await db_session.execute(text(f"EXPLAIN {sql}")))
        assert plan.count("Index Scan Backward using ix_monitor_events_notice") == 3, plan

    async def test_the_faults_last_report_is_one_probe_of_the_index(self, db_session):
        """Read on every ``alert`` inside a fault (#28): a 5-minute probe in a
        week-long fault must not walk 2,000 events per check-in."""
        stmt = _fault_report_query("01J0000000000000000000MONI", datetime.now(UTC))
        sql = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        await db_session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(row[0] for row in await db_session.execute(text(f"EXPLAIN {sql}")))
        assert "Index Scan Backward using ix_monitor_events_notice" in plan, plan

    async def test_an_update_answers_from_one_moment(self, api, headers, monitor, db_session):
        """The monitor's columns are re-read with its notices, not left as the
        session last saw them: a check-in landing meanwhile shows in both (CR 1)."""
        await _row(db_session, monitor["id"])  # the session now holds the monitor
        await db_session.execute(
            update(Monitor).where(Monitor.id == monitor["id"]).values(last_status="alert"),
            execution_options={"synchronize_session": False},
        )
        response = await api.patch(
            f"/api/v1/monitors/{monitor['id']}", headers=headers, json={"name": "renamed"}
        )
        assert response.json()["last_status"] == "alert"

    async def test_an_update_answers_with_them(self, api, headers, monitor, db_session):
        at = await _notice(db_session, monitor["id"], "recovered", "failed", timedelta(hours=1))
        response = await api.patch(
            f"/api/v1/monitors/{monitor['id']}", headers=headers, json={"name": "renamed"}
        )
        assert _served(response.json(), "recovery") == (at, "failed")


class TestTenantIsolation:
    async def test_another_tenant_cannot_see_or_check_in(self, api, monitor, db_session):
        """The check-in URL is guessable from a ULID; ownership is the guard."""
        other = Tenant(name=f"other-{secrets.token_hex(4)}")
        db_session.add(other)
        await db_session.flush()
        _, raw = await mint(db_session, other.id, "other", "production")
        intruder = {HEADER: raw}

        assert (await api.get("/api/v1/monitors", headers=intruder)).json() == []
        assert (
            await api.get(f"/api/v1/monitors/{monitor['id']}", headers=intruder)
        ).status_code == 404
        assert (await _checkin(api, intruder, monitor)).status_code == 404
