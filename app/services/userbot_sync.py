"""Feed messages heard by the userbot (小号) into the sync pipeline; the MAIN bot sends.

Threading: the Telethon client runs on its own asyncio loop (``app.userbot.manager``).
It converts each Telethon message into a plain :class:`UbMessage` (no Telethon objects),
picks matching rules (:func:`plans_for_item`, pure) and submits :func:`process_item` to the
PTB bot loop, where album buffering (shared helpers in ``routes``) and sending happen.

Delivery per target:
* no name prefix → ``copy_message`` / ``copy_messages`` (main bot must be in the source chat);
* name prefix, ``forward`` style → ``forward_message(s)``, falls back to the newline style;
* name prefix, ``newline`` style → text: ``send_message`` (prefix + HTML, no source access
  needed); media: ``copy_message`` with a new caption; albums: name line, then ``copy_messages``;
* if copying fails (main bot not in source / protected content) → the userbot downloads the
  media and the main bot re-uploads it (text is simply re-sent).
"""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable, Optional

from app.services.sync_service import (
    CAPTION_LIMIT,
    build_group_sender_prefix,
    combine_prefix_html,
    keyword_blocks_sync,
    normalize_prefix_style,
    prefix_fits,
)

TEXT_LIMIT = 4096
ALBUM_KINDS = ('photo', 'video', 'document', 'audio')
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # Bot API upload limit

DownloadFn = Callable[[int, int], Awaitable[Optional[tuple]]]


@dataclass
class UbMessage:
    chat_id: int
    message_id: int
    chat_title: str = ''
    grouped_id: Optional[str] = None
    text: str = ''          # raw text or caption
    html: str = ''          # Telegram-HTML of text/caption
    media_kind: Optional[str] = None  # photo|video|animation|audio|voice|video_note|sticker|document|other
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    sender_id: Optional[int] = None
    sender_name: str = ''
    sender_username: Optional[str] = None
    sender_is_bot: bool = False
    is_forward: bool = False


# ---------------------------------------------------------------------------
# Pure selection logic
# ---------------------------------------------------------------------------

def parse_sender_filter(text: Optional[str]) -> tuple[set, set]:
    """'@abc_bot, 12345\n@Other' → ({12345}, {'abc_bot', 'other'})."""
    ids, names = set(), set()
    for tok in re.split(r'[\s,，;；]+', text or ''):
        tok = tok.strip()
        if not tok:
            continue
        if re.fullmatch(r'-?\d+', tok):
            ids.add(int(tok))
        else:
            names.add(tok.lstrip('@').lower())
    return ids, names


def sender_matches(mode: Optional[str], sender_filter: Optional[str], item: UbMessage) -> bool:
    mode = (mode or 'bots').lower()
    if mode == 'all':
        return True
    if not item.sender_is_bot:
        return False
    if mode == 'selected':
        ids, names = parse_sender_filter(sender_filter)
        if not ids and not names:
            return False
        return (item.sender_id in ids) or ((item.sender_username or '').lower() in names)
    return True  # 'bots'


def parse_targets(raw: Any) -> list:
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        try:
            items = json.loads(raw or '[]')
        except (TypeError, ValueError):
            items = re.split(r'[\s,，]+', str(raw or ''))
        if not isinstance(items, list):
            items = [items]
    out = []
    for t in items:
        s = str(t).strip()
        if re.fullmatch(r'-?\d+', s) and s not in out:
            out.append(s)
    return out


def build_prefix(item: UbMessage, include: bool) -> str:
    if not include:
        return ''
    user = SimpleNamespace(id=item.sender_id, first_name=item.sender_name or item.sender_username or '',
                           last_name='')
    return build_group_sender_prefix(user, True)


