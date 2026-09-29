"""Tests for src/core/heartbeat.py — the pings that watch the sweep (#1).

healthchecks.io is reached through respx, never the network. What matters:
the right check gets the right signal, the ping key never reaches a log line,
and nothing a ping does can fail the sweep that sent it.
"""

import json
import logging

import httpx
import pytest
import respx

from src.core.heartbeat import (
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


@pytest.fixture
def pings():
    """healthchecks.io: every ping answered ``OK``, until a test says not."""
    with respx.mock(base_url=PING_BASE_URL, assert_all_called=False) as mock:
        mock.post(url__regex=r".*").respond(200, text="OK")
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
        assert body == {"checked": 3, "alerted": 1, "owed": 0, "undeliverable": 0}

    async def test_notifier_unreachable_fails_the_notifier_check(self, pings):
        await Heartbeat(KEY).sweep_completed(_report(notifier_ok=False))
        assert _paths(pings) == [f"/{KEY}/{SWEEP_CHECK}", f"/{KEY}/{NOTIFIER_CHECK}/fail"]

    async def test_an_owed_alert_fails_the_notifier_check(self, pings):
        """notifier answered /health but did not take the alert: not reachable enough."""
        await Heartbeat(KEY).sweep_completed(_report(owed=["01J1"]))
        assert _paths(pings)[1] == f"/{KEY}/{NOTIFIER_CHECK}/fail"
        assert b"1 alert(s) owed" in pings.calls[1].request.content


class TestSweepFailed:
    async def test_fails_the_sweep_check_only(self, pings):
        await Heartbeat(KEY).sweep_failed(RuntimeError("boom"))
        assert _paths(pings) == [f"/{KEY}/{SWEEP_CHECK}/fail"]

    async def test_sends_the_exception_type_not_its_message(self, pings):
        """An exception message can carry SQL, monitor names or a URL; the type cannot."""
        await Heartbeat(KEY).sweep_failed(RuntimeError("password=hunter2"))
        assert pings.calls[0].request.content == b"RuntimeError"


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
