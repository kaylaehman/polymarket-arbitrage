"""Tests for time-decay edge discounting (FEAT-07)."""

from datetime import datetime, timedelta, timezone

from core.arb_engine import hours_until_resolution, time_decay_multiplier


def _in_hours(h: float, aware: bool = True) -> datetime:
    base = datetime.now(timezone.utc) if aware else datetime.utcnow()
    return base + timedelta(hours=h)


def test_no_date_is_no_discount():
    assert time_decay_multiplier(None) == 1.0
    assert hours_until_resolution(None) is None


def test_buckets():
    assert time_decay_multiplier(_in_hours(200)) == 1.0    # > 7 days
    assert time_decay_multiplier(_in_hours(72)) == 0.75    # 2-7 days
    assert time_decay_multiplier(_in_hours(36)) == 0.5     # 24-48h
    assert time_decay_multiplier(_in_hours(6)) == 0.25     # < 24h


def test_handles_naive_datetime_without_raising():
    # Naive datetimes are treated as UTC (the codebase uses naive utcnow()).
    assert time_decay_multiplier(_in_hours(200, aware=False)) == 1.0
    h = hours_until_resolution(_in_hours(10, aware=False))
    assert 9 < h < 11


def test_handles_aware_datetime():
    h = hours_until_resolution(_in_hours(10, aware=True))
    assert 9 < h < 11
