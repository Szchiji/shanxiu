"""Connect original-post buttons to a verified discussion thread (Bot API only).

Official link format: https://core.telegram.org/api/links#message-links
A linked_chat_id alone is not enough to invent a root ID. Until the automatic
forward arrives, leave the post untouched so its native comment entry is retained.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
from flask import has_app_context
from sqlalchemy.exc import IntegrityError
from app import db
from app.models import BotGroup, ChannelDiscussionPost


def _row(gid, mid):
    row = ChannelDiscussionPost.query.filter_by(group_id=gid, message_id=mid).first()
    if row:
        return row
    try:
        with db.session.begin_nested():
            row = ChannelDiscussionPost(group_id=gid, message_id=mid)
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        row = ChannelDiscussionPost.query.filter_by(group_id=gid, message_id=mid).one()
    return row


def note_original(group, msg):
    from app.services.channel_post_buttons import message_has_inline_buttons
    row = _row(group.id, msg.message_id)
    row.original_known = True
    row.source_username = row.source_username or getattr(getattr(msg, 'chat', None), 'username', None)
    row.has_buttons = bool(row.has_buttons or message_has_inline_buttons(msg))
    row.source_album = row.source_album or getattr(msg, 'media_group_id', None)
    row.has_caption = bool(row.has_caption or getattr(msg, 'caption', None) or getattr(msg, 'text', None))
    db.session.commit()
    return row


def record_forward(channel, discussion, msg):
    if not channel or not channel.is_active or not discussion.is_active or not msg.is_automatic_forward:
        return None
    origin = getattr(msg, 'forward_origin', None)
    source_id = getattr(origin, 'message_id', None) or getattr(msg, 'forward_from_message_id', None)
    if not source_id:
        return None  # Never mistake the discussion ID for a source-channel message ID.
    row = _row(channel.id, source_id)
    row.source_username = row.source_username or getattr(msg.sender_chat, 'username', None)
    row.discussion_chat_id = str(discussion.chat_id)
    row.discussion_message_id = msg.message_id
    row.discussion_username = getattr(getattr(msg, 'chat', None), 'username', None)
    db.session.commit()
    return row.message_id


def thread_url(row):
    if not row or not row.discussion_message_id:
        return None
    name = row.discussion_username
    cid = str(row.discussion_chat_id)
    if not name and not cid.startswith('-100'):
        return None
    base = f'https://t.me/{name}' if name else f'https://t.me/c/{cid[4:]}'
    mid = row.discussion_message_id
    return f'{base}/{mid}?thread={mid}'


def append_comment_row(keyboard, url, source_urls=()):
    """Preserve configured rows; add exactly one final 评论 row unless equivalent exists."""
    from telegram import InlineKeyboardButton
    expected = urlsplit(url)
    root = parse_qs(expected.query).get('thread')
    sources = {urlsplit(u).path.rstrip('/').lower() for u in source_urls}
    expected_chat = expected.path.rstrip('/').rsplit('/', 1)[0].lower()
    for row in keyboard:
        for button in row:
            value = getattr(button, 'url', None)
            if not value:
                continue
            link = urlsplit(value)
            if link.netloc.lower() not in ('t.me', 'www.t.me', 'telegram.me', 'telegram.dog'):
                continue
            query = parse_qs(link.query)
            path = link.path.rstrip('/').lower()
            same_thread = query.get('thread') == root and path.rsplit('/', 1)[0] == expected_chat
            # Official alternate syntax: /<chat>/<thread_id>/<message_id>.
            parts = path.rsplit('/', 2)
            same_thread = same_thread or (len(parts) == 3 and parts[0] == expected_chat and [parts[1]] == root)
            comment = query.get('comment', [''])[0]
            same_comment = path in sources and comment.isdigit() and int(comment) > 0
            if same_thread or same_comment:
                return keyboard
    return list(keyboard) + [[InlineKeyboardButton('评论', url=url)]]


async def keyboard_for_post(bot, chat_id, mid, clone_id, keyboard):
    from app.modules.core import routes
    ctx = nullcontext() if has_app_context() or not routes.global_flask_app else routes.global_flask_app.app_context()
    with ctx:
        group = BotGroup.query.filter_by(chat_id=str(chat_id), clone_id=clone_id).first() if has_app_context() else None
        row = ChannelDiscussionPost.query.filter_by(group_id=group.id, message_id=mid).first() if group else None
        if group and not group.is_active:
            return None
        mapped = row if row and row.discussion_message_id else None
        if row and row.source_album:
            mapped = ChannelDiscussionPost.query.filter_by(group_id=group.id, source_album=row.source_album).filter(
                ChannelDiscussionPost.discussion_message_id.isnot(None)).order_by(ChannelDiscussionPost.discussion_message_id).first()
        if mapped:
            url = thread_url(mapped)
            if url:
                # Also recognize an admin-provided per-channel-post comment link.
                sources = [f'https://t.me/c/{str(chat_id)[4:]}/{mid}']
                info = row.source_username if row else None
                if info:
                    sources.append(f'https://t.me/{info}/{mid}')
                return append_comment_row(keyboard, url, sources)
        try:
            info = await bot.get_chat(chat_id)
            linked = getattr(info, 'linked_chat_id', None)
        except Exception as exc:
            print(f'[channel_post_buttons] cannot verify discussion chat={chat_id}: {type(exc).__name__}', flush=True)
            return None
        if isinstance(linked, int) and linked:
            print(f'[channel_post_buttons] waiting discussion root chat={chat_id} msg={mid}', flush=True)
            return None
        return keyboard


def mark_applied(chat_id, mid, clone_id):
    from app.modules.core import routes
    ctx = nullcontext() if has_app_context() or not routes.global_flask_app else routes.global_flask_app.app_context()
    with ctx:
        if not has_app_context():
            return
        group = BotGroup.query.filter_by(chat_id=str(chat_id), clone_id=clone_id).first()
        if group:
            row = ChannelDiscussionPost.query.filter_by(group_id=group.id, message_id=mid).first()
            if row:
                row.applied = True
                db.session.commit()


async def resume_pending(bot, gid, mid):
    from app.modules.core import routes
    from app.services.sync_loop_guard import is_sync_output
    with routes.global_flask_app.app_context():
        group = db.session.get(BotGroup, gid)
        row = ChannelDiscussionPost.query.filter_by(group_id=gid, message_id=mid).first()
        if not group or not group.is_active or not row or not row.original_known:
            return
        if row.source_album:
            key = (group.clone_id, int(group.chat_id), row.source_album)
            if key in routes._CHANNEL_POST_BUTTON_ALBUM_BUFFERS:
                return  # Existing debounce handles the complete album.
            parts = ChannelDiscussionPost.query.filter_by(group_id=gid, source_album=row.source_album,
                                                           original_known=True).all()
            if any(p.has_buttons or p.applied for p in parts):
                return
            row = min(parts, key=lambda p: (not p.has_caption, p.message_id))
        if row.has_buttons or row.applied or is_sync_output(group.chat_id, row.message_id, routes.global_flask_app):
            return
        chat = SimpleNamespace(id=int(group.chat_id), type='channel')
        original = SimpleNamespace(message_id=row.message_id, media_group_id=None, reply_markup=None,
                                   text=None, caption=None)
        await routes._maybe_attach_channel_post_buttons(bot, original, chat, group, group.clone_id)
