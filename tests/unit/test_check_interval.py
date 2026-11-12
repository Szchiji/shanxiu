"""Tests for per-group forced-subscription / inactive-user check intervals."""

from datetime import datetime, timedelta

from app.services.check_interval import (
    DEFAULT_FORCED_SUB_INTERVAL,
    DEFAULT_INACTIVE_INTERVAL,
    is_interval_due,
    normalize_check_interval,
)


NOW = datetime(2026, 11, 12, 18, 0, 0)


class TestNormalize:
    def test_invalid_falls_back_to_default(self):
        assert normalize_check_interval(None, 3600) == 3600
        assert normalize_check_interval(0, 3600) == 3600
        assert normalize_check_interval(-10, 86400) == 86400
        assert normalize_check_interval('abc', 3600) == 3600
        assert normalize_check_interval(120, 3600) == 120


class TestIsIntervalDue:
    def test_missing_last_checked_is_due(self):
        assert is_interval_due(None, 3600, NOW, DEFAULT_FORCED_SUB_INTERVAL) is True

    def test_forced_sub_default_one_hour(self):
        last = NOW - timedelta(seconds=3599)
        assert is_interval_due(last, 3600, NOW, DEFAULT_FORCED_SUB_INTERVAL) is False
        last = NOW - timedelta(seconds=3600)
        assert is_interval_due(last, None, NOW, DEFAULT_FORCED_SUB_INTERVAL) is True

    def test_inactive_default_one_day(self):
        last = NOW - timedelta(hours=23)
        assert is_interval_due(last, 86400, NOW, DEFAULT_INACTIVE_INTERVAL) is False
        last = NOW - timedelta(days=1)
        assert is_interval_due(last, None, NOW, DEFAULT_INACTIVE_INTERVAL) is True

    def test_custom_per_group_interval(self):
        last = NOW - timedelta(seconds=600)
        assert is_interval_due(last, 1800, NOW, DEFAULT_FORCED_SUB_INTERVAL) is False
        assert is_interval_due(last, 600, NOW, DEFAULT_FORCED_SUB_INTERVAL) is True

    def test_different_groups_independent(self):
        # Group A due (2h interval elapsed), group B not (1h interval, 30m ago)
        a_due = is_interval_due(NOW - timedelta(hours=2), 3600, NOW, DEFAULT_FORCED_SUB_INTERVAL)
        b_due = is_interval_due(NOW - timedelta(minutes=30), 3600, NOW, DEFAULT_FORCED_SUB_INTERVAL)
        assert a_due is True
        assert b_due is False
