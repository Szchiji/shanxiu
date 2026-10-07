"""Helpers for channel discussion automation (forward rules / templates / coupons).

UI under a channel BotGroup saves ChannelForwardRule etc. with that channel's
group_id. Runtime handle_channel_pin runs in the *linked discussion group* and
must resolve configs from the source channel (sender_chat), falling back to the
discussion group for legacy rows.
"""

from __future__ import annotations

from typing import Any, Optional


def resolve_channel_automation_group_id(
    channel_bot_group_id: Optional[int],
    discussion_bot_group_id: int,
) -> int:
    """Prefer channel BotGroup id (where the admin UI stores rules); else discussion."""
    if channel_bot_group_id is not None:
        return int(channel_bot_group_id)
    return int(discussion_bot_group_id)


def should_delete_as_promote(
    auto_delete_promote_msg: bool,
    is_automatic_forward: bool,
) -> bool:
    """Mutually-promo delete must not swallow linked-channel discussion posts.

    Linked discussion messages use ``is_automatic_forward`` and have their own
    toggle (auto_delete_channel_discussion_msg). Treating them as 互推 would
    delete them before forward rules / templates can run.
    """
    if not auto_delete_promote_msg:
        return False
    if is_automatic_forward:
        return False
    return True


def message_filter_text(msg: Any) -> Optional[str]:
    """Text or caption for keyword filters (media captions included)."""
    if msg is None:
        return None
    text = getattr(msg, "text", None)
    if text:
        return text
    return getattr(msg, "caption", None) or None
