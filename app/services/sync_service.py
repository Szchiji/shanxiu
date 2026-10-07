"""
Helpers for cross-group / channel message sync.

Pure functions used by handle_sync_group_messages and handle_sync_channel_posts
so unit tests can cover keyword filtering and anti-loop checks without a live bot.
"""

from __future__ import annotations

import json
from typing import Any, Optional


def keyword_blocks_sync(text: Optional[str], filter_keywords_json: Optional[str]) -> bool:
    """Return True if *text* hits any blacklist keyword in the JSON array.

    Sync filter is blacklist-only: messages containing a listed keyword are
    skipped. Empty / invalid JSON never blocks.
    """
    if not text or not filter_keywords_json:
        return False
    try:
        keywords = json.loads(filter_keywords_json)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(keywords, list):
        return False
    return any(isinstance(kw, str) and kw and kw in text for kw in keywords)


def should_skip_bot_sender(user: Any) -> bool:
    """Skip syncing messages sent by bots (anti-loop with clone bots)."""
    return bool(user is not None and getattr(user, "is_bot", False))


def is_same_source_target(source_chat_id: Any, target_group_id: Any) -> bool:
    """True when source chat id equals configured target (would sync to self)."""
    if source_chat_id is None or target_group_id is None:
        return False
    return str(source_chat_id) == str(target_group_id)


def sync_filter_text(msg: Any) -> Optional[str]:
    """Text used for keyword blacklist: message text or caption."""
    if msg is None:
        return None
    text = getattr(msg, "text", None)
    if text:
        return text
    return getattr(msg, "caption", None) or None


def is_group_chat(chat: Any) -> bool:
    """True for Telegram group / supergroup chats."""
    return bool(chat is not None and getattr(chat, "type", None) in ("group", "supergroup"))


def is_channel_chat(chat: Any) -> bool:
    """True for Telegram channel chats (channel_post source)."""
    return bool(chat is not None and getattr(chat, "type", None) == "channel")


def build_group_sender_prefix(user: Any, include_prefix: bool = False) -> str:
    """Optional prefix for group member sync, e.g. ``[Alice] ``.

    Default *include_prefix* is False so synced messages look like the original
    (same clean look as channel posts). When True, prepend ``[发送者名] ``.
    """
    if not include_prefix:
        return ""
    if user is None:
        return "[未知] "
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    name = f"{first} {last}".strip() or "未知"
    return f"[{name}] "


def build_channel_sender_prefix(chat: Any) -> str:
    """Prefix for channel post sync.

    Channel posts are always copied as-is (no ``[频道名]`` prefix). *chat* is
    accepted for API stability / call-site compatibility.
    """
    return ""


def fetch_chat_info(bot_token: str, chat_id: str, timeout: float = 10.0) -> dict:
    """Call Telegram getChat and return a normalized dict.

    Returns ``{"ok": True, "id": str, "title": str, "type": str, "username": str|None}``
    or ``{"ok": False, "error": str}``. Does not raise on Telegram API errors.
    """
    import requests as _requests

    if not bot_token:
        return {"ok": False, "error": "缺少 Bot Token"}
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return {"ok": False, "error": "目标 chat_id 为空"}

    try:
        resp = _requests.get(
            f"https://api.telegram.org/bot{bot_token}/getChat",
            params={"chat_id": chat_id},
            timeout=timeout,
        )
        data = resp.json()
    except Exception as e:
        return {"ok": False, "error": f"网络错误: {e}"}

    if not data.get("ok"):
        return {"ok": False, "error": data.get("description") or "getChat 失败"}

    chat = data.get("result") or {}
    title = chat.get("title") or chat.get("username") or str(chat.get("id", chat_id))
    return {
        "ok": True,
        "id": str(chat.get("id", chat_id)),
        "title": title,
        "type": chat.get("type") or "",
        "username": chat.get("username"),
    }
