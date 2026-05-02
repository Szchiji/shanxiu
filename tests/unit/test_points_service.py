"""
tests/unit/test_points_service.py
----------------------------------
Unit tests for app.services.points_service.

These tests do not touch the database or the Telegram API.
"""

import pytest
from app.services.points_service import (
    award_points,
    deduct_points,
    apply_points_change,
    InsufficientPointsError,
)


# ---------------------------------------------------------------------------
# award_points
# ---------------------------------------------------------------------------

class TestAwardPoints:
    def test_basic_award(self):
        assert award_points(100, 50) == 150

    def test_award_from_zero(self):
        assert award_points(0, 1) == 1

    def test_large_award(self):
        assert award_points(0, 1_000_000) == 1_000_000

    def test_award_zero_amount_raises(self):
        with pytest.raises(ValueError):
            award_points(100, 0)

    def test_award_negative_amount_raises(self):
        with pytest.raises(ValueError):
            award_points(100, -10)

    def test_negative_balance_raises(self):
        with pytest.raises(ValueError):
            award_points(-1, 10)


# ---------------------------------------------------------------------------
# deduct_points
# ---------------------------------------------------------------------------

class TestDeductPoints:
    def test_basic_deduction(self):
        assert deduct_points(100, 40) == 60

    def test_deduct_entire_balance(self):
        assert deduct_points(50, 50) == 0

    def test_insufficient_balance_raises(self):
        with pytest.raises(InsufficientPointsError) as exc_info:
            deduct_points(10, 50)
        err = exc_info.value
        assert err.current == 10
        assert err.requested == 50

    def test_deduct_zero_amount_raises(self):
        with pytest.raises(ValueError):
            deduct_points(100, 0)

    def test_deduct_negative_amount_raises(self):
        with pytest.raises(ValueError):
            deduct_points(100, -10)

    def test_insufficient_error_message(self):
        with pytest.raises(InsufficientPointsError, match="积分不足"):
            deduct_points(5, 100)

    def test_negative_balance_raises(self):
        with pytest.raises(ValueError):
            deduct_points(-1, 10)


# ---------------------------------------------------------------------------
# apply_points_change
# ---------------------------------------------------------------------------

class TestApplyPointsChange:
    def test_positive_change_awards(self):
        assert apply_points_change(100, 50) == 150

    def test_negative_change_deducts(self):
        assert apply_points_change(100, -30) == 70

    def test_zero_change_is_noop(self):
        assert apply_points_change(100, 0) == 100

    def test_negative_change_exceeds_balance(self):
        with pytest.raises(InsufficientPointsError):
            apply_points_change(10, -50)

    def test_exact_deduction_to_zero(self):
        assert apply_points_change(100, -100) == 0
