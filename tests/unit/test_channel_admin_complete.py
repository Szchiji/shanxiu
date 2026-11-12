"""Regression coverage for actual dispatcher wiring and the channel/group boundary."""
import asyncio
import itertools
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, MessageOriginChannel, Update, MessageId
from telegram.ext import Application, ApplicationBuilder

from app import db
from app.models import BotGroup, OtherSettings, ChannelPostRecord, SyncGroupMessages, SyncMessageLog
from app.modules.core import routes
from app.services.channel_admin import discussion_settings, record_post, post_count, source_message_id
from app.services import own_post_sync as ops

_ids = itertools.count(990001)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def pair(flask_app, monkeypatch):
    n = next(_ids)
    with flask_app.app_context():
        ch = BotGroup(chat_id=str(-1000000000-n), title='audit channel', type='channel', is_active=True)
        disc = BotGroup(chat_id=str(-2000000000-n), title='audit discussion', type='supergroup', is_active=True)
        db.session.add_all([ch, disc]); db.session.commit()
        ids = ch.id, disc.id
    monkeypatch.setattr(routes, 'global_flask_app', flask_app)
    ops.invalidate()
    yield ids
    ops.invalidate()


def message(chat_id, mid=1, **kw):
    ch = Chat(int(chat_id), 'channel', title='channel')
    values = dict(message_id=mid, date=datetime.now(timezone.utc), chat=ch, sender_chat=ch, text='hello')
    values.update(kw)
    return Message(**values)


@pytest.mark.parametrize('mode', ['main', 'clone'])
def test_real_dispatcher_reaches_channel_and_discussion_handlers(mode, flask_app, monkeypatch):
    app = Application.builder().token('123:LOCAL_TEST_ONLY').build()
    channel, discussion = AsyncMock(), AsyncMock()
    monkeypatch.setattr(routes, 'handle_sync_channel_posts', channel)
    monkeypatch.setattr(routes, 'handle_channel_pin', discussion)
    for attr in ('global_bot_loop', 'global_flask_app', 'global_ptb_app'):
        monkeypatch.setattr(routes, attr, getattr(routes, attr))
    if mode == 'clone':
        routes.setup_clone_handlers(app, flask_app, 991)
    else:
        class Registered(Exception):
            pass
        monkeypatch.setenv('TG_BOT_TOKEN', '123:LOCAL_TEST_ONLY')
        monkeypatch.setattr(ApplicationBuilder, 'build', lambda self: app)
        monkeypatch.setattr(Application, 'initialize', AsyncMock(side_effect=Registered))
        with pytest.raises(Registered):
            run(routes.run_bot(flask_app))
    app._initialized = True
    msg = message(-1001234567)
    update = Update(1, channel_post=msg)
    matches = [h for h in app.handlers[0] if h.check_update(update)]
    assert matches[0].callback is channel
    run(app.process_update(update))
    channel.assert_awaited_once(); discussion.assert_not_awaited()
    d = Message(2, datetime.now(timezone.utc), Chat(-2001234567, 'supergroup'),
                sender_chat=msg.chat, is_automatic_forward=True, text='hello')
    run(app.process_update(Update(2, message=d)))
    discussion.assert_awaited_once()
    assert channel.await_count == 1


def test_settings_fallback_and_explicit_off(pair, flask_app):
    with flask_app.app_context():
        ch, disc = [db.session.get(BotGroup, i) for i in pair]
        legacy = OtherSettings(group_id=disc.id, cancel_channel_pin=True, channel_auto_buttons=True,
                               auto_delete_promote_msg=True, channel_discussion_keyword_filter='old')
        primary = OtherSettings(group_id=ch.id)
        db.session.add_all([legacy, primary]); db.session.commit()
        got = discussion_settings(ch, disc)
        assert got.cancel_channel_pin and got.channel_auto_buttons and got.auto_delete_promote_msg
        assert got.channel_discussion_keyword_filter == 'old'
        primary.channel_auto_buttons = True; primary.channel_discussion_keyword_filter = 'new'
        db.session.commit()
        got = discussion_settings(ch, disc)
        assert got.channel_auto_buttons and got.channel_discussion_keyword_filter == 'new'
        assert got.auto_delete_promote_msg  # group-only control remains group-owned
        primary.channel_auto_buttons = False; primary.channel_discussion_keyword_filter = None
        ch.config = json.dumps({'channel_settings_configured': True}); db.session.commit()
        got = discussion_settings(ch, disc)
        assert not got.channel_auto_buttons and not got.cancel_channel_pin
        assert got.channel_discussion_keyword_filter is None
        assert legacy.channel_auto_buttons  # no wipe of old rows


