"""Expected membership skips are remembered separately from successful mutes."""
import asyncio
import inspect
import json
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, Forbidden

CHAT = -1009991234567
USER = 999123456


@pytest.fixture
def env(flask_app, monkeypatch):
    from app import db
    from app.models import BotGroup, GroupUser
    from app.modules.core import routes
    monkeypatch.setattr(routes, 'global_flask_app', flask_app)
    with flask_app.app_context():
        group = BotGroup(chat_id=str(CHAT), title='expired mute test', type='supergroup',
                         is_active=True, config='{}')
        db.session.add(group)
        db.session.flush()
        row = GroupUser(group_id=group.id, tg_id=USER,
                        expiration_date=routes.get_beijing_now() - timedelta(days=1),
                        is_banned=False)
        db.session.add(row)
        db.session.commit()
        gid, rid = group.id, row.id
    bot = NS(get_chat_member=AsyncMock(return_value=NS(status='member')),
             restrict_chat_member=AsyncMock(return_value=True), send_message=AsyncMock())
    context = NS(bot=bot, args=[], application=NS(bot_data={}))
    e = NS(app=flask_app, db=db, routes=routes, GroupUser=GroupUser, BotGroup=BotGroup,
           rid=rid, gid=gid, bot=bot, context=context)
    yield e
    with flask_app.app_context():
        GroupUser.query.filter_by(group_id=gid).delete()
        db.session.delete(db.session.get(BotGroup, gid))
        db.session.commit()


def state(e):
    with e.app.app_context():
        row = e.db.session.get(e.GroupUser, e.rid)
        return row.expired_mute_skip, row.is_banned, row.telegram_tag_skip


def change(e, **values):
    with e.app.app_context():
        row = e.db.session.get(e.GroupUser, e.rid)
        for key, value in values.items():
            setattr(row, key, value)
        e.db.session.commit()


def switch(e, enabled):
    with e.app.app_context():
        e.db.session.get(e.BotGroup, e.gid).config = json.dumps({'auto_mute_expired': enabled})
        e.db.session.commit()


def mute(e):
    return asyncio.run(e.routes.mute_expired_group_user(e.bot, e.rid))


@pytest.mark.parametrize('status,reason,present', [
    ('administrator', 'admin', None), ('creator', 'admin', None),
    ('left', 'left', None), ('kicked', 'left', None), ('restricted', 'left', False),
])
def test_skip_remembered_without_banned(env, capsys, status, reason, present):
    env.bot.get_chat_member.return_value = NS(status=status, is_member=present)
    change(env, telegram_tag_skip='independent')
    assert mute(env) is False
    assert state(env) == (reason, False, 'independent')
    env.bot.restrict_chat_member.assert_not_awaited()
    assert 'Traceback' not in capsys.readouterr().out


@pytest.mark.parametrize('stage', ['get_chat_member', 'restrict_chat_member'])
@pytest.mark.parametrize('error,reason', [
    ('Participant_id_invalid', 'invalid'), ('User not found', 'invalid'),
    ('User_id_invalid', 'invalid'), ('User is an administrator of the chat', 'admin'),
])
def test_expected_api_races_skip_quietly(env, capsys, stage, error, reason):
    getattr(env.bot, stage).side_effect = BadRequest(error)
    assert mute(env) is False
    assert state(env)[:2] == (reason, False)
    assert 'Traceback' not in capsys.readouterr().out


@pytest.mark.parametrize('stage', ['get_chat_member', 'restrict_chat_member'])
@pytest.mark.parametrize('error', [NetworkError('timeout'), Forbidden('bot is not a member'),
                                  BadRequest('Not enough rights to restrict chat member')])
def test_unexpected_errors_remain_retryable(env, stage, error):
    getattr(env.bot, stage).side_effect = error
    with pytest.raises(type(error)):
        mute(env)
    assert state(env)[:2] == (None, False)


@pytest.mark.parametrize('status', ['member', 'restricted'])
def test_regular_member_marks_banned_only_after_success(env, status):
    env.bot.get_chat_member.return_value = NS(status=status, is_member=True)

    async def restrict(**kwargs):
        assert state(env)[:2] == (None, False)
        assert kwargs['chat_id'] == CHAT and kwargs['user_id'] == USER
        return True

    env.bot.restrict_chat_member.side_effect = restrict
    assert mute(env) is True
    assert state(env)[:2] == (None, True)


