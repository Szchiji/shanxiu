"""
Per-group check-interval helpers for forced subscription and inactive users.

The global job runs on a short tick; each group is only actually processed
when its own ``check_interval`` has elapsed since ``last_checked_at``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

DEFAULT_FORCED_SUB_INTERVAL = 3600
DEFAULT_INACTIVE_INTERVAL = 86400
CHECK_INTERVAL_TICK_SECONDS = 60


def normalize_check_interval(interval_seconds, default_interval: int) -> int:
    try:
        interval = int(interval_seconds)
    except (TypeError, ValueError):
        return default_interval
    if interval <= 0:
        return default_interval
    return interval


def is_interval_due(
    last_checked_at: Optional[datetime],
    interval_seconds,
    now: datetime,
    default_interval: int,
) -> bool:
    """Return True if this group should run a check at ``now``.

    A missing ``last_checked_at`` is treated as due (first run after deploy /
    enabling the feature), matching the previous "run soon after startup" cadence.
    """
    interval = normalize_check_interval(interval_seconds, default_interval)
    if last_checked_at is None:
        return True
    try:
        elapsed = (now - last_checked_at).total_seconds()
    except (TypeError, ValueError):
        return True
    return elapsed >= interval