@pytest.mark.parametrize('page', ['settings', 'users', 'spam_protection', 'entry_exit_settings', 'forced_channel_subscription', 'points_rules', 'group_lottery', 'plugins'])
def test_group_only_pages_reject_channel(pair, client, page):
    with client.session_transaction() as s: s['logged_in'] = True
    assert client.get(f'/core/group/{pair[0]}/{page}').status_code == 404


def test_channel_other_settings_split_and_save_preserves_hidden_fields(pair, flask_app, client):
    gid, did = pair
    with flask_app.app_context():
        db.session.add(OtherSettings(group_id=gid, auto_delete_join_msg=True)); db.session.commit()
    with client.session_transaction() as s: s['logged_in'] = True
    body = client.get(f'/core/group/{gid}/other_settings').get_data(as_text=True)
    assert '频道设置' in body
    assert 'id="auto_delete_join_msg"' not in body and '自动删除进群消息' not in body
    assert 'id="channel_auto_buttons"' in body and 'id="cancel_channel_pin"' in body
    assert client.post('/core/api/save_other_settings', json={'group_id': gid, 'cancel_channel_pin': True}).json['status'] == 'ok'
    with flask_app.app_context():
        g = db.session.get(BotGroup, gid)
        assert json.loads(g.config)['channel_settings_configured']
        settings = OtherSettings.query.filter_by(group_id=gid).one()
        assert settings.auto_delete_join_msg and settings.cancel_channel_pin
    group = client.get(f'/core/group/{did}/other_settings').get_data(as_text=True)
    assert 'id="auto_delete_join_msg"' in group


@pytest.mark.parametrize('entry', ['core', 'incoming', 'buttons', 'reply', 'discussion', 'ids'])
def test_disabled_channel_blocks_all_entry_points(entry, pair, flask_app):
    with flask_app.app_context():
        ch, disc = [db.session.get(BotGroup, i) for i in pair]
        ch.is_active = False
        db.session.add(SyncGroupMessages(source_group_id=ch.id, target_group_id='-100999', enabled=True))
        db.session.add(OtherSettings(group_id=ch.id, channel_auto_buttons=True, cancel_channel_pin=True))
        db.session.commit()
        m = message(ch.chat_id)
        bot = AsyncMock()
        ctx = NS(bot=bot, application=NS(bot_data={'clone_id': None}))
        u = Update(1, channel_post=m)
        if entry == 'core': run(routes._sync_channel_post_core(bot, m, m.chat, None, None))
        if entry == 'incoming': run(routes.handle_sync_channel_posts(u, ctx))
        if entry == 'buttons': run(routes._maybe_attach_channel_post_buttons(bot, m, m.chat, ch, None))
        if entry == 'reply': run(routes._maybe_send_channel_auto_reply(bot, m, m.chat, ch, ctx))
        if entry == 'ids': run(ops.sync_ids_by_copy(bot, None, ch.chat_id, [1]))
        if entry == 'discussion':
            d = Message(2, m.date, Chat(int(disc.chat_id), 'supergroup'), sender_chat=m.chat, is_automatic_forward=True, text='hi')
            run(routes.handle_channel_pin(Update(2, message=d), ctx))
        assert bot.mock_calls == []
        assert ChannelPostRecord.query.filter_by(group_id=ch.id).count() == 0
        assert ch.chat_id not in ops.own_sync_sources(None, flask_app)


def test_post_count_albums_duplicates_edits_and_source_id(pair, flask_app):
    with flask_app.app_context():
        ch = db.session.get(BotGroup, pair[0])
        a = message(ch.chat_id, 42, media_group_id='album1')
        b = message(ch.chat_id, 43, media_group_id='album1')
        record_post(ch, a); record_post(ch, b); record_post(ch, a)
        record_post(ch, message(ch.chat_id, 44))
        assert post_count(ch.id) == 2
        assert ChannelPostRecord.query.filter_by(group_id=ch.id).count() == 3
        assert post_count(ch.id, datetime.now() + timedelta(days=1)) == 0
        origin = MessageOriginChannel(a.date, a.chat, 42)
        d = Message(777, a.date, Chat(-200222, 'supergroup'), sender_chat=a.chat, forward_origin=origin, text='hi')
        assert source_message_id(d) == 42


