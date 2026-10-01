"""Tests for src/core/heartbeat.py — the pings that watch the sweep (#1) and the API (#13).

healthchecks.io and the API's ``/ready`` are reached through respx, never the
network. What matters: the right check gets the right signal, the ping key
never reaches a log line, and nothing a ping does can fail the sweep that
sent it.
"""

import asyncio
import json
import logging
import time

import httpx
import pytest
import respx

from src.core import heartbeat
from src.core.heartbeat import (
    API_CHECK,
    API_READY_URL,
    CREDENTIAL_NAME,
    NOTIFIER_CHECK,
    PING_BASE_URL,
    SWEEP_CHECK,
    Heartbeat,
    heartbeat_from_environment,
)
from src.core.sweep import SweepReport

KEY = "pk_test_0123456789abcdef"
PROD_DB = "postgresql+asyncpg://status@localhost/status"
DEV_DB = "postgresql+asyncpg://status@localhost/status_dev"


@pytest.fixture(autouse=True)
def _restore_httpx_filters():
    """Each Heartbeat adds a filter to the process-wide httpx logger; take them back."""
    httpx_logger = logging.getLogger("httpx")
    saved = list(httpx_logger.filters)
    yield
    httpx_logger.filters[:] = saved


@pytest.fixture
def pings():
    """healthchecks.io: every ping answered ``OK``, until a test says not."""
    with respx.mock(base_url=PING_BASE_URL, assert_all_called=False) as mock:
        mock.post(url__regex=r".*").respond(200, text="OK")
        yield mock


#: ``/ready`` from the production API, as it answers today.
READY = {
    "status": "ready",
    "db": True,
    "database": "status",
    "environment": "production",
    "schema_state": "current",
}


@pytest.fixture
def api(monkeypatch):
    """The production API's ``/ready``: ready, until a test says not. A short window."""
    monkeypatch.setattr(heartbeat, "API_WINDOW_SECONDS", 0.2)
    monkeypatch.setattr(heartbeat, "API_RETRY_SECONDS", 0.01)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(API_READY_URL, name="ready").respond(200, json=READY)
        yield mock


def _paths(mock) -> list[str]:
    return [call.request.url.path for call in mock.calls]


def _report(**fields) -> SweepReport:
    return SweepReport(checked=3, **fields)


class TestSweepCompleted:
    async def test_pings_both_checks_success(self, pings):
        await Heartbeat(KEY).sweep_completed(_report())
        assert _paths(pings) == [f"/{KEY}/{SWEEP_CHECK}", f"/{KEY}/{NOTIFIER_CHECK}"]

    async def test_the_sweep_ping_carries_the_counts_only(self, pings):
        await Heartbeat(KEY).sweep_completed(_report(alerted=["01J1"], owed=[]))
        body = json.loads(pings.calls[0].request.content)
        assert body == {
            "checked": 3,
            "alerted": 1,
            "owed": 0,
            "undeliverable": 0,
            "undelivered": 0,
        }

    async def test_notifier_unreachable_fails_the_notifier_check(self, pings):
        await Heartbeat(KEY).sweep_completed(_report(notifier_ok=False))
        assert _paths(pings) == [f"/{KEY}/{SWEEP_CHECK}", f"/{KEY}/{NOTIFIER_CHECK}/fail"]

    async def test_an_owed_alert_fails_the_notifier_check(self, pings):
        """notifier answered /health but did not take the alert: not reachable enough."""
        await Heartbeat(KEY).sweep_completed(_report(owed=["01J1"]))
        assert _paths(pings)[1] == f"/{KEY}/{NOTIFIER_CHECK}/fail"
        assert b"1 alert(s) owed" in pings.calls[1].request.content

    async def test_an_undelivered_alert_fails_the_notifier_check(self, pings):
        """notifier took the alert and could not deliver it (#6)."""
        await Heartbeat(KEY).sweep_completed(
            _report(undelivered={"01J1": "failed", "01J2": "partial", "01J3": "failed"})
        )
        assert _paths(pings)[1] == f"/{KEY}/{NOTIFIER_CHECK}/fail"
        assert pings.calls[1].request.content == b"3 alert(s) undelivered (2 failed, 1 partial)"

    async def test_unreachable_does_not_hide_earlier_undelivered_alerts(self, pings):
        """Fixing the outage will not deliver them; the body must still say so."""
        await Heartbeat(KEY).sweep_completed(
            _report(notifier_ok=False, undelivered={"01J1": "failed"})
        )
        assert pings.calls[1].request.content == (
            b"notifier unreachable at sweep start; 1 alert(s) undelivered (1 failed)"
        )

    async def test_owed_and_undelivered_are_both_named(self, pings):
        await Heartbeat(KEY).sweep_completed(_report(owed=["01J1"], undelivered={"01J2": "failed"}))
        assert pings.calls[1].request.content == (
            b"1 alert(s) owed; 1 alert(s) undelivered (1 failed)"
        )


