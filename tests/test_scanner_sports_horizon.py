"""Tests for sports-futures extended scan horizon (_within_horizon helper)."""
import datetime
from core.directional.scanner import _within_horizon  # module-level helper added below


def test_sports_future_admitted_under_long_horizon():
    close = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=120)
    assert _within_horizon("KXNBA-27", close, default_days=30, sports_days=220) is True


def test_non_sports_keeps_default_horizon():
    close = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=120)
    assert _within_horizon("KXCPI-26JUL", close, default_days=30, sports_days=220) is False


def test_past_close_rejected():
    close = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    assert _within_horizon("KXNBA-27", close, default_days=30, sports_days=220) is False


def test_none_close_rejected():
    assert _within_horizon("KXNBA-27", None, default_days=30, sports_days=220) is False