def test_channel_stats_pages_use_posts_not_members(pair, flask_app, client):
    with flask_app.app_context():
        ch = db.session.get(BotGroup, pair[0]); record_post(ch, message(ch.chat_id, 51))
    with client.session_transaction() as s: s['logged_in'] = True
    for page in ['dashboard', 'message_statistics', 'channel_stats', 'scheduled_messages', 'auto_replies', 'sync_message_logs']:
        response = client.get(f'/core/group/{pair[0]}/{page}')
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        if page == 'message_statistics':
            assert '频道帖子统计' in body and '累计帖子：<strong>1</strong>' in body
            assert '活跃用户' not in body
        if page == 'scheduled_messages':
            assert '发送到当前频道' in body and '发送到当前群组' not in body
        if page == 'dashboard':
            assert '累计帖子' in body and '关键词过滤</p>' not in body


@pytest.mark.parametrize('restrict,meta,expected', [
    ({'sync_media': False}, {'media_type':'photo'}, False),
    ({'sync_media': False}, {'media_type':'text'}, True),
    ({'sync_forwards': False}, {'media_type':'text','is_forward':True}, False),
    ({'filter_keywords':'["广告"]'}, {'media_type':'text','text':'有广告'}, False),
    ({'filter_keywords':'["广告"]'}, {'media_type':'text','text':'干净'}, True),
    ({'filter_keywords':'["广告"]'}, {}, False),
    ({'sync_media':False}, {}, False),
    ({}, {}, True),
])
def test_id_only_options_fail_closed(restrict, meta, expected):
    setting = NS(sync_media=True, sync_forwards=True, filter_keywords='[]')
    setting.__dict__.update(restrict)
    selected, reasons = ops.filter_id_posts(setting, [1], {1:meta} if meta else {})
    assert bool(selected) is expected
    assert bool(reasons) is not expected


def test_id_album_keyword_filters_all_parts():
    s = NS(sync_media=True, sync_forwards=True, filter_keywords='["广告"]')
    meta = {1:dict(post_key='album:1',media_type='photo',text='广告'),
            2:dict(post_key='album:1',media_type='photo',text='')}
    assert ops.filter_id_posts(s, [1,2], meta)[0] == []


def test_id_only_runtime_filter_writes_log_and_does_not_send(pair, flask_app):
    with flask_app.app_context():
        ch = db.session.get(BotGroup, pair[0])
        record_post(ch, message(ch.chat_id, 60, text='广告'))
        db.session.add(SyncGroupMessages(source_group_id=ch.id, target_group_id='-100998', enabled=True,
                                        sync_own_bot_posts=True, filter_keywords='["广告"]'))
        db.session.commit()
        bot = AsyncMock()
        run(ops.sync_ids_by_copy(bot, None, ch.chat_id, [60]))
        bot.copy_message.assert_not_awaited(); bot.copy_messages.assert_not_awaited()
        assert SyncMessageLog.query.filter_by(source_group_id=ch.id, status='filtered').count() == 1


def test_own_send_counted_without_sync_config_and_copied_metadata(pair, flask_app):
    with flask_app.app_context():
        ch = db.session.get(BotGroup, pair[0]); cid = ch.chat_id
        run(ops.observe_result(None, cid, 'send_message', {}, message(cid, 80, text='known')))
        assert post_count(ch.id) == 1
        run(ops.observe_result(None, cid, 'copy_message', {'from_chat_id':cid,'message_id':80}, MessageId(81)))
        row = ChannelPostRecord.query.filter_by(group_id=ch.id, message_id=81).one()
        assert row.media_type == 'text' and row.message_text == 'known'
        assert post_count(ch.id) == 2


