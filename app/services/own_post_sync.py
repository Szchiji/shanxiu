"""Sync posts that OUR bot sends into a channel (定时消息、广播、立即发送 …).

Telegram never delivers a bot's own outgoing messages back to it as ``channel_post``
updates, so :func:`handle_sync_channel_posts` never sees them. We hook the bot's send
calls instead (class-level wrapper on ``telegram.ext.ExtBot``): after a successful send
into a channel that is an enabled sync source with 「同步主机器人发的帖子」 on, the sent
message(s) are fed to the normal channel-sync core (albums buffered together, buttons
copied the same way as for any post).

Loop guard: sends made by sync / forward-rule delivery run inside ``TrackingBot`` which
sets ``IN_SYNC_DELIVERY`` → ignored here; after a short delay we also re-check the
registry (``is_sync_output``) before syncing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

# Methods whose result is Message / tuple[Message]
MESSAGE_METHODS = frozenset({
    'send_message', 'send_photo', 'send_video', 'send_animation', 'send_audio', 'send_voice',
    'send_video_note', 'send_sticker', 'send_document', 'send_poll', 'send_location', 'send_venue',
    'send_contact', 'send_dice', 'send_media_group', 'forward_message',
})
# Methods whose result is MessageId / tuple[MessageId] (content unknown → copy by id)
ID_METHODS = frozenset({'copy_message', 'copy_messages', 'forward_messages'})

CACHE_TTL = 30.0
SETTLE_DELAY = 1.5  # let TrackingBot / SyncMessageLog register sync outputs first

_bots: dict = {}          # id(bot) -> clone_id
_sources_cache: dict = {}  # clone_id -> (ts, frozenset(chat_id str))
_installed = False


def register_bot(bot, clone_id) -> None:
    """Enable the hook for *bot* (main bot: clone_id=None)."""
    if bot is None:
        return
    _bots[id(bot)] = clone_id
    install()


def invalidate() -> None:
    _sources_cache.clear()


def _chat_id_of(args, kwargs) -> Optional[str]:
    cid = kwargs.get('chat_id', args[0] if args else None)
    try:
        return str(int(cid))
    except (TypeError, ValueError):
        return None


def own_sync_sources(clone_id, flask_app=None) -> frozenset:
    """Channel chat_ids (str) that have ≥1 enabled sync target with sync_own_bot_posts on."""
    now = time.monotonic()
    hit = _sources_cache.get(clone_id)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    from app.models import BotGroup, SyncGroupMessages
    if flask_app is None:
        from app.modules.core import routes
        flask_app = routes.global_flask_app
    out = set()
    try:
        with flask_app.app_context():
            q = BotGroup.query
            q = q.filter(BotGroup.clone_id.is_(None)) if clone_id is None else q.filter_by(clone_id=clone_id)
            # Channels only: supergroup ids also start with -100, and in groups our own
            # messages are already relayed by the 小号 (userbot) → would be delivered twice.
            groups = {g.id: str(g.chat_id) for g in q.all() if (g.type or '').lower() == 'channel' and g.is_active}
            for s in SyncGroupMessages.query.filter_by(enabled=True).all():
                if s.source_group_id in groups and getattr(s, 'sync_own_bot_posts', True) is not False:
                    out.add(groups[s.source_group_id])
    except Exception as e:
        print(f"[own-post-sync] sources query failed: {type(e).__name__}", flush=True)
    res = frozenset(c for c in out if c.startswith('-100'))
    _sources_cache[clone_id] = (now, res)
    return res


def should_handle(bot, method: str, args, kwargs) -> Optional[tuple]:
    """Return (clone_id, chat_id) if this send must be synced, else None. Pure-ish (no I/O
    except the cached sources lookup)."""
    if id(bot) not in _bots:
        return None
    from app.services.sync_loop_guard import IN_SYNC_DELIVERY
    if IN_SYNC_DELIVERY.get():
        return None
    chat_id = _chat_id_of(args, kwargs)
    if not chat_id or not chat_id.startswith('-100'):
        return None
    clone_id = _bots[id(bot)]
    if chat_id not in own_sync_sources(clone_id):
        return None
    return clone_id, chat_id


async def _process(bot, clone_id, chat_id: str, method: str, kwargs: dict, result) -> None:
    from app.services.sync_loop_guard import is_sync_output
    from app.modules.core import routes
    await asyncio.sleep(SETTLE_DELAY)
    items = list(result) if isinstance(result, (list, tuple)) else [result]
    if method in MESSAGE_METHODS:
        for m in items:
            mid = getattr(m, 'message_id', None)
            if mid is None or is_sync_output(chat_id, mid, routes.global_flask_app):
                continue
            chat = getattr(m, 'chat', None)
            if chat is None or str(getattr(chat, 'type', '') or '').lower() != 'channel':
                continue
            await routes._sync_channel_post_core(bot, m, chat, getattr(m, 'from_user', None), clone_id,
                                                 own_bot_post=True)
        return
    ids = [getattr(x, 'message_id', None) for x in items]
    ids = [i for i in ids if isinstance(i, int)
           and not is_sync_output(chat_id, i, routes.global_flask_app)]
    if ids:
        await sync_ids_by_copy(bot, clone_id, chat_id, ids, kwargs.get('reply_markup'))


async def sync_ids_by_copy(bot, clone_id, chat_id: str, ids: list, reply_markup=None) -> None:
    """copy_message(s)/forward_messages results carry only ids → copy those ids onward."""
    from app import db
    from app.models import BotGroup, SyncGroupMessages, SyncMessageLog
    from app.modules.core import routes
    from app.services.sync_loop_guard import track_bot
    from app.services.sync_service import build_channel_sender_prefix, is_same_source_target, normalize_prefix_style
    from app.utils import get_beijing_now
    tbot = track_bot(bot)
    flask_app = routes.global_flask_app
    with flask_app.app_context():
        q = BotGroup.query.filter_by(chat_id=chat_id, type='channel')
        q = q.filter(BotGroup.clone_id.is_(None)) if clone_id is None else q.filter_by(clone_id=clone_id)
        group = q.first()
        if not group or not group.is_active:
            return
        settings = [s for s in SyncGroupMessages.query.filter_by(source_group_id=group.id, enabled=True).all()
                    if getattr(s, 'sync_own_bot_posts', True) is not False]
        print(f"[sync] own-bot channel post (ids) chat={chat_id} ids={ids} targets={len(settings)}", flush=True)
        try:
            chat = await bot.get_chat(int(chat_id))
        except Exception:
            chat = None
        from app.services.channel_admin import source_metadata
        metadata = source_metadata(chat_id, ids, clone_id)
        for s in settings:
            selected, reasons = filter_id_posts(s, ids, metadata)
            target = s.target_group_id
            if reasons:
                db.session.add(SyncMessageLog(source_group_id=group.id, target_group_id=str(target),
                    source_message_id=ids[0], status='filtered', error_message='; '.join(reasons)[:500],
                    synced_at=get_beijing_now()))
                db.session.commit()
            if not selected:
                continue
            if is_same_source_target(chat_id, target):
                continue
            status, err, first = 'success', None, None
            try:
                prefix = build_channel_sender_prefix(chat, bool(getattr(s, 'include_sender_prefix', False)))
                style = normalize_prefix_style(getattr(s, 'sender_prefix_style', None))
                if prefix and style == 'forward':
                    sent = await tbot.forward_messages(chat_id=target, from_chat_id=int(chat_id), message_ids=selected)
                else:
                    if prefix:
                        from telegram import LinkPreviewOptions
                        await tbot.send_message(chat_id=target, text=prefix.rstrip('\n'), parse_mode='HTML',
                                                link_preview_options=LinkPreviewOptions(is_disabled=True))
                    if len(selected) == 1:
                        kw = dict(chat_id=target, from_chat_id=int(chat_id), message_id=selected[0])
                        if reply_markup is not None:
                            kw['reply_markup'] = reply_markup
                        sent = [await tbot.copy_message(**kw)]
                    else:
                        sent = await tbot.copy_messages(chat_id=target, from_chat_id=int(chat_id), message_ids=selected)
                first = getattr(sent[0], 'message_id', None) if sent else None
            except Exception as e:
                status, err = 'failed', str(e)[:500]
                print(f"[sync] own-bot ids copy to {target} failed: {type(e).__name__}: {e}", flush=True)
            db.session.add(SyncMessageLog(
                source_group_id=group.id, target_group_id=str(target), source_message_id=selected[0],
                target_message_id=first, message_type='media_group' if len(selected) > 1 else 'copy',
                status=status, error_message=err, synced_at=get_beijing_now(),
            ))
            db.session.commit()


def filter_id_posts(setting, ids, metadata):
    """Filter whole albums; unknown content fails closed only for content restrictions.
    Bot API cannot fetch historical text by message id. Never leak unknown content
    through a configured blacklist or media restriction.
    """
    import json
    from app.services.sync_service import keyword_blocks_sync
    try:
        keywords = json.loads(setting.filter_keywords or '[]')
        if not isinstance(keywords, list):
            raise ValueError('invalid blacklist')
        invalid = False
    except (ValueError, TypeError):
        keywords = []
        invalid = True
    groups = {}
    for mid in ids:
        meta = metadata.get(mid, {})
        groups.setdefault(meta.get('post_key', f'unknown:{mid}'), []).append(mid)
    selected, reasons = [], []
    for mids in groups.values():
        metas = [metadata.get(mid, {}) for mid in mids]
        unknown = any(not m or m.get('media_type') == 'unknown' for m in metas)
        text = '\n'.join(m.get('text', '') for m in metas)
        reason = None
        if invalid:
            reason = '黑名单格式无效，安全跳过'
        elif not setting.sync_forwards and any(m.get('is_forward') for m in metas):
            reason = '已关闭同步转发内容'
        elif unknown and (keywords or not setting.sync_media):
            reason = '来源内容不可读取，无法校验关键词/媒体限制，安全跳过'
        elif not setting.sync_media and any(m.get('media_type') != 'text' for m in metas):
            reason = '已关闭同步媒体文件'
        elif keywords and keyword_blocks_sync(text, setting.filter_keywords):
            reason = '命中黑名单关键词'
        if reason:
            reasons.append(reason)
        else:
            selected.extend(mids)
    return selected, list(dict.fromkeys(reasons))


async def observe_result(clone_id, chat_id, method, payload, result):
    """Count all active-channel sends, including sync outputs (but never relay those).
    ID-only copies use locally observed source metadata; no credential or history fetch.
    """
    from types import SimpleNamespace
    from app.modules.core import routes
    from app.models import BotGroup
    from app.services.channel_admin import record_post, source_metadata
    if routes.global_flask_app is None:
        return
    items = list(result) if isinstance(result, (tuple, list)) else [result]
    if not items or not any(isinstance(getattr(m, 'message_id', None), int) for m in items):
        return
    with routes.global_flask_app.app_context():
        group = BotGroup.query.filter_by(chat_id=chat_id, clone_id=clone_id, type='channel').first()
        if not group or not group.is_active:
            return
        if method in MESSAGE_METHODS:
            for msg in items:
                if getattr(getattr(msg, 'chat', None), 'type', None) == 'channel':
                    record_post(group, msg)
            return
        source_ids = payload.get('message_ids') or [payload.get('message_id')]
        metadata = source_metadata(payload.get('from_chat_id'), source_ids, clone_id)
        mapped = len(items) == len(source_ids)
        first = items[0].message_id
        for i, msg in enumerate(items):
            meta = metadata.get(source_ids[i], {}) if mapped else {}
            origin_key = meta.get('post_key', 'unknown-batch')
            key = f'copy:{first}:{origin_key}'
            text = payload.get('caption') if payload.get('caption') is not None else meta.get('text', '')
            record_post(group, SimpleNamespace(message_id=msg.message_id), batch_key=key,
                        media_type=meta.get('media_type', 'unknown'), text=text,
                        is_forward=method == 'forward_messages')


async def _attach_own_post_buttons(bot, clone_id, chat_id, method, payload, result):
    """No sync target is required for original-post buttons. Never overwrite known buttons."""
    from types import SimpleNamespace
    from app.modules.core import routes
    from app.models import BotGroup
    from app.services.sync_loop_guard import IN_SYNC_DELIVERY, is_sync_output
    if IN_SYNC_DELIVERY.get() or routes.global_flask_app is None:
        return
    await asyncio.sleep(SETTLE_DELAY)
    items = list(result) if isinstance(result, (tuple, list)) else [result]
    with routes.global_flask_app.app_context():
        group = BotGroup.query.filter_by(chat_id=str(chat_id), clone_id=clone_id, type='channel').first()
        if not group or not group.is_active:
            return
        for msg in items:
            mid = getattr(msg, 'message_id', None)
            if not isinstance(mid, int) or is_sync_output(chat_id, mid, routes.global_flask_app):
                continue
            chat = getattr(msg, 'chat', None)
            if chat is None:
                # copyMessage(s) creates new messages without inherited keyboards; an
                # explicit keyboard belongs to the sender and must never be replaced.
                # forwardMessages only returns IDs and can retain source keyboards:
                # absence cannot be verified, so fail closed rather than overwrite.
                if method == 'forward_messages':
                    print(f"[channel_post_buttons] skip unverifiable forwarded keyboard chat={chat_id} msg={mid}", flush=True)
                    continue
                chat = SimpleNamespace(id=int(chat_id), type='channel')
                msg = SimpleNamespace(message_id=mid, chat=chat, reply_markup=payload.get('reply_markup'),
                    media_group_id=f'copy:{items[0].message_id}' if len(items) > 1 else None,
                    text=payload.get('text'), caption=payload.get('caption'))
            if getattr(chat, 'type', None) == 'channel':
                await routes._maybe_attach_channel_post_buttons(bot, msg, chat, group, clone_id)


async def _after_send(bot, clone_id, chat_id, method, payload, result, hit):
    try:
        await observe_result(clone_id, chat_id, method, payload, result)
        if hit:
            await _process(bot, clone_id, chat_id, method, payload, result)
        if not hit or method in ID_METHODS:
            await _attach_own_post_buttons(bot, clone_id, chat_id, method, payload, result)
    except Exception as e:
        print(f"[own-post-sync] post processing failed: {type(e).__name__}", flush=True)


def _wrap(name, orig):
    async def wrapper(self, *args, **kwargs):
        result = await orig(self, *args, **kwargs)
        try:
            payload = dict(kwargs)
            # Preserve positional chat/source/message arguments as well as kwargs.
            import inspect
            try:
                bound = inspect.signature(orig).bind(self, *args, **kwargs)
                payload.update({k: v for k, v in bound.arguments.items() if k not in ('self', 'args', 'kwargs')})
            except (TypeError, ValueError):
                pass

            hit = should_handle(self, name, args, kwargs)
            chat_id = _chat_id_of(args, kwargs)
            if id(self) in _bots and chat_id and chat_id.startswith('-100'):
                asyncio.get_running_loop().create_task(
                    _after_send(self, _bots[id(self)], chat_id, name, payload, result, hit))
        except Exception as e:
            print(f"[own-post-sync] hook error: {type(e).__name__}: {e}", flush=True)
        return result
    wrapper.__wrapped__ = orig
    wrapper.__name__ = name
    return wrapper


def install() -> None:
    """Patch ExtBot send methods once (class level — PTB Bot instances are frozen)."""
    global _installed
    if _installed:
        return
    from telegram.ext import ExtBot
    for name in MESSAGE_METHODS | ID_METHODS:
        orig = getattr(ExtBot, name, None)
        if orig is None or getattr(orig, '__wrapped__', None) is not None:
            continue
        setattr(ExtBot, name, _wrap(name, orig))
    _installed = True