def plans_for_item(rules: Iterable[dict], item: UbMessage, *, own_bot_ids: Iterable[int] = (),
                   main_synced_pairs: Iterable[tuple] = ()) -> list:
    """Return per-target send plans for *item* (deduped by target).

    * messages from our own main/clone bots are never relayed (anti-loop);
    * in 'all' mode, non-bot messages are skipped for targets the main bot already syncs
      itself from the same source (the Bot API delivers those to us directly).
    """
    if item.sender_id is not None and item.sender_id in set(own_bot_ids or ()):
        return []
    synced = {(str(s), str(t)) for s, t in (main_synced_pairs or ())}
    src = str(item.chat_id)
    plans, seen = [], set()
    for rule in rules or ():
        if not rule.get('enabled', True) or str(rule.get('source_chat_id')) != src:
            continue
        if not sender_matches(rule.get('sender_mode'), rule.get('sender_filter'), item):
            continue
        if item.media_kind and not rule.get('sync_media', True):
            continue
        for target in parse_targets(rule.get('target_chat_ids')):
            if target == src or target in seen:
                continue
            if not item.sender_is_bot and (src, target) in synced:
                continue
            seen.add(target)
            plans.append({
                'rule_id': rule.get('id'),
                'target_chat_id': target,
                'sender_prefix': build_prefix(item, bool(rule.get('include_sender_prefix'))),
                'prefix_style': normalize_prefix_style(rule.get('sender_prefix_style')),
                'filter_keywords': rule.get('filter_keywords') or '[]',
                'source_group_id': rule.get('source_group_id'),
            })
    return plans


class RecentSeen:
    """Small TTL set of (chat_id, message_id) so a message is relayed at most once."""

    def __init__(self, ttl: float = 900.0, max_items: int = 5000):
        self.ttl, self.max_items = ttl, max_items
        self._d: OrderedDict = OrderedDict()

    def check_and_add(self, key) -> bool:
        """True if *key* is new (and records it)."""
        now = time.monotonic()
        while self._d:
            k, ts = next(iter(self._d.items()))
            if now - ts > self.ttl or len(self._d) > self.max_items:
                self._d.popitem(last=False)
            else:
                break
        if key in self._d:
            return False
        self._d[key] = now
        return True


# ---------------------------------------------------------------------------
# Delivery (runs on the PTB bot loop)
# ---------------------------------------------------------------------------

def _mid(result) -> Optional[int]:
    if result is None:
        return None
    if isinstance(result, (list, tuple)):
        return _mid(result[0]) if result else None
    return getattr(result, 'message_id', None)


async def _send_header(bot, target, prefix):
    from telegram import LinkPreviewOptions
    return await bot.send_message(chat_id=target, text=(prefix or '').rstrip('\n') or '未知',
                                  parse_mode='HTML', link_preview_options=LinkPreviewOptions(is_disabled=True))


async def _send_text(bot, target, item: UbMessage, prefix: str):
    from telegram import LinkPreviewOptions
    body = item.html or ''
    if prefix and not prefix_fits(prefix, item.text, TEXT_LIMIT):
        await _send_header(bot, target, prefix)
        prefix = ''
    text = combine_prefix_html(prefix, body) if prefix else body
    return await bot.send_message(chat_id=target, text=text or '(空消息)', parse_mode='HTML',
                                  link_preview_options=LinkPreviewOptions(is_disabled=False))


async def _reupload_single(bot, target, item: UbMessage, prefix: str, download: Optional[DownloadFn]):
    if not item.media_kind:
        return await _send_text(bot, target, item, prefix)
    if download is None:
        raise RuntimeError('main bot cannot copy and no userbot download available')
    got = await download(item.chat_id, item.message_id)
    if not got:
        raise RuntimeError('userbot media download failed or file too large')
    data, filename = got
    from telegram import InputFile
    header_sep = bool(prefix) and not prefix_fits(prefix, item.text, CAPTION_LIMIT)
    if header_sep:
        await _send_header(bot, target, prefix)
        prefix = ''
    caption = (combine_prefix_html(prefix, item.html) if prefix else item.html) or None
    kw = {'chat_id': target}
    cap_kw = {'caption': caption, 'parse_mode': 'HTML' if caption else None}
    f = InputFile(data, filename=filename or item.file_name or 'file')
    kind = item.media_kind
    if kind == 'photo':
        return await bot.send_photo(photo=f, **kw, **cap_kw)
    if kind == 'video':
        return await bot.send_video(video=f, **kw, **cap_kw)
    if kind == 'animation':
        return await bot.send_animation(animation=f, **kw, **cap_kw)
    if kind == 'audio':
        return await bot.send_audio(audio=f, **kw, **cap_kw)
    if kind == 'voice':
        return await bot.send_voice(voice=f, **kw, **cap_kw)
    if kind == 'video_note':
        return await bot.send_video_note(video_note=f, **kw)
    if kind == 'sticker':
        return await bot.send_sticker(sticker=f, **kw)
    return await bot.send_document(document=f, **kw, **cap_kw)


