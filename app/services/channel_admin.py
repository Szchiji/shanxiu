"""Channel/discussion settings and observed-post statistics. No Telegram I/O."""
from types import SimpleNamespace
from sqlalchemy.exc import IntegrityError
from app import db
from app.models import BotGroup, OtherSettings, ChannelPostRecord
from app.utils import get_beijing_now

CHANNEL_FIELDS = ('cancel_channel_pin', 'auto_delete_channel_discussion_msg',
                  'channel_auto_buttons', 'channel_discussion_keyword_filter')
GROUP_FIELDS = ('auto_delete_join_msg', 'auto_delete_leave_msg',
                'auto_delete_promote_msg', 'auto_delete_pin_msg')


def discussion_settings(channel, discussion):
    """Explicit channel saves (including OFF) win; untouched installations keep legacy settings.
    Group-only options always belong to the discussion group, never the channel.
    """
    legacy = OtherSettings.query.filter_by(group_id=discussion.id).first()
    primary = OtherSettings.query.filter_by(group_id=channel.id).first() if channel else None
    from app.modules.core.routes import get_group_conf
    conf = get_group_conf(channel) if channel else {}
    use_primary = primary is not None and (conf.get('channel_settings_configured') or
                                          any(getattr(primary, f, None) for f in CHANNEL_FIELDS))
    selected = primary if use_primary else legacy
    values = {f: getattr(legacy, f, False) if legacy else False for f in GROUP_FIELDS}
    values.update({f: getattr(selected, f, None) if selected else None for f in CHANNEL_FIELDS})
    return SimpleNamespace(**values)


def source_message_id(msg):
    origin = getattr(msg, 'forward_origin', None)
    return getattr(origin, 'message_id', None) or getattr(msg, 'forward_from_message_id', None) or msg.message_id


def record_post(group, msg, *, batch_key=None, media_type=None, text=None, is_forward=None):
    if not group or not group.is_active or group.type != 'channel':
        return
    mid = getattr(msg, 'message_id', None)
    if mid is None:
        return
    album = getattr(msg, 'media_group_id', None)
    key = batch_key or (f'album:{album}' if album else f'message:{mid}')
    kind = media_type or ('photo' if getattr(msg, 'photo', None) else
                         'video' if getattr(msg, 'video', None) else
                         'media' if any(getattr(msg, f, None) for f in
                                        ('document', 'audio', 'animation', 'voice', 'video_note', 'sticker')) else 'text')
    body = text if text is not None else (getattr(msg, 'text', None) or getattr(msg, 'caption', None) or '')
    row = ChannelPostRecord.query.filter_by(group_id=group.id, message_id=mid).first()
    if row:
        return row
    date = getattr(msg, 'date', None)
    if date and date.tzinfo:
        from zoneinfo import ZoneInfo
        date = date.astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
    row = ChannelPostRecord(group_id=group.id, message_id=mid, post_key=key,
                            media_type=kind, message_text=body,
                            is_forward=bool(getattr(msg, 'forward_origin', None)) if is_forward is None else is_forward,
                            posted_at=date or get_beijing_now())
    # A duplicate webhook/own-send echo must not double count or roll back other work.
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        return ChannelPostRecord.query.filter_by(group_id=group.id, message_id=mid).first()
    db.session.commit()
    return row


def post_summary(gid):
    # An album crossing midnight belongs to the date of its first observed part.
    return db.session.query(ChannelPostRecord.post_key,
                            db.func.min(ChannelPostRecord.posted_at).label('posted_at')).filter(
        ChannelPostRecord.group_id == gid).group_by(ChannelPostRecord.post_key).subquery()


def post_count(gid, since=None):
    posts = post_summary(gid)
    q = db.session.query(db.func.count()).select_from(posts)
    if since is not None:
        q = q.filter(posts.c.posted_at >= since)
    return q.scalar() or 0


def source_metadata(chat_id, ids, clone_id):
    group = BotGroup.query.filter_by(chat_id=str(chat_id), clone_id=clone_id, type='channel').first()
    if not group:
        return {}
    rows = ChannelPostRecord.query.filter(ChannelPostRecord.group_id == group.id,
                                          ChannelPostRecord.message_id.in_(ids)).all()
    # Album caption can be on a different part; filters apply to the entire album.
    keys = {r.post_key for r in rows if r.post_key.startswith('album:')}
    siblings = ChannelPostRecord.query.filter(ChannelPostRecord.group_id == group.id,
                                               ChannelPostRecord.post_key.in_(keys)).all() if keys else []
    captions = {r.post_key: r.message_text for r in siblings if r.message_text}
    return {r.message_id: dict(media_type=r.media_type, text=captions.get(r.post_key, r.message_text or ''),
                              post_key=r.post_key, is_forward=bool(r.is_forward)) for r in rows}