@pytest.mark.parametrize('skip', ['admin', 'left', 'invalid'])
def test_returned_regular_member_clears_skip_and_mutes(env, skip):
    change(env, expired_mute_skip=skip)
    assert mute(env) is True
    assert state(env)[:2] == (None, True)


def test_switch_off_does_not_mute(env):
    switch(env, False)
    assert mute(env) is False
    env.bot.restrict_chat_member.assert_not_awaited()
    assert state(env)[:2] == (None, False)


@pytest.mark.parametrize('eligible', ['switch_off', 'not_expired'])
def test_return_clears_skip_even_when_not_eligible(env, eligible):
    change(env, expired_mute_skip='left')
    if eligible == 'switch_off':
        switch(env, False)
    else:
        change(env, expiration_date=env.routes.get_beijing_now() + timedelta(days=1))
    assert mute(env) is False
    assert state(env)[:2] == (None, False)
    env.bot.restrict_chat_member.assert_not_awaited()


@pytest.mark.parametrize('old_status', ['administrator', 'creator', 'left', 'kicked'])
def test_demote_rejoin_event_rechecks(env, old_status):
    change(env, expired_mute_skip='admin' if old_status in ('administrator', 'creator') else 'left')
    member = NS(status='member', user=NS(id=USER, is_bot=False))
    update = NS(chat_member=NS(chat=NS(id=CHAT), old_chat_member=NS(status=old_status),
                              new_chat_member=member))
    asyncio.run(env.routes.on_expired_mute_chat_member(update, env.context))
    assert state(env)[:2] == (None, True)


def test_event_ignores_own_mute(env):
    update = NS(chat_member=NS(chat=NS(id=CHAT), old_chat_member=NS(status='member'),
                              new_chat_member=NS(status='restricted', is_member=True,
                                                 user=NS(id=USER, is_bot=False))))
    asyncio.run(env.routes.on_expired_mute_chat_member(update, env.context))
    env.bot.get_chat_member.assert_not_awaited()


def test_event_honors_switch(env):
    switch(env, False)
    change(env, expired_mute_skip='admin')
    update = NS(chat_member=NS(chat=NS(id=CHAT), old_chat_member=NS(status='administrator'),
                              new_chat_member=NS(status='member', user=NS(id=USER, is_bot=False))))
    asyncio.run(env.routes.on_expired_mute_chat_member(update, env.context))
    assert state(env)[:2] == (None, False)
    env.bot.restrict_chat_member.assert_not_awaited()


def test_hourly_excludes_remembered_skips_before_limit(env, monkeypatch):
    change(env, expired_mute_skip='admin')
    # Remembered admin is first; batch of one must still reach a regular member.
    with env.app.app_context():
        row = env.GroupUser(group_id=env.gid, tg_id=USER + 1,
                            expiration_date=env.routes.get_beijing_now() - timedelta(days=1),
                            is_banned=False)
        env.db.session.add(row)
        env.db.session.commit()
    monkeypatch.setattr(env.routes, 'EXPIRED_USERS_BATCH_SIZE', 1)
    asyncio.run(env.routes.check_expired_users(env.context))
    env.bot.restrict_chat_member.assert_awaited_once()
    assert env.bot.restrict_chat_member.await_args.kwargs['user_id'] == USER + 1
    assert state(env)[:2] == ('admin', False)


def test_hourly_first_skip_is_not_retried_on_next_run(env):
    env.bot.get_chat_member.return_value = NS(status='administrator')
    asyncio.run(env.routes.check_expired_users(env.context))
    count = env.bot.get_chat_member.await_count
    assert count == 1
    asyncio.run(env.routes.check_expired_users(env.context))
    assert env.bot.get_chat_member.await_count == count
    assert state(env)[:2] == ('admin', False)


@pytest.mark.parametrize('status', ['administrator', 'member'])
def test_message_path_rechecks_skip(env, status):
    change(env, expired_mute_skip='admin')
    env.bot.get_chat_member.return_value = NS(status=status)
    update = NS(effective_chat=NS(id=CHAT, type='supergroup'),
                effective_user=NS(id=USER, is_bot=False))
    asyncio.run(env.routes.check_and_mute_expired_user(update, env.context))
    assert state(env)[:2] == (('admin', False) if status == 'administrator' else (None, True))