def test_discussion_uses_channel_delete_switch_and_legacy_fallback(pair, flask_app):
    with flask_app.app_context():
        ch, disc = [db.session.get(BotGroup, i) for i in pair]
        primary = OtherSettings(group_id=ch.id, auto_delete_channel_discussion_msg=True)
        legacy = OtherSettings(group_id=disc.id, auto_delete_channel_discussion_msg=False)
        db.session.add_all([primary, legacy]); db.session.commit()
        m = NS(sender_chat=Chat(int(ch.chat_id),'channel'), is_automatic_forward=True, message_id=555,
               delete=AsyncMock())
        u = NS(message=m, effective_chat=Chat(int(disc.chat_id),'supergroup'))
        ctx = NS(bot=AsyncMock(), application=NS(bot_data={'clone_id':None}))
        run(routes.handle_channel_pin(u, ctx)); m.delete.assert_awaited_once()
        primary.auto_delete_channel_discussion_msg=False
        legacy.auto_delete_channel_discussion_msg=True
        db.session.commit(); m.delete.reset_mock()
        run(routes.handle_channel_pin(u, ctx)); m.delete.assert_awaited_once()


def test_discussion_writes_actual_origin_id(pair, flask_app):
    from app.models import ChannelMessageStats
    with flask_app.app_context():
        ch, disc = [db.session.get(BotGroup, i) for i in pair]
        original = message(ch.chat_id, 42)
        d = Message(777, original.date, Chat(int(disc.chat_id),'supergroup'), sender_chat=original.chat,
                    forward_origin=MessageOriginChannel(original.date, original.chat, 42),
                    is_automatic_forward=True, text='test')
        ctx = NS(bot=AsyncMock(), application=NS(bot_data={'clone_id':None}))
        run(routes.handle_channel_pin(Update(1,message=d),ctx))
        row = ChannelMessageStats.query.filter_by(group_id=ch.id).one()
        assert row.channel_message_id == 42 and row.discussion_message_id == 777


def test_incoming_posts_count_without_sync_and_edited_updates_do_not_count(pair, flask_app):
    with flask_app.app_context():
        ch = db.session.get(BotGroup, pair[0]); cid = ch.chat_id
        bot = AsyncMock();ctx = NS(bot=bot,application=NS(bot_data={'clone_id':None}))
        a = message(cid, 90, media_group_id='same')
        b = message(cid, 91, media_group_id='same')
        run(routes.handle_sync_channel_posts(Update(1,channel_post=a),ctx))
        run(routes.handle_sync_channel_posts(Update(2,channel_post=b),ctx))
        run(routes.handle_sync_channel_posts(Update(3,channel_post=a),ctx))
        run(routes.handle_sync_channel_posts(Update(4,edited_channel_post=message(cid,92)),ctx))
        assert post_count(ch.id) == 1
        assert ChannelPostRecord.query.filter_by(group_id=ch.id).count() == 2


def test_invalid_id_filter_is_not_ignored():
    setting = NS(sync_media=True,sync_forwards=True,filter_keywords='broken')
    assert ops.filter_id_posts(setting,[1],{1:dict(media_type='text',text='anything')})[0] == []


@pytest.mark.parametrize('kind', ['buttons', 'forward'])
def test_buffered_channel_automation_rechecks_disabled_source(kind, pair, flask_app):
    with flask_app.app_context():
        ch, disc = [db.session.get(BotGroup, i) for i in pair]
        ch.is_active=False; db.session.commit()
        bot=AsyncMock();m=message(ch.chat_id,100,media_group_id='pending')
        key=(None, int(ch.chat_id) if kind=='buttons' else int(disc.chat_id), 'pending')
        if kind=='buttons':
            routes._CHANNEL_POST_BUTTON_ALBUM_BUFFERS[key]=dict(messages=[m],bot=bot,chat_id=int(ch.chat_id),links=[dict(text='x',url='https://example.com')])
            run(routes._flush_channel_post_button_album(key))
        else:
            routes._FORWARD_MEDIA_GROUP_BUFFERS[key]=dict(messages=[m],bot=bot,from_chat_id=int(disc.chat_id),
                source_channel_chat_id=int(ch.chat_id),rules=[dict(target_chat_id='-100999',source_keywords='')])
            run(routes._flush_forward_media_group(key))
        assert bot.mock_calls==[]


def test_album_crossing_midnight_counts_on_first_day_only(pair, flask_app):
    with flask_app.app_context():
        ch=db.session.get(BotGroup,pair[0])
        from app.utils import get_beijing_today
        today=get_beijing_today()
        a=NS(message_id=201,media_group_id='midnight',date=today-timedelta(seconds=1),text='x')
        b=NS(message_id=202,media_group_id='midnight',date=today+timedelta(seconds=1),text='x')
        record_post(ch,a);record_post(ch,b)
        assert post_count(ch.id)==1
        assert post_count(ch.id,today)==0
