"""No Telegram/network: exercise completeness, limits and failure semantics."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telethon import types
from telethon.errors import FloodWaitError

from app.services import lottery_roster as roster


class FakeClient:
    def __init__(self, users=None, expected=4, hidden=False):
        self.users = users if users is not None else [
            types.User(id=1), types.User(id=2, bot=True),
            types.User(id=3, deleted=True), types.User(id=4),
        ]
        self.entity = types.Channel(id=123, title='test', photo=types.ChatPhotoEmpty(), date=None, megagroup=True)
        self.full = NS(participants_count=expected, participants_hidden=hidden, can_view_participants=True)
        self.get_entity = AsyncMock(return_value=self.entity)
        self.get_permissions = AsyncMock(return_value=object())
        self.is_user_authorized = AsyncMock(return_value=True)
        self.iterated = False

    def is_connected(self):
        return True

    async def __call__(self, request):
        return NS(full_chat=self.full)

    async def iter_participants(self, entity, limit=None):
        self.iterated = True
        assert limit is None
        for user in self.users:
            yield user


@pytest.mark.asyncio
async def test_live_roster_excludes_bots_deleted_and_duplicates():
    c = FakeClient()
    c.users.append(types.User(id=1))
    assert await roster._bounded_collect(c, '-100123') == [1, 4]
    c.get_permissions.assert_awaited_once_with(c.entity, 'me')


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [3, 5, None, 0])
async def test_partial_or_unknown_count_refuses_draw(count):
    with pytest.raises(roster.LiveRosterError, match='未开奖'):
        await roster._bounded_collect(FakeClient(expected=count), '-100123')


@pytest.mark.asyncio
async def test_hidden_roster_refuses_without_iterating():
    c = FakeClient(hidden=True)
    with pytest.raises(roster.LiveRosterError, match='隐藏'):
        await roster._bounded_collect(c, '-100123')
    assert not c.iterated


@pytest.mark.asyncio
async def test_cannot_view_roster_refuses():
    c = FakeClient()
    c.full.can_view_participants = False
    with pytest.raises(roster.LiveRosterError, match='限制'):
        await roster._bounded_collect(c, '-100123')


@pytest.mark.asyncio
async def test_large_group_refuses_no_truncated_draw():
    c = FakeClient(expected=roster.MAX_ROSTER_MEMBERS + 1)
    with pytest.raises(roster.LiveRosterError, match='安全上限'):
        await roster._bounded_collect(c, '-100123')
    assert not c.iterated


@pytest.mark.asyncio
async def test_flood_wait_is_clear_and_does_not_use_partial_roster():
    c = FakeClient()
    c.get_permissions.side_effect = FloodWaitError(request=None, capture=80)
    with pytest.raises(roster.LiveRosterError, match='限流.*80'):
        await roster._bounded_collect(c, '-100123')


@pytest.mark.asyncio
async def test_timeout_cancels_fetch(monkeypatch):
    c = FakeClient()
    stopped = []
    async def slow(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)
    monkeypatch.setattr(roster, 'ROSTER_TIMEOUT', 0.01)
    c.get_entity = slow
    with pytest.raises(roster.LiveRosterError, match='超过.*未开奖'):
        await roster._bounded_collect(c, '-100123')
    assert stopped == [True]


@pytest.mark.asyncio
async def test_not_member_or_access_error_is_sanitized():
    c = FakeClient()
    c.get_permissions.side_effect = RuntimeError('PRIVATE_ERROR_MUST_NOT_LEAK')
    with pytest.raises(roster.LiveRosterError, match='小号已登录') as err:
        await roster._bounded_collect(c, '-100123')
    assert 'PRIVATE_ERROR' not in str(err.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['disconnected', 'unauthorized', 'left'])
async def test_unavailable_account_fails(condition):
    c = FakeClient()
    if condition == 'disconnected':
        c.is_connected = lambda: False
    elif condition == 'unauthorized':
        c.is_user_authorized.return_value = False
    else:
        c.entity.left = True
    with pytest.raises(roster.LiveRosterError, match='小号'):
        await roster._bounded_collect(c, '-100123')


def test_sync_bridge_reuses_running_userbot_loop(monkeypatch):
    from app.userbot.manager import userbot_manager
    c = FakeClient()
    monkeypatch.setattr(userbot_manager, 'client', c)
    monkeypatch.setattr(userbot_manager, 'loop', object())
    calls = []
    def run(coro, timeout):
        calls.append(timeout)
        return asyncio.run(coro)
    monkeypatch.setattr(userbot_manager, 'run', run)
    assert roster.fetch_live_member_ids('-100123') == [1, 4]
    assert calls == [roster.ROSTER_TIMEOUT + 2]


@pytest.mark.asyncio
async def test_basic_group_full_roster():
    c = FakeClient(users=[types.User(id=1)], expected=1)
    c.entity = types.Chat(id=123, title='basic', photo=types.ChatPhotoEmpty(), participants_count=1, date=None, version=1)
    c.get_entity.return_value = c.entity
    c.full = NS(participants=types.ChatParticipants(chat_id=123, participants=[types.ChatParticipant(user_id=1, inviter_id=2, date=None)], version=1))
    assert await roster._bounded_collect(c, '-123') == [1]