@pytest.mark.parametrize('status', ['administrator', 'left', 'member'])
def test_private_start_rechecks_skip_and_only_notifies_actual_mute(env, status):
    change(env, expired_mute_skip='admin')
    env.bot.get_chat_member.return_value = NS(status=status)
    message = NS(reply_html=AsyncMock(), reply_text=AsyncMock())
    update = NS(effective_chat=NS(id=USER, type='private'), message=message,
                effective_user=NS(id=USER, username='tester', first_name='Test', last_name=''))
    asyncio.run(env.routes.cmd_start(update, env.context))
    if status == 'member':
        assert state(env)[:2] == (None, True)
    else:
        assert state(env)[1] is False
        assert not any('已被暂时禁言' in str(call) for call in message.reply_html.await_args_list)


def test_join_service_message_rechecks_without_entry_settings(env):
    change(env, expired_mute_skip='left')
    update = NS(effective_chat=NS(id=CHAT, type='supergroup', title='test'),
                message=NS(new_chat_members=[NS(id=USER, is_bot=False)]))
    asyncio.run(env.routes.handle_new_chat_member(update, env.context))
    assert state(env)[:2] == (None, True)


def test_checkin_and_both_handler_registrations_use_shared_gate(env):
    # on_message has extensive unrelated side effects; assert its check-in block
    # uses exactly the tested gate, never an unguarded restriction call.
    source = inspect.getsource(env.routes.on_message)
    block = source.split('# 2. 打卡', 1)[1].split('# 🆕 Send ephemeral message', 1)[0]
    assert 'mute_expired_group_user(context.bot, expired_user_id)' in block
    assert 'restrict_chat_member(' not in block
    for function in (env.routes.run_bot, env.routes.setup_clone_handlers):
        assert 'ChatMemberHandler(on_expired_mute_chat_member, ChatMemberHandler.CHAT_MEMBER)' in inspect.getsource(function)


def test_migration_has_dedicated_nullable_column():
    from pathlib import Path
    migration = Path('migrate_database.py').read_text()
    assert 'ALTER TABLE group_users ADD COLUMN IF NOT EXISTS expired_mute_skip VARCHAR(16)' in migration


@pytest.mark.parametrize('status,enabled', [
    ('administrator', True), ('left', True), ('member', True), ('member', False),
])
def test_checkin_runtime_uses_membership_gate(env, monkeypatch, status, enabled):
    # Isolate unrelated filters, but execute the real on_message check-in branch.
    for name in ('check_spam_protection', 'check_keyword_filter', 'check_forced_subscription'):
        monkeypatch.setattr(env.routes, name, AsyncMock(return_value=False))
    for name in ('check_and_mute_expired_user', 'update_user_activity', 'track_message_statistics',
                 'track_user_name_change', 'update_group_member', 'handle_sync_group_messages'):
        monkeypatch.setattr(env.routes, name, AsyncMock())
    monkeypatch.setattr(env.routes, 'is_plugin_enabled', lambda *args: True)
    with env.app.app_context():
        env.db.session.get(env.BotGroup, env.gid).config = json.dumps({
            'auto_mute_expired': enabled, 'checkin_open': True,
            'auto_like': False, 'auto_member_tag': False,
        })
        env.db.session.commit()
    change(env, expired_mute_skip='admin')
    env.bot.get_chat_member.return_value = NS(status=status)
    env.context.job_queue = NS(run_once=lambda *args, **kwargs: None)
    message = NS(text='打卡', reply_html=AsyncMock())
    update = NS(effective_chat=NS(id=CHAT, type='supergroup', title='test'),
                effective_user=NS(id=USER, is_bot=False), effective_message=message)
    asyncio.run(env.routes.on_message(update, env.context))
    if status == 'member' and enabled:
        assert state(env)[:2] == (None, True)
        env.bot.restrict_chat_member.assert_awaited_once()
    else:
        assert state(env)[1] is False
        env.bot.restrict_chat_member.assert_not_awaited()
        message.reply_html.assert_not_awaited()
    # Assert the branch was actually reached, rather than silently failing early.
    env.bot.get_chat_member.assert_awaited_once()


def test_unknown_status_is_not_skip_marked(env):
    env.bot.get_chat_member.return_value = NS(status='unknown')
    with pytest.raises(ValueError):
        mute(env)
    assert state(env)[:2] == (None, False)


def test_hourly_honors_switch(env):
    switch(env, False)
    asyncio.run(env.routes.check_expired_users(env.context))
    env.bot.get_chat_member.assert_not_awaited()
    env.bot.restrict_chat_member.assert_not_awaited()
    assert state(env)[:2] == (None, False)
