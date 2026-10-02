"""Tests for scripts/check_drift.py — the drift timer's python entrypoint (#12).

Thin by design: the verdict is tested in tests/core/test_drift.py. What is
only true here: the verdict reaches ``co-status-drift`` as its signal, every
run leaves a journal line, and a missing key costs the ping, never the run.
"""

from datetime import UTC, datetime

import pytest

from scripts import check_drift
from src.core import build
from src.core.drift import DRIFT_CHECK, Verdict
from src.core.heartbeat import CREDENTIAL_NAME, Signal

KEY = "pk_test_0123456789abcdef"


@pytest.fixture
def seen(monkeypatch):
    """What the script asked and pinged: ``assess`` and ``ping`` stand in."""
    calls: dict = {"pings": []}
    verdict = Verdict(Signal.FAIL, "live abc, main def: 1 commit ahead")

    async def assess(live, *, now):
        calls["live"], calls["now"] = live, now
        return verdict

    async def ping(key, check, signal, body):
        calls["pings"].append((key, check, signal, body))

    monkeypatch.setattr(check_drift, "assess", assess)
    monkeypatch.setattr(check_drift, "ping", ping)
    monkeypatch.setattr(build, "build_id", lambda: "abc")
    return calls


@pytest.fixture
def credentials(tmp_path):
    (tmp_path / CREDENTIAL_NAME).write_text(f"{KEY}\n")
    return {"CREDENTIALS_DIRECTORY": str(tmp_path)}


async def test_the_verdict_reaches_the_drift_check(seen, credentials):
    assert await check_drift.main(credentials) == 0
    assert seen["live"] == "abc"
    assert seen["pings"] == [(KEY, DRIFT_CHECK, Signal.FAIL, "live abc, main def: 1 commit ahead")]


async def test_the_clock_is_utc_now(seen, credentials):
    before = datetime.now(UTC)
    await check_drift.main(credentials)
    assert before <= seen["now"] <= datetime.now(UTC)


async def test_every_run_leaves_a_journal_line(seen, credentials, caplog):
    with caplog.at_level("INFO"):
        await check_drift.main(credentials)
    (record,) = [r for r in caplog.records if r.name == check_drift.logger.name]
    assert record.levelname == "WARNING", "a /fail verdict is a warning"
    assert "1 commit ahead" in record.getMessage()


async def test_without_a_key_it_still_runs_and_says_so(seen, tmp_path, caplog):
    (tmp_path / CREDENTIAL_NAME).write_text("\n")
    with caplog.at_level("WARNING"):
        assert await check_drift.main({"CREDENTIALS_DIRECTORY": str(tmp_path)}) == 0
    assert seen["pings"] == []
    assert any(CREDENTIAL_NAME in r.getMessage() for r in caplog.records)
