"""Tests for src/core/utils.py — UTC ISO 8601 formatting."""

from datetime import UTC, datetime, timedelta, timezone

from src.core.utils import format_utc_iso


def test_naive_datetime_treated_as_utc():
    dt = datetime(2026, 5, 9, 12, 30, 45, 123456)
    assert format_utc_iso(dt) == "2026-05-09T12:30:45.123456Z"


def test_aware_utc_datetime_keeps_value():
    dt = datetime(2026, 5, 9, 12, 30, 45, 123456, tzinfo=UTC)
    assert format_utc_iso(dt) == "2026-05-09T12:30:45.123456Z"


def test_non_utc_aware_datetime_is_converted():
    eastern_eu = timezone(timedelta(hours=2))
    dt = datetime(2026, 5, 9, 14, 30, 0, tzinfo=eastern_eu)
    assert format_utc_iso(dt) == "2026-05-09T12:30:00Z"