async def deliver_single(bot, item: UbMessage, plan: dict, download: Optional[DownloadFn] = None):
    """Returns (target_message_id, method)."""
    target = plan['target_chat_id']
    prefix = plan.get('sender_prefix') or ''
    style = plan.get('prefix_style') or 'newline'
    src = item.chat_id

    if prefix and style == 'forward':
        try:
            return _mid(await bot.forward_message(chat_id=target, from_chat_id=src, message_id=item.message_id)), 'forward'
        except Exception as e:
            print(f"[userbot-sync] forward failed → newline style: {type(e).__name__}", flush=True)

    if prefix and not item.media_kind:
        return _mid(await _send_text(bot, target, item, prefix)), 'send_message'

    try:
        if not prefix:
            return _mid(await bot.copy_message(chat_id=target, from_chat_id=src, message_id=item.message_id)), 'copy'
        if prefix_fits(prefix, item.text, CAPTION_LIMIT) and item.media_kind not in ('sticker', 'video_note'):
            caption = combine_prefix_html(prefix, item.html)
            return _mid(await bot.copy_message(chat_id=target, from_chat_id=src, message_id=item.message_id,
                                               caption=caption, parse_mode='HTML')), 'copy'
        await _send_header(bot, target, prefix)
        prefix = ''
        return _mid(await bot.copy_message(chat_id=target, from_chat_id=src, message_id=item.message_id)), 'copy+header'
    except Exception as e:
        print(f"[userbot-sync] copy failed ({type(e).__name__}: {e}) → re-upload via userbot", flush=True)
    return _mid(await _reupload_single(bot, target, item, prefix, download)), 'reupload'


async def _reupload_album(bot, target, items: list, prefix: str, download: Optional[DownloadFn]):
    from telegram import InputFile, InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo
    if download is None:
        raise RuntimeError('main bot cannot copy and no userbot download available')
    cap_idx = next((i for i, m in enumerate(items) if m.text), 0)
    if prefix and not prefix_fits(prefix, items[cap_idx].text, CAPTION_LIMIT):
        await _send_header(bot, target, prefix)
        prefix = ''
    media = []
    for i, m in enumerate(items):
        if m.media_kind not in ALBUM_KINDS:
            continue
        got = await download(m.chat_id, m.message_id)
        if not got:
            continue
        data, filename = got
        cap = m.html or ''
        if i == cap_idx and prefix:
            cap = combine_prefix_html(prefix, cap)
        cap = cap or None
        f = InputFile(data, filename=filename or m.file_name or 'file')
        cls = {'photo': InputMediaPhoto, 'video': InputMediaVideo,
               'document': InputMediaDocument, 'audio': InputMediaAudio}[m.media_kind]
        media.append(cls(media=f, caption=cap, parse_mode='HTML' if cap else None))
    if not media:
        raise RuntimeError('album re-upload: nothing downloadable')
    return await bot.send_media_group(chat_id=target, media=media)


async def deliver_album(bot, items: list, plan: dict, download: Optional[DownloadFn] = None):
    """Send buffered album *items* to one target as ONE media group. Returns (first_id, method)."""
    items = sorted(items, key=lambda m: m.message_id)
    target = plan['target_chat_id']
    prefix = plan.get('sender_prefix') or ''
    style = plan.get('prefix_style') or 'newline'
    src = items[0].chat_id
    ids = [m.message_id for m in items]

    if prefix and style == 'forward':
        try:
            return _mid(await bot.forward_messages(chat_id=target, from_chat_id=src, message_ids=ids)), 'forward_messages'
        except Exception as e:
            print(f"[userbot-sync] album forward failed → newline style: {type(e).__name__}", flush=True)

    header_sent = False
    try:
        if prefix:
            await _send_header(bot, target, prefix)
            header_sent = True
        return _mid(await bot.copy_messages(chat_id=target, from_chat_id=src, message_ids=ids)), (
            'header+copy_messages' if prefix else 'copy_messages')
    except Exception as e:
        print(f"[userbot-sync] album copy failed ({type(e).__name__}: {e}) → re-upload via userbot", flush=True)
    return _mid(await _reupload_album(bot, target, items, '' if header_sent else prefix, download)), 'reupload_album'