class TestSweepFailed:
    async def test_fails_the_sweep_check_only(self, pings):
        await Heartbeat(KEY).sweep_failed(RuntimeError("boom"))
        assert _paths(pings) == [f"/{KEY}/{SWEEP_CHECK}/fail"]

    async def test_sends_the_exception_type_not_its_message(self, pings):
        """An exception message can carry SQL, monitor names or a URL; the type cannot."""
        await Heartbeat(KEY).sweep_failed(RuntimeError("password=hunter2"))
        assert pings.calls[0].request.content == b"RuntimeError"


class TestApiChecked:
    """#13: the production API, by the name consumers use, answers ``/ready``."""

    async def test_a_ready_api_pings_success_with_its_answer(self, pings, api):
        await Heartbeat(KEY).api_checked()
        assert _paths(pings) == [f"/{KEY}/{API_CHECK}"]
        assert pings.calls[0].request.content == b"200 " + api.calls[0].response.content

    async def test_not_ready_fails_the_api_check_with_its_answer(self, pings, api):
        """503: the database, or a schema behind the code. The body says which."""
        api.routes["ready"].respond(503, json={"status": "not_ready", "db": False})
        await Heartbeat(KEY).api_checked()
        assert _paths(pings) == [f"/{KEY}/{API_CHECK}/fail"]
        assert pings.calls[0].request.content.startswith(b"503 ")
        assert b"not_ready" in pings.calls[0].request.content

    async def test_another_environment_is_not_ready(self, pings, api):
        """:9000 recording check-ins where the production sweep never reads them."""
        api.routes["ready"].respond(
            200, json={**READY, "database": "status_dev", "environment": "development"}
        )
        await Heartbeat(KEY).api_checked()
        assert _paths(pings) == [f"/{KEY}/{API_CHECK}/fail"]
        assert b"development" in pings.calls[0].request.content

    @pytest.mark.parametrize("body", ["<html>bad gateway</html>", "[]"])
    async def test_an_answer_that_is_not_readys_is_not_ready(self, pings, api, body):
        api.routes["ready"].respond(200, text=body)
        await Heartbeat(KEY).api_checked()
        assert _paths(pings) == [f"/{KEY}/{API_CHECK}/fail"]

    async def test_a_long_answer_is_cut_short(self, pings, api):
        api.routes["ready"].respond(500, text="x" * 10_000)
        await Heartbeat(KEY).api_checked()
        assert len(pings.calls[0].request.content) < 300

    @pytest.mark.parametrize(
        ("error", "body"),
        [
            # What httpx says here with nothing listening, and with no MagicDNS.
            (httpx.ConnectError("All connection attempts failed"), b"ConnectError: All con"),
            (httpx.ConnectError("[Errno -2] Name or service not known"), b"ConnectError: [Errno"),
            (httpx.ConnectTimeout(""), b"ConnectTimeout"),
        ],
        ids=["refused", "unresolved", "silent"],
    )
    async def test_unreachable_throughout_sends_the_error(self, pings, api, error, body):
        """Retried until the window closes, then down. The URL carries no key, so the
        message goes too: it is what tells nothing listening from no name."""
        api.routes["ready"].mock(side_effect=error)
        await Heartbeat(KEY).api_checked()
        assert _paths(pings) == [f"/{KEY}/{API_CHECK}/fail"]
        assert pings.calls[0].request.content.startswith(body)
        assert pings.calls[0].request.content.endswith(str(error).encode() or body)
        assert api.routes["ready"].call_count > 1

    async def test_a_restart_inside_the_window_is_not_an_outage(self, pings, api):
        """2026-10-01 14:54:10: deploy.sh's forced pass ended 0.2 s before uvicorn listened."""
        api.routes["ready"].side_effect = [
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.Response(200, json=READY),
        ]
        await Heartbeat(KEY).api_checked()
        assert _paths(pings) == [f"/{KEY}/{API_CHECK}"]

    async def test_a_stalled_api_is_cut_off_at_the_window(self, pings, api):
        """The window bounds the whole probe, however many tries are still in flight."""

        async def stall(request):
            await asyncio.sleep(5)
            return httpx.Response(200, json=READY)

        api.routes["ready"].mock(side_effect=stall)
        started = time.monotonic()
        await Heartbeat(KEY).api_checked()
        assert time.monotonic() - started < 1
        assert _paths(pings) == [f"/{KEY}/{API_CHECK}/fail"]
        assert pings.calls[0].request.content == b"TimeoutError"

    async def test_a_try_cut_off_by_the_window_reports_the_hang(self, pings, api):
        """Not the try before it: the body describes how the window ended."""

        async def stall(request):
            await asyncio.sleep(5)
            return httpx.Response(200, json=READY)

        api.routes["ready"].side_effect = [httpx.Response(503, json={"db": False}), stall]
        await Heartbeat(KEY).api_checked()
        assert pings.calls[0].request.content == b"TimeoutError"

    async def test_not_ready_is_a_journal_warning_too(self, pings, api, caplog):
        api.routes["ready"].respond(503, json={"status": "not_ready", "db": False})
        with caplog.at_level("WARNING"):
            await Heartbeat(KEY).api_checked()
        assert any("not_ready" in r.getMessage() for r in caplog.records)

    async def test_a_failed_ping_never_raises(self, pings, api):
        pings.routes.clear()
        pings.post(url__regex=r".*").mock(side_effect=httpx.ConnectError("refused"))
        await Heartbeat(KEY).api_checked()


