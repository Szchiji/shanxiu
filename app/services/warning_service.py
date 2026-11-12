"""
app/services/warning_service.py
--------------------------------
Helpers for group warning (/warn and keyword action=warn) persistence,
optional auto-punishment, and clearing counts.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple


DEFAULT_WARN_THRESHOLD = 3
DEFAULT_WARN_ACTION = 'mute'
DEFAULT_WARN_MUTE_MINUTES = 60
VALID_WARN_ACTIONS = ('mute', 'kick', 'ban')
ACTION_LABELS = {
    'mute': '禁言',
    'kick': '踢出',
    'ban': '封禁',
}


def normalize_warn_settings(conf: Optional[Mapping[str, Any]] = None) -> dict:
    """Return a sanitized warn-settings dict. Default is auto-punish off."""
    data = dict(conf or {})
    enabled = bool(data.get('warn_auto_punish'))
    try:
        threshold = int(data.get('warn_threshold') or DEFAULT_WARN_THRESHOLD)
    except (TypeError, ValueError):
        threshold = DEFAULT_WARN_THRESHOLD
    if threshold < 1:
        threshold = DEFAULT_WARN_THRESHOLD
    action = data.get('warn_action') or DEFAULT_WARN_ACTION
    if action not in VALID_WARN_ACTIONS:
        action = DEFAULT_WARN_ACTION
    try:
        mute_minutes = int(data.get('warn_mute_minutes') or DEFAULT_WARN_MUTE_MINUTES)
    except (TypeError, ValueError):
        mute_minutes = DEFAULT_WARN_MUTE_MINUTES
    if mute_minutes < 1:
        mute_minutes = DEFAULT_WARN_MUTE_MINUTES
    return {
        'warn_auto_punish': enabled,
        'warn_threshold': threshold,
        'warn_action': action,
        'warn_mute_minutes': mute_minutes,
    }


def resolve_escalation(
    warn_count: int,
    settings: Optional[Mapping[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Decide whether *warn_count* should trigger auto-punishment.

    Default (no settings, or ``warn_auto_punish`` off) returns
    ``(None, None)`` so existing groups keep "count only" behaviour.
    When enabled, every warning at or above ``warn_threshold`` escalates.
    """
    cfg = normalize_warn_settings(settings)
    if not cfg['warn_auto_punish']:
        return None, None
    try:
        count = int(warn_count)
    except (TypeError, ValueError):
        return None, None
    if count < cfg['warn_threshold']:
        return None, None
    action = cfg['warn_action']
    return action, ACTION_LABELS.get(action, action)


def format_punishment_line(action: str, mute_minutes: Optional[int] = None) -> str:
    """Human-readable suffix describing the punishment that was applied."""
    if action == 'mute':
        minutes = mute_minutes or DEFAULT_WARN_MUTE_MINUTES
        return f"已执行处罚：禁言 {minutes} 分钟"
    if action == 'kick':
        return "已执行处罚：踢出群组"
    if action == 'ban':
        return "已执行处罚：永久封禁"
    label = ACTION_LABELS.get(action, action)
    return f"已执行处罚：{label}"


def format_command_warn_reply(
    user_name: str,
    reason: str,
    count: int,
    punishment_line: Optional[str] = None,
) -> str:
    """Chinese reply text for ``/warn`` after a successful record."""
    name = user_name or '该用户'
    reason_text = reason or '违反群规'
    text = (
        f"⚠️ 警告\n\n"
        f"用户: {name}\n"
        f"原因: {reason_text}\n"
        f"累计警告: {count} 次\n\n"
        f"请遵守群规，避免再次违规！"
    )
    if punishment_line:
        text += f"\n\n🚫 {punishment_line}"
    return text


def format_keyword_warn_reply(count: int, punishment_line: Optional[str] = None) -> str:
    """Chinese reply text for keyword-filter ``action=warn``."""
    text = f"⚠️ 警告：消息包含禁止关键词（累计警告 {count} 次）"
    if punishment_line:
        text += f"\n🚫 {punishment_line}"
    return text


def format_unwarn_reply(user_name: str, cleared: int) -> str:
    name = user_name or '该用户'
    if cleared <= 0:
        return f"✅ {name} 当前没有警告记录"
    return f"✅ 已清除 {name} 的 {cleared} 次警告"


def count_warnings(group_id: int, user_id: int) -> int:
    """Return number of stored warnings for *user_id* in *group_id*."""
    from app.models import GroupWarning

    return GroupWarning.query.filter_by(group_id=group_id, user_id=user_id).count()


def record_warning(
    group_id: int,
    user_id: int,
    *,
    admin_id: Optional[int] = None,
    reason: Optional[str] = None,
    source: str = 'command',
) -> int:
    """Persist a warning and return the user's new cumulative count.

    Requires an active Flask application context.
    """
    from app import db
    from app.models import GroupWarning

    row = GroupWarning(
        group_id=group_id,
        user_id=user_id,
        admin_id=admin_id,
        reason=reason or '违反群规',
        source=source or 'command',
    )
    db.session.add(row)
    db.session.commit()
    return count_warnings(group_id, user_id)


def clear_warnings(group_id: int, user_id: int) -> int:
    """Delete all warnings for *user_id* in *group_id*. Return previous count."""
    from app import db
    from app.models import GroupWarning

    q = GroupWarning.query.filter_by(group_id=group_id, user_id=user_id)
    previous = q.count()
    if previous:
        q.delete(synchronize_session=False)
        db.session.commit()
    return previous
