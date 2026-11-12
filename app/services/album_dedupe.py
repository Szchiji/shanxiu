"""
Album / media-message deduplication helpers.

Telegram delivers each album item as a separate update with the same
``media_group_id``.  Speech-related side effects (statistics, points,
lottery counts, auto-like, member tags, name-change tracking) must run
once per album, not once per photo.

Captioned media is delivered only on the media path (``message.text`` is
empty; the caption lives in ``message.caption``), so the text handler
and the media handler must not both count the same update.
"""

from __future__ import annotations

import time
from typing import Any, MutableMapping, Optional


# Default window long enough to absorb a late album part, short enough not
# to collapse two independent albums that happen to reuse an id later.
DEFAULT_ALBUM_TTL_SECONDS = 120.0

# Process-wide cache used by the live bot.  Tests should pass their own map.
MEDIA_ENGAGEMENT_CACHE: dict = {}


def album_should_count(
    cache: MutableMapping,
    chat_id: Any,
    media_group_id: Optional[Any],
    *,
    now: Optional[float] = None,
    ttl: float = DEFAULT_ALBUM_TTL_SECONDS,
) -> bool:
    """Return True if this message should be counted as one speech event.

    * Messages without ``media_group_id`` always count (single sticker,
      photo, video, etc.).
    * The first item of an album counts; later items with the same
      ``(chat_id, media_group_id)`` within *ttl* seconds do not.
    """
    ts = time.time() if now is None else now
    _prune_album_cache(cache, ts, ttl)

    if media_group_id is None or media_group_id == '':
        return True

    key = (str(chat_id), str(media_group_id))
    if key in cache:
        return False
    cache[key] = ts
    return True


def _prune_album_cache(cache: MutableMapping, now: float, ttl: float) -> None:
    expired = [k for k, stamped in cache.items() if (now - float(stamped)) > ttl]
    for k in expired:
        cache.pop(k, None)


def should_skip_text_path_duplicate(message) -> bool:
    """True when a media update also has ``message.text`` (should be rare).

    In Telegram Bot API a photo/video never carries ``text`` (only
    ``caption``).  This guard exists so a future filter change cannot
    let the text path and the media path each award points once.
    """
    if message is None:
        return False
    text = getattr(message, 'text', None)
    return bool(text and str(text).strip())


def caption_or_empty(message) -> str:
    """Return caption text, or empty string for pure media."""
    if message is None:
        return ''
    caption = getattr(message, 'caption', None)
    if caption is None:
        return ''
    return str(caption).strip()
