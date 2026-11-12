"""Collect due forced-channel-subscription checks as detached-safe snapshots.

The scheduler ticks every minute, but each group only runs when its own
``check_interval`` has elapsed. The collection step must not return live ORM
instances: after ``commit()`` (and after the Flask app context exits) those
objects are expired/detached, and later attribute access raises
``DetachedInstanceError``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.check_interval import (
    DEFAULT_FORCED_SUB_INTERVAL,
    is_interval_due,
)
from app.utils import get_beijing_now


def snapshot_forced_subscription(settings) -> Dict[str, Any]:
    """Copy the fields the scheduled job needs into a plain dict.

    Call this while ``settings`` is still bound to an open session, and
    preferably before ``commit()`` expires the instance.
    """
    return {
        'id': settings.id,
        'group_id': settings.group_id,
        'channel_id': settings.channel_id,
        'unsubscribe_action': settings.unsubscribe_action,
        'reward_points': settings.reward_points,
    }


def collect_due_forced_subscriptions(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Return snapshots for enabled groups whose check interval is due.

    Updates ``last_checked_at`` for those groups and commits. Callers must
    invoke this inside a Flask app context. The returned dicts are safe to
    use after the session is closed.
    """
    from app import db
    from app.models import ForcedChannelSubscription

    if now is None:
        now = get_beijing_now()

    due: List[Dict[str, Any]] = []
    rows = ForcedChannelSubscription.query.filter_by(enabled=True).all()
    for settings in rows:
        if not is_interval_due(
            settings.last_checked_at,
            settings.check_interval,
            now,
            DEFAULT_FORCED_SUB_INTERVAL,
        ):
            continue
        settings.last_checked_at = now
        due.append(snapshot_forced_subscription(settings))
    if due:
        db.session.commit()
    return due
