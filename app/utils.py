"""
app/utils.py
------------
Shared utility functions used across bot handlers, services, and web
routes.  This module must NOT import from ``app.modules.core.routes`` to
avoid circular dependencies.  It may import from ``app.models`` and
``app.bot.state``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

BEIJING_TZ = pytz.timezone('Asia/Shanghai')


def get_beijing_now() -> datetime:
    """Return the current time in Beijing timezone as a naive datetime (for DB storage)."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def get_beijing_today() -> datetime:
    """Return today's date at midnight in Beijing timezone as a naive datetime."""
    now = datetime.now(BEIJING_TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


# ---------------------------------------------------------------------------
# Chat-permission helpers
# ---------------------------------------------------------------------------

def get_muted_permissions() -> ChatPermissions:
    """Return ChatPermissions that mute a member (send_messages=False only).

    Using the minimal form is the most compatible approach across Telegram
    clients and API versions.
    """
    return ChatPermissions(can_send_messages=False)


def get_unrestricted_permissions() -> ChatPermissions:
    """Return ChatPermissions that restore the default rights for a member."""
    return ChatPermissions(
        can_send_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

async def is_user_admin_in_group(bot, chat_id: int, user_id: int) -> bool:
    """Return True if *user_id* is an admin or creator in *chat_id*."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ('creator', 'administrator')
    except Exception as exc:
        print(f"Error checking admin status: {exc}")
        return False


async def is_user_chat_owner(bot, chat_id: int, user_id: int) -> bool:
    """Return True if *user_id* is the creator (owner) of *chat_id*."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status == 'creator'
    except Exception as exc:
        print(f"Error checking chat owner status: {exc}")
        return False


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def convert_chat_id_to_int(chat_id, group_id=None, group_title=None):
    """Safely convert *chat_id* to ``int`` for the Telegram API.

    Returns ``None`` and prints an error on failure (instead of raising).
    """
    try:
        return int(chat_id)
    except (ValueError, TypeError) as exc:
        msg = f"Invalid chat_id '{chat_id}'"
        if group_id:
            msg += f" for group {group_id}"
        if group_title:
            msg += f" ({group_title})"
        msg += f": {exc}"
        print(msg)
        return None


def log_admin_action(
    group_id: int,
    admin_id: int,
    admin_name: str,
    action_type: str,
    target_user_id=None,
    target_user_name=None,
    details=None,
):
    """Persist an admin action to the database.

    Requires an active Flask application context.
    """
    from app import db
    from app.models import AdminActionLog

    try:
        log = AdminActionLog(
            group_id=group_id,
            admin_id=admin_id,
            admin_name=admin_name,
            action_type=action_type,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            details=details,
        )
        db.session.add(log)
        db.session.commit()
    except Exception as exc:
        print(f"Error logging admin action: {exc}")


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def safe_int(val, default: int = 0) -> int:
    """Convert *val* to int, returning *default* on failure."""
    if val is None:
        return default
    if isinstance(val, str) and val.strip() == '':
        return default
    try:
        return int(val)
    except Exception:
        return default


def parse_telegram_message_link(url: str):
    """Parse a ``t.me`` message link into ``(from_chat_id, message_id)``.

    Supports both username-based and private-channel links:
    - ``https://t.me/username/42``      → ``('@username', 42)``
    - ``https://t.me/c/1234567890/42``  → ``(-1001234567890, 42)``

    Returns ``(None, None)`` when the URL cannot be parsed.
    """
    if not url:
        return None, None
    m = re.match(r'(?:https?://)?t\.me/c/(\d+)/(\d+)', url)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2))
    m = re.match(r'(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)', url)
    if m:
        return f"@{m.group(1)}", int(m.group(2))
    return None, None


def build_inline_keyboard_from_links(links: list) -> list:
    """Convert a list of link dicts into rows for an InlineKeyboardMarkup.

    Each dict may contain ``text``, ``url``, ``row`` (int), and ``order`` (int).
    Buttons are grouped by ``row`` and sorted by ``order`` within each row.
    """
    if not links:
        return []

    rows_dict: dict = {}
    for link in links:
        if not link.get('text') or not link.get('url'):
            continue
        row_num = link.get('row', 0)
        order = link.get('order', 0)
        rows_dict.setdefault(row_num, []).append({
            'button': InlineKeyboardButton(link['text'], url=link['url']),
            'order': order,
        })

    keyboard = []
    for row_num in sorted(rows_dict.keys()):
        row_buttons = sorted(rows_dict[row_num], key=lambda x: x['order'])
        keyboard.append([btn['button'] for btn in row_buttons])

    return keyboard