def _log(flask_app, item: UbMessage, plan: dict, *, status: str, message_type: str,
         target_message_id=None, error: Optional[str] = None, preview: Optional[str] = None):
    try:
        from app import db
        from app.models import SyncMessageLog
        from app.utils import get_beijing_now
        with flask_app.app_context():
            db.session.add(SyncMessageLog(
                source_group_id=plan.get('source_group_id'),
                target_group_id=str(plan['target_chat_id']),
                source_message_id=item.message_id,
                target_message_id=target_message_id,
                user_id=item.sender_id,
                username=item.sender_username or (item.sender_name or None),
                message_type=message_type,
                content_preview=(preview if preview is not None else (item.text or '')[:100]) or None,
                status=status,
                error_message=(error or None) and str(error)[:500],
                synced_at=get_beijing_now(),
                via='userbot',
            ))
            db.session.commit()
    except Exception as e:  # logging must never break delivery
        print(f"[userbot-sync] log write failed: {type(e).__name__}", flush=True)


async def deliver_to_plans(bot, flask_app, item: UbMessage, plans: list, download: Optional[DownloadFn] = None):
    for plan in plans:
        if keyword_blocks_sync(item.text, plan.get('filter_keywords')):
            _log(flask_app, item, plan, status='filtered', message_type=item.media_kind or 'text')
            continue
        try:
            mid, method = await deliver_single(bot, item, plan, download)
            print(f"[userbot-sync] chat={item.chat_id} msg={item.message_id} -> {plan['target_chat_id']} "
                  f"via {method}", flush=True)
            _log(flask_app, item, plan, status='success', message_type=item.media_kind or 'text',
                 target_message_id=mid)
        except Exception as e:
            print(f"[userbot-sync] send to {plan['target_chat_id']} failed: {type(e).__name__}: {e}", flush=True)
            _log(flask_app, item, plan, status='failed', message_type=item.media_kind or 'text', error=str(e))


# Album buffer (shared debounce helpers from routes; kind='userbot').
USERBOT_MEDIA_GROUP_BUFFERS: dict = {}


def _album_key(item: UbMessage):
    return ('userbot', str(item.chat_id), str(item.grouped_id))


async def flush_userbot_album(buffer_key):
    from app.modules.core import routes
    buf = USERBOT_MEDIA_GROUP_BUFFERS.pop(buffer_key, None)
    if not buf:
        return
    routes._note_album_flushed('userbot', buffer_key, buf)
    items = sorted(buf.get('messages') or [], key=lambda m: m.message_id)
    if not items:
        return
    bot, flask_app, download = buf['bot'], buf['flask_app'], buf.get('download')
    caption = next((m.text for m in items if m.text), '')
    head = items[0]
    for plan in buf.get('plans') or []:
        if keyword_blocks_sync(caption, plan.get('filter_keywords')):
            _log(flask_app, head, plan, status='filtered', message_type='media_group', preview=caption[:100])
            continue
        try:
            mid, method = await deliver_album(bot, items, plan, download)
            print(f"{routes._album_log_prefix('userbot', buffer_key)} -> {plan['target_chat_id']} "
                  f"items={len(items)} via {method}", flush=True)
            _log(flask_app, head, plan, status='success', message_type='media_group',
                 target_message_id=mid, preview=caption[:100])
        except Exception as e:
            print(f"[userbot-sync] album to {plan['target_chat_id']} failed: {type(e).__name__}: {e}", flush=True)
            _log(flask_app, head, plan, status='failed', message_type='media_group', error=str(e),
                 preview=caption[:100])


async def process_item(bot, flask_app, item: UbMessage, plans: list, download: Optional[DownloadFn] = None):
    """Entry point on the bot loop."""
    if not plans:
        return
    if item.grouped_id and item.media_kind in ALBUM_KINDS:
        from app.modules.core import routes
        key = _album_key(item)
        routes._media_group_buffer_add(USERBOT_MEDIA_GROUP_BUFFERS, key, item, kind='userbot', init={
            'bot': bot, 'flask_app': flask_app, 'plans': plans, 'download': download,
        })
        routes._schedule_media_group_flush(USERBOT_MEDIA_GROUP_BUFFERS, key, flush_userbot_album,
                                           routes._SYNC_MEDIA_GROUP_FLUSH_DELAY)
        return
    await deliver_to_plans(bot, flask_app, item, plans, download)
