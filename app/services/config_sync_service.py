"""One-time copy of auto-reply / scheduled-message config to other groups.

This is a configuration copy, not live message forwarding and not an ongoing
link.  Copies are always inserted as new disabled rows; existing target rows
are never updated or deleted.  Dedup uses a content fingerprint so repeating
the same sync, or a target that already has the same config, is skipped.
Keywords are not treated as a unique key.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any, Iterable, Optional

from app import db
from app.models import AutoReply, BotGroup, ScheduledMessage

KIND_AUTO_REPLIES = 'auto_replies'
KIND_SCHEDULED_MESSAGES = 'scheduled_messages'
VALID_KINDS = (KIND_AUTO_REPLIES, KIND_SCHEDULED_MESSAGES)
KIND_LABELS = {
    KIND_AUTO_REPLIES: '自动回复',
    KIND_SCHEDULED_MESSAGES: '定时消息',
}
TARGET_GROUP_TYPES = ('group', 'supergroup')
MAX_SYNC_TARGETS = 50

# Serialize overlapping syncs in the same process (double-click / repeated POST).
_SYNC_LOCKS_GUARD = threading.Lock()
_SYNC_LOCKS: dict[str, threading.Lock] = {}
_BUSY_MSG = '同步正在进行，请稍后再试'


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def is_valid_kind(kind: str) -> bool:
    return kind in VALID_KINDS


def sync_lock_key(source_group_id: int, kind: str) -> str:
    return f'{int(source_group_id)}:{kind}'


def try_acquire_sync_lock(source_group_id: int, kind: str) -> Optional[threading.Lock]:
    """Non-blocking lock for one source+kind. Caller must release."""
    key = sync_lock_key(source_group_id, kind)
    with _SYNC_LOCKS_GUARD:
        lock = _SYNC_LOCKS.setdefault(key, threading.Lock())
    if lock.acquire(blocking=False):
        return lock
    return None


def _model_for_kind(kind: str):
    if kind == KIND_AUTO_REPLIES:
        return AutoReply
    if kind == KIND_SCHEDULED_MESSAGES:
        return ScheduledMessage
    raise ValueError('invalid kind')


def count_source_items(group_id: int, kind: str) -> int:
    return _model_for_kind(kind).query.filter_by(group_id=group_id).count()


def list_eligible_targets(source: BotGroup) -> list[BotGroup]:
    """Groups of the same bot, excluding the source itself and all channels."""
    return (
        BotGroup.query.filter(
            BotGroup.clone_id == source.clone_id,
            BotGroup.id != source.id,
            BotGroup.type.in_(TARGET_GROUP_TYPES),
        )
        .order_by(BotGroup.is_active.desc(), BotGroup.title.asc(), BotGroup.id.asc())
        .all()
    )


def serialize_target(group: BotGroup) -> dict:
    return {
        'id': group.id,
        'title': group.title or '',
        'type': group.type,
        'is_active': bool(group.is_active),
    }


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _parse_json(raw: Any, default):
    if raw is None or raw == '':
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _norm_links(raw: Any) -> Any:
    links = _parse_json(raw, [])
    if not isinstance(links, list):
        return links
    cleaned = []
    for item in links:
        if isinstance(item, dict):
            cleaned.append({
                'text': item.get('text') or '',
                'url': item.get('url') or '',
                'row': item.get('row', 0),
                'order': item.get('order', 0),
            })
        else:
            cleaned.append(item)
    return cleaned


def _norm_media(item) -> list[dict]:
    typed = item.get_media_url_with_types() if hasattr(item, 'get_media_url_with_types') else []
    return [{'url': url, 'type': mtype} for url, mtype in typed]


def _norm_dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return ''


def _sha(payload: dict) -> str:
    return 'v1:' + hashlib.sha256(_dump(payload).encode('utf-8')).hexdigest()


def auto_reply_fingerprint(item: AutoReply) -> str:
    return _sha({
        'trigger_keyword': item.trigger_keyword or '',
        'media_type': item.media_type or 'text',
        'media': _norm_media(item),
        'content': item.content or '',
        'links': _norm_links(item.links),
        'delete_after': int(item.delete_after or 0),
        'remark': item.remark or '',
    })


def scheduled_message_fingerprint(item: ScheduledMessage) -> str:
    return _sha({
        'media_type': item.media_type or 'text',
        'media': _norm_media(item),
        'content': item.content or '',
        'links': _norm_links(item.links),
        'repeat_interval': int(item.repeat_interval or 0),
        'delete_previous': bool(item.delete_previous),
        'start_time': _norm_dt(item.start_time),
        'stop_time': _norm_dt(item.stop_time),
        'remark': item.remark or '',
        'auto_pin': bool(getattr(item, 'auto_pin', False)),
    })


def item_fingerprint(item, kind: str) -> str:
    if kind == KIND_AUTO_REPLIES:
        return auto_reply_fingerprint(item)
    if kind == KIND_SCHEDULED_MESSAGES:
        return scheduled_message_fingerprint(item)
    raise ValueError('invalid kind')


def snapshot_auto_reply(item: AutoReply) -> dict:
    return {
        'fingerprint': auto_reply_fingerprint(item),
        'trigger_keyword': item.trigger_keyword,
        'media_type': item.media_type or 'text',
        'media_url': item.media_url,
        'media_urls': item.media_urls if item.media_urls else '[]',
        'content': item.content,
        'links': item.links or '[]',
        'delete_after': int(item.delete_after or 0),
        'remark': item.remark,
    }


def snapshot_scheduled_message(item: ScheduledMessage) -> dict:
    return {
        'fingerprint': scheduled_message_fingerprint(item),
        'media_type': item.media_type or 'text',
        'media_url': item.media_url,
        'media_urls': item.media_urls if item.media_urls else '[]',
        'content': item.content,
        'links': item.links or '[]',
        'repeat_interval': int(item.repeat_interval or 0),
        'delete_previous': bool(item.delete_previous),
        'start_time': item.start_time,
        'stop_time': item.stop_time,
        'remark': item.remark,
        'auto_pin': bool(getattr(item, 'auto_pin', False)),
    }


def snapshot_source_items(group_id: int, kind: str) -> list[dict]:
    model = _model_for_kind(kind)
    rows = model.query.filter_by(group_id=group_id).order_by(model.id.asc()).all()
    if kind == KIND_AUTO_REPLIES:
        return [snapshot_auto_reply(row) for row in rows]
    return [snapshot_scheduled_message(row) for row in rows]


def _build_auto_reply(target_id: int, snap: dict) -> AutoReply:
    return AutoReply(
        group_id=target_id,
        trigger_keyword=snap['trigger_keyword'],
        media_type=snap['media_type'],
        media_url=snap['media_url'],
        media_urls=snap['media_urls'],
        content=snap['content'],
        links=snap['links'],
        delete_after=snap['delete_after'],
        remark=snap['remark'],
        is_active=False,
    )


def _build_scheduled_message(target_id: int, snap: dict) -> ScheduledMessage:
    return ScheduledMessage(
        group_id=target_id,
        media_type=snap['media_type'],
        media_url=snap['media_url'],
        media_urls=snap['media_urls'],
        content=snap['content'],
        links=snap['links'],
        repeat_interval=snap['repeat_interval'],
        delete_previous=snap['delete_previous'],
        start_time=snap['start_time'],
        stop_time=snap['stop_time'],
        remark=snap['remark'],
        auto_pin=snap['auto_pin'],
        is_active=False,
        message_thread_id=None,
        target_type='group',
        target_channel_id=None,
        last_message_id=None,
        last_message_ids=None,
        last_sent_at=None,
        next_send_at=None,
        fail_count=0,
    )


def copy_snapshots_to_target(kind: str, target_id: int, snapshots: Iterable[dict]) -> tuple[int, int]:
    """Insert missing configs into one target. Returns (added, skipped).

    Existing rows are locked when the database supports it, so concurrent
    identical syncs cannot both insert the same fingerprint.
    """
    model = _model_for_kind(kind)
    existing = (
        model.query.filter_by(group_id=target_id)
        .with_for_update()
        .all()
    )
    seen = {item_fingerprint(row, kind) for row in existing}
    added = 0
    skipped = 0
    for snap in snapshots:
        fp = snap['fingerprint']
        if fp in seen:
            skipped += 1
            continue
        if kind == KIND_AUTO_REPLIES:
            db.session.add(_build_auto_reply(target_id, snap))
        else:
            db.session.add(_build_scheduled_message(target_id, snap))
        seen.add(fp)
        added += 1
    return added, skipped


def classify_target(
    source: BotGroup,
    target_id: Any,
    *,
    session_clone_id,
) -> tuple[Optional[BotGroup], Optional[str]]:
    """Return (target, error). error is a safe, generic message when denied."""
    try:
        tid = int(target_id)
    except (TypeError, ValueError):
        return None, '无权或不存在'
    if tid == source.id:
        return None, '不能同步到源群自己'
    target = db.session.get(BotGroup, tid)
    if target is None:
        return None, '无权或不存在'
    # Same-bot only; also honor the logged-in clone session.
    if target.clone_id != source.clone_id:
        return None, '无权或不存在'
    if session_clone_id is not None and target.clone_id != session_clone_id:
        return None, '无权或不存在'
    if target.type not in TARGET_GROUP_TYPES:
        return None, '只能同步到已添加的群组'
    return target, None
