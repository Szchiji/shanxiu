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


def increment_lottery_message_counts(group_id: int, user_id: int, now: Optional[datetime] = None,
                                     *, return_qualifications=False):
    """Same window/counter for text and media, including random lotteries.

    The guarded parent UPDATE serializes each increment against the draw claim.
    No await happens inside this transaction. Counters freeze once drawing starts.
    """
    from app import db
    from app.models import BotGroup, GroupLottery, LotteryMessageCount
    from app.plugins import is_plugin_enabled
    from app.utils import get_beijing_now

    group = BotGroup.query.filter_by(id=group_id).with_for_update().first()
    if not group or not group.is_active or not is_plugin_enabled(group_id, 'lottery'):
        return [] if return_qualifications else 0
    stamp = now or get_beijing_now()
    ids = [lid for (lid,) in db.session.query(GroupLottery.id).filter(
        GroupLottery.group_id == group_id,
        GroupLottery.lottery_type.in_(['message_count', 'message_rank', 'random']),
        GroupLottery.status.in_(['pending', 'active']),
        GroupLottery.start_time <= stamp, GroupLottery.end_time >= stamp,
    ).order_by(GroupLottery.id).all()]
    updated, qualifications = 0, []
    for lid in ids:
        # Do not replace this write with an unlocked SELECT: this is also the
        # serialization point for a concurrent draw and first counter insertion.
        locked = GroupLottery.query.filter(
            GroupLottery.id == lid, GroupLottery.status.in_(['pending', 'active']),
            GroupLottery.start_time <= stamp, GroupLottery.end_time >= stamp,
        ).update({'updated_at': GroupLottery.updated_at}, synchronize_session=False)
        if not locked:
            continue
        lottery = db.session.get(GroupLottery, lid, populate_existing=True)
        row = LotteryMessageCount.query.filter_by(lottery_id=lid, user_id=user_id).first()
        if row is None:
            row = LotteryMessageCount(lottery_id=lid, group_id=group_id,
                                      user_id=user_id, message_count=0)
            db.session.add(row)
        row.message_count = (row.message_count or 0) + 1
        row.updated_at = stamp
        if lottery.lottery_type == 'message_count' and row.message_count == lottery.min_messages:
            qualifications.append(lid)
        updated += 1
    db.session.commit()
    return qualifications if return_qualifications else updated


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
