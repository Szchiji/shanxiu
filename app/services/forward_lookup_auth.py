"""Authorization for the private-chat forwarded-user lookup."""

from __future__ import annotations

import json
from typing import Optional, Sequence

from app.models import BotClone, BotGroup, GroupMember

FORWARD_LOOKUP_DENIED_TEXT = '❌ 仅管理员可查询用户资料。'


def is_forward_lookup_authorized(
    user_id: int,
    *,
    admin_id: int = 0,
    is_clone_admin: bool = False,
    is_registered_group_admin: bool = False,
) -> bool:
    if admin_id and user_id == admin_id:
        return True
    if is_clone_admin:
        return True
    if is_registered_group_admin:
        return True
    return False


def _clone_admin_ids(clone) -> list:
    ids = []
    if getattr(clone, 'owner_user_id', None):
        ids.append(clone.owner_user_id)
    try:
        extra = json.loads(clone.admin_user_ids or '[]')
        if isinstance(extra, list):
            ids.extend(extra)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return ids


def user_is_any_clone_admin(user_id: int) -> bool:
    for clone in BotClone.query.all():
        if user_id in _clone_admin_ids(clone):
            return True
    return False


def user_is_recorded_group_admin(user_id: int, clone_id: Optional[int] = None) -> bool:
    """True if GroupMember records this user as creator/admin of an active group.

    When ``clone_id`` is given, only that bot instance's groups are considered;
    when omitted, any registered active group counts.
    """
    query = (
        GroupMember.query
        .join(BotGroup, GroupMember.group_id == BotGroup.id)
        .filter(
            GroupMember.user_id == user_id,
            GroupMember.status.in_(['creator', 'administrator']),
            BotGroup.is_active.is_(True),
            BotGroup.type.in_(['group', 'supergroup']),
        )
    )
    if clone_id is not None:
        query = query.filter(BotGroup.clone_id == clone_id)
    return db_exists(query)


def db_exists(query) -> bool:
    return query.first() is not None


def list_active_group_chat_ids(clone_id: Optional[int] = None, limit: int = 50) -> Sequence[str]:
    query = BotGroup.query.filter(
        BotGroup.is_active.is_(True),
        BotGroup.type.in_(['group', 'supergroup']),
    )
    if clone_id is not None:
        query = query.filter(BotGroup.clone_id == clone_id)
    groups = query.order_by(BotGroup.updated_at.desc()).limit(limit).all()
    return [g.chat_id for g in groups if g.chat_id]