class TestAPingNeverFailsTheSweep:
    @pytest.mark.parametrize(
        "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")], ids=repr
    )
    async def test_a_network_error_is_logged_and_swallowed(self, pings, caplog, error):
        pings.routes.clear()
        pings.post(url__regex=r".*").mock(side_effect=error)
        with caplog.at_level("WARNING"):
            await Heartbeat(KEY).sweep_completed(_report())
        assert any("ping" in r.message for r in caplog.records)

    async def test_a_stalled_endpoint_is_cut_off_at_the_timeout(self, pings, caplog, monkeypatch):
        """The bound is on the whole ping; httpx's own timeout is per phase."""
        monkeypatch.setattr(heartbeat, "PING_TIMEOUT_SECONDS", 0.05)

        async def stall(request):
            await asyncio.sleep(5)
            return httpx.Response(200)

        pings.routes.clear()
        pings.post(url__regex=r".*").mock(side_effect=stall)
        started = time.monotonic()
        with caplog.at_level("WARNING"):
            await Heartbeat(KEY).sweep_failed(RuntimeError())
        assert time.monotonic() - started < 1
        assert any("TimeoutError" in r.message for r in caplog.records)

    async def test_a_non_2xx_answer_is_logged(self, pings, caplog):
        """404 is healthchecks' answer for a slug with no check behind it."""
        pings.routes.clear()
        pings.post(url__regex=r".*").respond(404, text="not found")
        with caplog.at_level("WARNING"):
            await Heartbeat(KEY).sweep_failed(RuntimeError())
        assert any("404" in r.message for r in caplog.records)

    async def test_the_key_never_reaches_httpxs_request_log(self, pings, caplog):
        """httpx logs every request's URL at INFO, and the URL holds the key."""
        with caplog.at_level("DEBUG"):
            await Heartbeat(KEY).sweep_completed(_report())
        assert any(r.name == "httpx" for r in caplog.records)
        assert all(KEY not in r.getMessage() for r in caplog.records)

    async def test_the_key_never_reaches_an_error_log_line(self, pings, caplog):
        pings.routes.clear()
        pings.post(url__regex=r".*").mock(
            side_effect=httpx.ConnectError(f"refused {PING_BASE_URL}/{KEY}/x")
        )
        with caplog.at_level("DEBUG"):
            await Heartbeat(KEY).sweep_completed(_report())
        assert caplog.records
        assert all(KEY not in r.getMessage() for r in caplog.records)


class TestHeartbeatFromEnvironment:
    def test_production_with_a_key(self, tmp_path):
        (tmp_path / CREDENTIAL_NAME).write_text(f"{KEY}\n")
        env = {"DATABASE_URL": PROD_DB, "CREDENTIALS_DIRECTORY": str(tmp_path)}
        assert isinstance(heartbeat_from_environment(env), Heartbeat)

    def test_production_without_a_key_warns(self, tmp_path, caplog):
        """The unit's newline fallback: the sweep runs, and says it is unwatched."""
        (tmp_path / CREDENTIAL_NAME).write_text("\n")
        env = {"DATABASE_URL": PROD_DB, "CREDENTIALS_DIRECTORY": str(tmp_path)}
        with caplog.at_level("WARNING"):
            assert heartbeat_from_environment(env) is None
        assert any(CREDENTIAL_NAME in r.message for r in caplog.records)

    def test_development_never_pings_even_with_a_key(self, tmp_path):
        (tmp_path / CREDENTIAL_NAME).write_text(KEY)
        env = {"DATABASE_URL": DEV_DB, "CREDENTIALS_DIRECTORY": str(tmp_path)}
        assert heartbeat_from_environment(env) is None

    def test_an_unparseable_database_url_never_pings(self, tmp_path):
        (tmp_path / CREDENTIAL_NAME).write_text(KEY)
        env = {"DATABASE_URL": "nonsense", "CREDENTIALS_DIRECTORY": str(tmp_path)}
        assert heartbeat_from_environment(env) is None


def test_the_redaction_is_installed_once_per_key():
    Heartbeat(KEY)
    Heartbeat(KEY)
    httpx_filters = logging.getLogger("httpx").filters
    assert sum(getattr(f, "key", None) == KEY for f in httpx_filters) == 1
