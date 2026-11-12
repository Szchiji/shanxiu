"""Bounded read-only roster fetch on the existing Telethon userbot loop.

Bot API cannot enumerate members. A partial/hidden/truncated roster must never
be used for an 'all members' draw. No logins, joins, messages or DB sync here.
"""
import asyncio

ROSTER_TIMEOUT = 20
MAX_ROSTER_MEMBERS = 20000


class LiveRosterError(ValueError):
    """Safe Chinese explanation suitable for the admin UI (no raw RPC text)."""


async def _collect(client, chat_id):
    from telethon import types, functions
    from telethon.errors import FloodWaitError

    if client is None or not client.is_connected():
        raise LiveRosterError('全部成员开奖需要小号已登录、在线且在该群中；请检查小号状态后重试。')
    try:
        if not await client.is_user_authorized():
            raise LiveRosterError('全部成员开奖需要已登录的小号，请检查小号状态。')
        entity = await client.get_entity(int(chat_id))
        if not isinstance(entity, (types.Channel, types.Chat)) or getattr(entity, 'left', False):
            raise LiveRosterError('全部成员开奖需要小号在该群中；本次未开奖。')
        # Access to a public chat does not mean this account is a member.
        await client.get_permissions(entity, 'me')
        if isinstance(entity, types.Channel):
            full = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
            if getattr(full, 'participants_hidden', False) or not getattr(full, 'can_view_participants', False):
                raise LiveRosterError('Telegram 隐藏或限制了成员名单，小号无法读取完整名单；本次未开奖。')
            expected = full.participants_count
        else:
            full = (await client(functions.messages.GetFullChatRequest(entity.id))).full_chat
            participants = getattr(full, 'participants', None)
            if not isinstance(participants, types.ChatParticipants):
                raise LiveRosterError('小号无法读取完整群成员名单；本次未开奖。')
            expected = len(participants.participants)
        if expected is None or expected <= 0:
            raise LiveRosterError('无法确认 Telegram 完整成员数量；本次未开奖。')
        if expected > MAX_ROSTER_MEMBERS:
            raise LiveRosterError(f'群成员超过 {MAX_ROSTER_MEMBERS} 人的安全上限，无法在限时内保证完整名单；本次未开奖。')
        seen, eligible = set(), []
        async for user in client.iter_participants(entity, limit=None):
            if user.id in seen:
                continue
            seen.add(user.id)
            if len(seen) > MAX_ROSTER_MEMBERS:
                raise LiveRosterError('群成员超过现场拉取安全上限；本次未开奖。')
            if not getattr(user, 'bot', False) and not getattr(user, 'deleted', False):
                eligible.append(int(user.id))
        # Telegram can return fewer than requested, even without an RPC error.
        # A concurrent join/leave can also cause this: retry rather than bias.
        if len(seen) != expected:
            raise LiveRosterError('Telegram 返回的名单不完整或成员正在变动，请稍后重试；本次未开奖。')
        return eligible
    except LiveRosterError:
        raise
    except FloodWaitError as error:
        raise LiveRosterError(f'Telegram 限流，请约 {error.seconds} 秒后重试；本次未开奖。') from None
    except Exception:
        raise LiveRosterError('现场拉取成员失败：请确认小号已登录、在该群中且有权查看完整名单；本次未开奖。') from None


async def _bounded_collect(client, chat_id):
    try:
        return await asyncio.wait_for(_collect(client, chat_id), timeout=ROSTER_TIMEOUT)
    except asyncio.TimeoutError:
        raise LiveRosterError(f'现场拉取成员超过 {ROSTER_TIMEOUT} 秒（可能限流或群过大），请稍后重试；本次未开奖。') from None


def fetch_live_member_ids(chat_id):
    from app.userbot.manager import userbot_manager, UserbotError
    # Reuse the one running, logged-in account. Never decrypt/open a new session.
    if userbot_manager.client is None or userbot_manager.loop is None:
        raise LiveRosterError('全部成员开奖需要小号已登录、在线且在该群中；请检查小号状态后重试。')
    try:
        return userbot_manager.run(_bounded_collect(userbot_manager.client, chat_id), timeout=ROSTER_TIMEOUT + 2)
    except UserbotError:
        raise LiveRosterError('现场拉取成员超时或小号不可用；本次未开奖，请检查小号后重试。') from None
