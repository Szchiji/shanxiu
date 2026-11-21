"""Copy auto-reply / scheduled-message config to other groups, with status follow.

This is a unidirectional source→target link, not live message forwarding.
First sync creates target rows and mapping records.  Later syncs update the
already-linked rows instead of inserting duplicates.  Enable/pause of a source
rule (and the source group's module switch) is propagated only to linked
target rows; the target group's own module switch and native rules stay local.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import or_

from app import db
from app.models import AutoReply, BotGroup, ConfigSyncLink, ScheduledMessage, DEFAULT_SYSTEM

KIND_AUTO_REPLIES = 'auto_replies'
KIND_SCHEDULED_MESSAGES = 'scheduled_messages'
VALID_KINDS = (KIND_AUTO_REPLIES, KIND_SCHEDULED_MESSAGES)
KIND_LABELS = {
    KIND_AUTO_REPLIES: '自动回复',
    KIND_SCHEDULED_MESSAGES: '定时消息',
}
TARGET_GROUP_TYPES = ('group', 'supergroup')
MAX_SYNC_TARGETS = 50
SOURCE_IS_FOLLOWER_MSG = '本群已有跟随其他群的规则，不能再作为源同步到其他群'
TARGET_IS_SOURCE_MSG = '目标群已作为源同步到其他群，不能再接收跟随'
FOLLOWED_TOGGLE_MSG = '该规则跟随源群开启和停止，请到源群修改'
_BUSY_MSG = '同步正在进行，请稍后再试'

_SYNC_LOCKS_GUARD = threading.Lock()
_SYNC_LOCKS: dict[str, threading.Lock] = {}


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


def serialize_target(group: BotGroup, *, kind: str) -> dict:
    blocked = group_is_source_of_links(group.id, kind)
    return {
        'id': group.id,
        'title': group.title or '',
        'type': group.type,
        'is_active': bool(group.is_active),
        'can_receive': not blocked,
        'block_reason': TARGET_IS_SOURCE_MSG if blocked else '',
    }


def group_is_follower(group_id: int, kind: str) -> bool:
    return db.session.query(ConfigSyncLink.id).filter_by(
        kind=kind, target_group_id=group_id
    ).first() is not None


def group_is_source_of_links(group_id: int, kind: str) -> bool:
    return db.session.query(ConfigSyncLink.id).filter_by(
        kind=kind, source_group_id=group_id
    ).first() is not None


def source_block_reason(group_id: int, kind: str) -> Optional[str]:
    if group_is_follower(group_id, kind):
        return SOURCE_IS_FOLLOWER_MSG
    return None


# ---------------------------------------------------------------------------
# Fingerprints / snapshots
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
        'source_item_id': item.id,
        'source_is_active': bool(item.is_active),
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
        'source_item_id': item.id,
        'source_is_active': bool(item.is_active),
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


def _apply_auto_reply_fields(item: AutoReply, snap: dict) -> None:
    item.trigger_keyword = snap['trigger_keyword']
    item.media_type = snap['media_type']
    item.media_url = snap['media_url']
    item.media_urls = snap['media_urls']
    item.content = snap['content']
    item.links = snap['links']
    item.delete_after = snap['delete_after']
    item.remark = snap['remark']


def _apply_scheduled_fields(item: ScheduledMessage, snap: dict) -> None:
    item.media_type = snap['media_type']
    item.media_url = snap['media_url']
    item.media_urls = snap['media_urls']
    item.content = snap['content']
    item.links = snap['links']
    item.repeat_interval = snap['repeat_interval']
    item.delete_previous = snap['delete_previous']
    item.start_time = snap['start_time']
    item.stop_time = snap['stop_time']
    item.remark = snap['remark']
    item.auto_pin = snap['auto_pin']


def _build_auto_reply(target_id: int, snap: dict) -> AutoReply:
    item = AutoReply(group_id=target_id, is_active=False)
    _apply_auto_reply_fields(item, snap)
    return item


def _build_scheduled_message(target_id: int, snap: dict) -> ScheduledMessage:
    item = ScheduledMessage(
        group_id=target_id,
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
    _apply_scheduled_fields(item, snap)
    return item


def _master_switch_key(kind: str) -> str:
    if kind == KIND_AUTO_REPLIES:
        return 'auto_reply_open'
    return 'scheduled_msg_open'


def master_switch_open(group: Optional[BotGroup], kind: str) -> bool:
    if not group:
        return True
    conf = dict(DEFAULT_SYSTEM)
    if group.config:
        try:
            parsed = json.loads(group.config)
            if isinstance(parsed, dict):
                if 'config' in parsed and isinstance(parsed['config'], dict):
                    parsed = parsed['config']
                conf.update(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return bool(conf.get(_master_switch_key(kind), True))


def followed_is_active(source_item_active: bool, source_group: Optional[BotGroup], kind: str) -> bool:
    return bool(source_item_active) and master_switch_open(source_group, kind)


def set_target_master_switch(target: BotGroup, kind: str, enabled: bool) -> None:
    """Make a synced target usable for the synced feature.

    A sync action is intended to copy a working feature to every selected
    target, so the target's corresponding module switch follows the source
    instead of silently blocking the copied rules.
    """
    key = _master_switch_key(kind)
    conf = _parse_json(target.config, {})
    if not isinstance(conf, dict):
        conf = {}
    if isinstance(conf.get('config'), dict):
        conf['config'][key] = bool(enabled)
    else:
        conf[key] = bool(enabled)
    target.config = json.dumps(conf, ensure_ascii=False)


def _set_followed_status(kind: str, item, want_active: bool, now) -> None:
    """Apply followed enable/pause. Never sends a message."""
    if kind == KIND_SCHEDULED_MESSAGES and want_active:
        # Align next_send_at so enabling does not immediately backfill missed slots.
        from app.modules.core.routes import prepare_scheduled_message_enable
        item.is_active = True
        prepare_scheduled_message_enable(item, now)
        return
    item.is_active = bool(want_active)


def _now():
    from app.utils import get_beijing_now
    return get_beijing_now()


def upsert_linked_copies(kind: str, source: BotGroup, target: BotGroup, snapshots: Iterable[dict]) -> tuple[int, int]:
    """Create or update linked copies in one target. Returns (added, updated)."""
    model = _model_for_kind(kind)
    # Lock existing target rows / links to avoid concurrent duplicate inserts.
    model.query.filter_by(group_id=target.id).with_for_update().all()
    links = (
        ConfigSyncLink.query.filter_by(
            kind=kind, source_group_id=source.id, target_group_id=target.id
        )
        .with_for_update()
        .all()
    )
    by_source = {link.source_item_id: link for link in links}
    added = 0
    updated = 0
    now = _now()
    set_target_master_switch(target, kind, master_switch_open(source, kind))
    for snap in snapshots:
        want = followed_is_active(snap['source_is_active'], source, kind)
        link = by_source.get(snap['source_item_id'])
        if link:
            item = db.session.get(model, link.target_item_id)
            if item is None:
                item = _build_auto_reply(target.id, snap) if kind == KIND_AUTO_REPLIES else _build_scheduled_message(target.id, snap)
                db.session.add(item)
                db.session.flush()
                link.target_item_id = item.id
                added += 1
            else:
                if kind == KIND_AUTO_REPLIES:
                    _apply_auto_reply_fields(item, snap)
                else:
                    _apply_scheduled_fields(item, snap)
                updated += 1
            _set_followed_status(kind, item, want, now)
            continue
        item = _build_auto_reply(target.id, snap) if kind == KIND_AUTO_REPLIES else _build_scheduled_message(target.id, snap)
        db.session.add(item)
        db.session.flush()
        _set_followed_status(kind, item, want, now)
        db.session.add(ConfigSyncLink(
            kind=kind,
            source_group_id=source.id,
            source_item_id=snap['source_item_id'],
            target_group_id=target.id,
            target_item_id=item.id,
        ))
        added += 1
    return added, updated


def classify_target(
    source: BotGroup,
    target_id: Any,
    *,
    session_clone_id,
    kind: str,
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
    if target.clone_id != source.clone_id:
        return None, '无权或不存在'
    if session_clone_id is not None and target.clone_id != session_clone_id:
        return None, '无权或不存在'
    if target.type not in TARGET_GROUP_TYPES:
        return None, '只能同步到已添加的群组'
    if group_is_source_of_links(target.id, kind):
        return None, TARGET_IS_SOURCE_MSG
    return target, None


# ---------------------------------------------------------------------------
# Status follow / annotations
# ---------------------------------------------------------------------------

def link_for_target_item(kind: str, item_id: int) -> Optional[ConfigSyncLink]:
    return ConfigSyncLink.query.filter_by(kind=kind, target_item_id=item_id).first()


def followed_target_ids(kind: str, item_ids: Iterable[int]) -> set[int]:
    ids = [int(i) for i in item_ids if i is not None]
    if not ids:
        return set()
    rows = ConfigSyncLink.query.filter(
        ConfigSyncLink.kind == kind,
        ConfigSyncLink.target_item_id.in_(ids),
    ).all()
    return {row.target_item_id for row in rows}


def detach_links_for_item(kind: str, item_id: int) -> int:
    """Drop mappings when a source or target row is deleted; keep the other row."""
    q = ConfigSyncLink.query.filter(
        ConfigSyncLink.kind == kind,
        or_(
            ConfigSyncLink.source_item_id == item_id,
            ConfigSyncLink.target_item_id == item_id,
        ),
    )
    count = q.count()
    q.delete(synchronize_session=False)
    return count


def detach_links_for_group(group_id: int) -> None:
    ConfigSyncLink.query.filter(
        or_(
            ConfigSyncLink.source_group_id == group_id,
            ConfigSyncLink.target_group_id == group_id,
        )
    ).delete(synchronize_session=False)


def propagate_source_item_status(kind: str, source_item) -> int:
    """Copy enable/pause of one source item to all linked targets. No sends."""
    if source_item is None:
        return 0
    source_group = db.session.get(BotGroup, source_item.group_id)
    want = followed_is_active(bool(source_item.is_active), source_group, kind)
    now = _now()
    model = _model_for_kind(kind)
    updated = 0
    links = ConfigSyncLink.query.filter_by(kind=kind, source_item_id=source_item.id).all()
    for link in links:
        item = db.session.get(model, link.target_item_id)
        if item is None:
            continue
        _set_followed_status(kind, item, want, now)
        updated += 1
    return updated


def propagate_source_item_fields(kind: str, source_item, *, reset_schedule: bool = False) -> int:
    """Copy edited source fields to linked target rows.

    Target rows are controlled copies, so editing the source must update their
    content as well as their enabled state. Runtime delivery fields are not
    copied. For scheduled messages, reset cadence only when the source's
    schedule itself changed; a text/media edit keeps the target's current
    cadence.
    """
    if source_item is None:
        return 0
    source_group = db.session.get(BotGroup, source_item.group_id)
    model = _model_for_kind(kind)
    snap = snapshot_auto_reply(source_item) if kind == KIND_AUTO_REPLIES else snapshot_scheduled_message(source_item)
    want = followed_is_active(bool(source_item.is_active), source_group, kind)
    now = _now()
    updated = 0
    links = ConfigSyncLink.query.filter_by(kind=kind, source_item_id=source_item.id).all()
    for link in links:
        item = db.session.get(model, link.target_item_id)
        if item is None:
            continue
        if kind == KIND_AUTO_REPLIES:
            _apply_auto_reply_fields(item, snap)
            item.is_active = want
        else:
            _apply_scheduled_fields(item, snap)
            if reset_schedule and want:
                _set_followed_status(kind, item, True, now)
            else:
                item.is_active = want
        updated += 1
    return updated


def propagate_source_group_master(kind: str, source_group: BotGroup) -> int:
    """Follow the source module switch on linked target groups and rules."""
    model = _model_for_kind(kind)
    now = _now()
    updated = 0
    links = ConfigSyncLink.query.filter_by(kind=kind, source_group_id=source_group.id).all()
    if not links:
        return 0
    source_ids = {link.source_item_id for link in links}
    sources = {row.id: row for row in model.query.filter(model.id.in_(source_ids)).all()}
    target_ids = {link.target_group_id for link in links}
    targets = {
        group.id: group
        for group in BotGroup.query.filter(BotGroup.id.in_(target_ids)).all()
    }
    source_enabled = master_switch_open(source_group, kind)
    for target in targets.values():
        set_target_master_switch(target, kind, source_enabled)
    for link in links:
        src = sources.get(link.source_item_id)
        if src is None:
            continue
        item = db.session.get(model, link.target_item_id)
        if item is None:
            continue
        want = followed_is_active(bool(src.is_active), source_group, kind)
        _set_followed_status(kind, item, want, now)
        updated += 1
    return updated


def annotations_for_group(group_id: int, kind: str) -> dict[int, dict]:
    """Map local item id → {role, source_title, source_group_id, target_count}."""
    outgoing = ConfigSyncLink.query.filter_by(kind=kind, source_group_id=group_id).all()
    incoming = ConfigSyncLink.query.filter_by(kind=kind, target_group_id=group_id).all()
    result: dict[int, dict] = {}
    if outgoing:
        counts: dict[int, int] = {}
        for link in outgoing:
            counts[link.source_item_id] = counts.get(link.source_item_id, 0) + 1
        for item_id, count in counts.items():
            result[item_id] = {'role': 'source', 'target_count': count, 'source_title': '', 'source_group_id': None}
    if incoming:
        source_ids = {link.source_group_id for link in incoming}
        titles = {
            g.id: (g.title or '')
            for g in BotGroup.query.filter(BotGroup.id.in_(source_ids)).all()
        }
        for link in incoming:
            result[link.target_item_id] = {
                'role': 'target',
                'target_count': 0,
                'source_title': titles.get(link.source_group_id, ''),
                'source_group_id': link.source_group_id,
            }
    return result
