"""
Shared speech-engagement helpers used by the pure-media message path.

Awards message/share points and increments lottery speech counts.  Auto-like
and member tags stay in the bot handler (they need tokens / executors).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func

from app.services.points_service import clamp_award_to_daily_cap


REASON_MESSAGE = "发送消息"
REASON_SHARE = "转发消息"


def increment_lottery_message_counts(group_id: int, user_id: int, now: Optional[datetime] = None) -> int:
    """Increment message_count for active/pending speech lotteries. Return updated rows."""
    from app import db
    from app.models import GroupLottery, LotteryMessageCount
    from app.plugins import is_plugin_enabled
    from app.utils import get_beijing_now

    if not is_plugin_enabled(group_id, 'lottery'):
        return 0

    stamp = now or get_beijing_now()
    lotteries = GroupLottery.query.filter_by(group_id=group_id).filter(
        GroupLottery.status.in_(['pending', 'active']),
        GroupLottery.lottery_type.in_(['message_count', 'message_rank']),
    ).limit(10).all()

    updated = 0
    for lottery in lotteries:
        if lottery.start_time and lottery.end_time:
            if not (lottery.start_time <= stamp <= lottery.end_time):
                continue
        row = LotteryMessageCount.query.filter_by(
            lottery_id=lottery.id, user_id=user_id
        ).first()
        if not row:
            row = LotteryMessageCount(
                lottery_id=lottery.id,
                group_id=group_id,
                user_id=user_id,
                message_count=0,
            )
            db.session.add(row)
        row.message_count = (row.message_count or 0) + 1
        row.updated_at = stamp
        updated += 1
    if updated:
        db.session.commit()
    return updated


def award_speech_points(
    group_id: int,
    user_id: int,
    *,
    is_forward: bool = False,
    skip_points: bool = False,
) -> int:
    """Award message (and optional share) points. Return total points granted."""
    if skip_points:
        return 0

    from app import db
    from app.models import PointsLog, PointsRule, UserPoints
    from app.plugins import is_plugin_enabled
    from app.utils import get_beijing_today

    if not is_plugin_enabled(group_id, 'points'):
        return 0

    granted = 0

    def _apply(rule_type: str, reason: str) -> int:
        rule = PointsRule.query.filter_by(
            group_id=group_id, rule_type=rule_type, is_active=True
        ).first()
        if not rule:
            return 0
        user_points = UserPoints.query.filter_by(group_id=group_id, user_id=user_id).first()
        if not user_points:
            user_points = UserPoints(group_id=group_id, user_id=user_id, points_balance=0)
            db.session.add(user_points)

        earned_today = 0
        if rule.max_daily_points:
            today_start = get_beijing_today()
            earned_today = db.session.query(
                func.coalesce(func.sum(PointsLog.points_change), 0)
            ).filter(
                PointsLog.group_id == group_id,
                PointsLog.user_id == user_id,
                PointsLog.reason == reason,
                PointsLog.created_at >= today_start,
            ).scalar() or 0

        actual = clamp_award_to_daily_cap(
            rule.points_amount, earned_today, rule.max_daily_points
        )
        if actual <= 0:
            return 0
        user_points.points_balance += actual
        db.session.add(PointsLog(
            group_id=group_id,
            user_id=user_id,
            points_change=actual,
            reason=reason,
            balance_after=user_points.points_balance,
        ))
        return actual

    granted += _apply('message', REASON_MESSAGE)
    if is_forward:
        granted += _apply('share', REASON_SHARE)
    if granted:
        db.session.commit()
    return granted
