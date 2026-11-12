"""No-network regressions: real main/clone dispatchers, own sends, complete reply cleanup."""
import asyncio
import itertools
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Chat, Message, MessageId, MessageOriginChannel, Update, User, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import Forbidden
from telegram.ext import Application, ApplicationBuilder, ExtBot
from app import db
from app.models import (AutoReply, BotGroup, OtherSettings, ChannelPostButtonConfig,
                        ChannelFirstComment, ChannelDiscussionPost, UserbotSyncRule)
from app.modules.core import routes
from app.services import own_post_sync as ops, userbot_sync as us
from app.services.channel_admin import discussion_settings, maybe_first_comment, check_first_comment_link
from app.services.channel_discussion import record_forward, resume_pending

_numbers = itertools.count(880000)
NOW = datetime.now(timezone.utc)
KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton('已有', url='https://example.com/existing')]])


def run(coro):
    async def work():
        result = await coro
        # Own-send hooks and pending discussion buttons finish on this same local loop.
        for _ in range(4):
            await asyncio.sleep(0)
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not tasks:
                break
            await asyncio.gather(*tasks)
        return result
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(work())
    finally:
        loop.close()


@pytest.fixture
def pair(flask_app, monkeypatch):
    n = next(_numbers)
    with flask_app.app_context():
        channel = BotGroup(chat_id=str(-1008800000000-n), title='channel audit', type='channel', is_active=True)
        group = BotGroup(chat_id=str(-1009900000000-n), title='discussion audit', type='supergroup', is_active=True)
        db.session.add_all([channel, group]); db.session.commit()
        ids = channel.id, group.id
    monkeypatch.setattr(routes, 'global_flask_app', flask_app)
    monkeypatch.setattr(ops, 'SETTLE_DELAY', 0)
    yield ids
    ops.invalidate()


def models(ids):
    return [db.session.get(BotGroup, i) for i in ids]


def post(channel, mid=42, **kw):
    chat = Chat(int(channel.chat_id), 'channel', title='channel', username='audit_channel')
    args = dict(message_id=mid, date=NOW, chat=chat, sender_chat=chat, text='test')
    args.update(kw)
    return Message(**args)


def forwarded(channel, group, mid=77, source=42, **kw):
    ch = Chat(int(channel.chat_id), 'channel', title='channel', username='audit_channel')
    args = dict(message_id=mid, date=NOW, chat=Chat(int(group.chat_id), 'supergroup', username='audit_disc'),
                sender_chat=ch, is_automatic_forward=True, forward_origin=MessageOriginChannel(NOW, ch, source), text='test')
    args.update(kw)
    return Message(**args)


def bot():
    return NS(send_message=AsyncMock(return_value=NS(message_id=900)),
              send_media_group=AsyncMock(return_value=[NS(message_id=101), NS(message_id=102)]),
              copy_message=AsyncMock(return_value=MessageId(103)),
              edit_message_reply_markup=AsyncMock(return_value=True),
              delete_message=AsyncMock(return_value=True),
              get_chat=AsyncMock(return_value=NS(linked_chat_id=None)))


def context(b, clone=None):
    return NS(bot=b, job_queue=NS(run_once=Mock()), application=NS(bot_data={'clone_id': clone}))


def enable_buttons(channel, links=None):
    db.session.add(ChannelPostButtonConfig(group_id=channel.id, enabled=True,
        links=json.dumps(links or [{'text': '快捷', 'url': 'https://example.com', 'row': 0}])))
    db.session.commit()


def dispatcher(mode, flask_app, monkeypatch):
    app = Application.builder().token('123:LOCAL_TEST_ONLY').build()
    monkeypatch.setattr(routes, 'handle_new_chat_member', AsyncMock())
    monkeypatch.setattr(routes, 'handle_left_chat_member', AsyncMock())
    monkeypatch.setattr(routes, 'on_message', AsyncMock())
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
    return app


@pytest.mark.parametrize('mode', ['main', 'clone'])
@pytest.mark.parametrize('kind', ['join', 'leave', 'pin'])
def test_real_dispatcher_cleans_service_notices_without_swallowing_members(mode, kind, pair, flask_app, monkeypatch):
    app = dispatcher(mode, flask_app, monkeypatch)
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(ExtBot, 'delete_message', delete)
    with flask_app.app_context():
        channel, group = models(pair)
        channel.clone_id = group.clone_id = 991 if mode == 'clone' else None
        db.session.add(OtherSettings(group_id=group.id, auto_delete_join_msg=True,
                                    auto_delete_leave_msg=True, auto_delete_pin_msg=True)); db.session.commit()
        fields = {'join': {'new_chat_members': [User(7, 'new', False)]},
                  'leave': {'left_chat_member': User(7, 'left', False)},
                  'pin': {'pinned_message': forwarded(channel, group)}}[kind]
        msg = Message(200, NOW, Chat(int(group.chat_id), 'supergroup'), from_user=User(8, 'admin', False), **fields)
        msg.set_bot(app.bot)
        run(app.process_update(Update(201, message=msg)))
    delete.assert_awaited_once()
    if kind == 'join': routes.handle_new_chat_member.assert_awaited_once()
    if kind == 'leave': routes.handle_left_chat_member.assert_awaited_once()


@pytest.mark.parametrize('mode', ['main', 'clone'])
@pytest.mark.parametrize('state', ['ordinary', 'existing', 'inactive', 'sync'])
def test_real_dispatcher_own_channel_post_buttons(mode, state, pair, flask_app, monkeypatch):
    app = dispatcher(mode, flask_app, monkeypatch)
    edit, get = AsyncMock(return_value=True), AsyncMock(return_value=NS(linked_chat_id=None))
    monkeypatch.setattr(ExtBot, 'edit_message_reply_markup', edit)
    monkeypatch.setattr(ExtBot, 'get_chat', get)
    with flask_app.app_context():
        channel, group = models(pair)
        channel.clone_id = group.clone_id = 991 if mode == 'clone' else None
        channel.is_active = state != 'inactive'; db.session.commit(); enable_buttons(channel)
        msg = post(channel, from_user=User(123, 'our bot', True), reply_markup=KEYBOARD if state == 'existing' else None)
        if state == 'sync':
            from app.services.sync_loop_guard import mark_sync_output
            mark_sync_output(channel.chat_id, msg.message_id)
        run(app.process_update(Update(202, channel_post=msg)))
    assert edit.await_count == (1 if state == 'ordinary' else 0)


@pytest.mark.parametrize('clone', [None, 991])
@pytest.mark.parametrize('hit', [False, True])
@pytest.mark.parametrize('existing', [False, True])
def test_successful_own_send_hook_applies_buttons_without_sync_target(clone, hit, existing, pair, flask_app, monkeypatch):
    b = bot()
    with flask_app.app_context():
        channel, _ = models(pair); channel.clone_id = clone; db.session.commit(); enable_buttons(channel)
        msg = post(channel, reply_markup=KEYBOARD if existing else None)
        chat_id = channel.chat_id
    ops._bots[id(b)] = clone
    monkeypatch.setattr(ops, 'should_handle', lambda *a: (clone, chat_id) if hit else None)
    async def original(self, chat_id, **kw): return msg
    try:
        run(ops._wrap('send_message', original)(b, chat_id=chat_id, text='manual'))
    finally:
        ops._bots.pop(id(b), None)
    assert b.edit_message_reply_markup.await_count == (0 if existing else 1)


@pytest.mark.parametrize('has_buttons', [False, True])
def test_own_album_one_button_anchor_and_existing_whole_album_skip(has_buttons, pair, flask_app, monkeypatch):
    b = bot(); monkeypatch.setattr(routes, '_CHANNEL_POST_BUTTON_FLUSH_DELAY', 0)
    with flask_app.app_context():
        ch, _ = models(pair); enable_buttons(ch)
        msgs = [post(ch, 41, text=None, caption=None, media_group_id='album'),
                post(ch, 42, text=None, caption='caption', media_group_id='album', reply_markup=KEYBOARD if has_buttons else None)]
        run(ops._after_send(b, None, ch.chat_id, 'send_media_group', {}, msgs, False))
    assert b.edit_message_reply_markup.await_count == (0 if has_buttons else 1)
    if not has_buttons: assert b.edit_message_reply_markup.await_args.kwargs['message_id'] == 42


@pytest.mark.parametrize('path', ['channel', 'group'])
@pytest.mark.parametrize('media', ['album', 'album_buttons', 'tme', 'text'])
def test_auto_reply_deletes_entire_result_in_both_paths(path, media, pair, flask_app):
    b = bot(); ctx = context(b)
    b.send_message.return_value = NS(message_id=104)
    urls = ['https://example.com/1.jpg', 'https://example.com/2.jpg'] if media.startswith('album') else ['https://t.me/c/12345/9'] if media == 'tme' else []
    rule = AutoReply(trigger_keyword='test', media_type='image' if urls else 'text', media_urls=json.dumps(urls),
                     content='body', delete_after=15, links='[]', is_active=True)
    with flask_app.app_context():
        ch, group = models(pair)
        if path == 'channel':
            rule.group_id = ch.id; db.session.add(rule); db.session.commit()
            if media == 'album_buttons': rule.links = json.dumps([{'text': 'go', 'url': 'https://example.com'}]); db.session.commit()
            run(routes._maybe_send_channel_auto_reply(b, post(ch), post(ch).chat, ch, ctx))
            chat_id = int(ch.chat_id)
        else:
            chat_id = int(group.chat_id)
            run(routes._send_auto_reply_content(b, ctx, chat_id, rule,
                KEYBOARD if media == 'album_buttons' else None, reply_text=b.send_message))
    call = ctx.job_queue.run_once.call_args
    assert call is not None
    expected = [101, 102, 104] if media == 'album_buttons' else [101, 102] if media == 'album' else [103] if media == 'tme' else [104]
    assert call.kwargs['data']['message_ids'] == expected
    run(routes._auto_delete_msg(NS(bot=b, job=NS(data=call.kwargs['data']))))
    assert [a.kwargs['message_id'] for a in b.delete_message.await_args_list] == expected


def test_partial_reply_send_failure_still_schedules_already_sent_ids(pair, monkeypatch):
    b = bot(); ctx = context(b)
    async def partial(*a, collected_ids, **kw):
        collected_ids.extend([101, 102]); raise RuntimeError('later part failed')
    monkeypatch.setattr(routes, 'send_multi_media', partial)
    rule = AutoReply(media_type='image', media_urls='["https://example.com/1.jpg"]', content='', delete_after=15)
    with pytest.raises(RuntimeError): run(routes._send_auto_reply_content(b, ctx, -100123, rule, None))
    assert ctx.job_queue.run_once.call_args.kwargs['data']['message_ids'] == [101, 102]


def test_delete_failure_does_not_stop_remaining_parts():
    b = bot(); b.delete_message.side_effect = [Forbidden('not allowed'), True]
    run(routes._auto_delete_msg(NS(bot=b, job=NS(data={'chat_id': -100123, 'message_ids': [1, 2, 2]}))))
    assert b.delete_message.await_count == 2


@pytest.mark.parametrize('enabled', [None, False, True])
def test_channel_master_switch_actual_value_recovery_and_default(enabled, pair, flask_app, client):
    with flask_app.app_context():
        ch, _ = models(pair); ch.config = json.dumps({} if enabled is None else {'auto_reply_open': enabled}); db.session.commit(); gid = ch.id
    with client.session_transaction() as session: session['logged_in'] = True
    body = client.get(f'/core/group/{gid}/auto_replies').get_data(as_text=True)
    tag = re.search(r'<input[^>]*id="channel_auto_reply_open"[^>]*>', body).group(0)
    assert ('checked' in tag) == (enabled is not False)
    assert '包含匹配' not in body and '回复冷却' not in body and '/start 命令' not in body
    assert '成员发送' not in body
    response = client.post('/core/api/set_channel_auto_reply', json={'group_id': gid, 'enabled': True})
    assert response.json['status'] == 'ok'
    with flask_app.app_context(): assert routes.get_group_conf(db.session.get(BotGroup, gid))['auto_reply_open'] is True


def test_channel_settings_pin_notice_fallback_off_and_first_comment_validation(pair, flask_app, client):
    with client.session_transaction() as session: session['logged_in'] = True
    with flask_app.app_context():
        ch, group = models(pair)
        ch.config = json.dumps({'channel_settings_configured': True})
        db.session.add_all([OtherSettings(group_id=ch.id), OtherSettings(group_id=group.id, auto_delete_pin_msg=True)])
        db.session.commit(); gid = ch.id
        assert discussion_settings(ch, group).auto_delete_pin_msg
    response = client.post('/core/api/save_other_settings', json={'group_id': gid, 'first_comment_enabled': True, 'first_comment_text': '  '})
    assert response.status_code == 400
    assert client.post('/core/api/save_other_settings', json={'group_id': gid, 'first_comment_enabled': True,
        'first_comment_text': '欢迎讨论', 'auto_delete_pin_msg': False}).json['status'] == 'ok'
    with flask_app.app_context():
        ch, group = models(pair)
        assert not discussion_settings(ch, group).auto_delete_pin_msg
        assert OtherSettings.query.filter_by(group_id=group.id).one().auto_delete_pin_msg
        assert routes.get_group_conf(ch)['first_comment_text'] == '欢迎讨论'
    body = client.get(f'/core/group/{gid}/other_settings').get_data(as_text=True)
    assert 'id="auto_delete_pin_msg"' in body and 'id="first_comment_enabled"' in body
    for key in ('auto_delete_join_msg', 'auto_delete_leave_msg', 'auto_delete_promote_msg'):
        assert f'id="{key}"' not in body
    assert '欢迎讨论' in body


@pytest.mark.parametrize('state', ['default', 'disabled', 'inactive', 'inactive_discussion', 'no_discussion', 'not_auto', 'edited', 'blank', 'own_comment', 'sync'])
def test_first_comment_skips_invalid_or_off_cases(state, pair, flask_app):
    b = bot()
    with flask_app.app_context():
        ch, group = models(pair)
        ch.config = json.dumps({} if state == 'default' else {'first_comment_enabled': state != 'disabled', 'first_comment_text': '' if state == 'blank' else '自定义评论'})
        ch.is_active = state != 'inactive'; group.is_active = state != 'inactive_discussion'; db.session.commit()
        kw = {'is_automatic_forward': False} if state in ('not_auto', 'own_comment') else {'edit_date': NOW} if state == 'edited' else {}
        msg = forwarded(ch, group, **kw)
        if state == 'sync':
            from app.services.sync_loop_guard import mark_sync_output
            mark_sync_output(ch.chat_id, 42)
        run(maybe_first_comment(b, msg, ch, None if state == 'no_discussion' else group))
    b.send_message.assert_not_awaited()


@pytest.mark.parametrize('album', [False, True])
def test_first_comment_is_early_reply_and_deduplicated_across_repeats_and_album(album, pair, flask_app):
    b = bot(); ctx = context(b)
    with flask_app.app_context():
        ch, group = models(pair); ch.config = json.dumps({'first_comment_enabled': True, 'first_comment_text': '欢迎讨论'})
        # First comment must happen before existing linked-message deletion/filter exits.
        db.session.add(OtherSettings(group_id=ch.id, auto_delete_channel_discussion_msg=True)); db.session.commit()
        first = forwarded(ch, group, media_group_id='album' if album else None)
        first.set_bot(b)
        run(routes.handle_channel_pin(Update(1, message=first), ctx))
        run(routes.handle_channel_pin(Update(2, message=first), ctx))
        if album:
            second = forwarded(ch, group, mid=78, source=43, media_group_id='album'); second.set_bot(b)
            run(routes.handle_channel_pin(Update(3, message=second), ctx))
        run(routes.handle_channel_pin(Update(4, edited_message=first), ctx))
        assert ChannelFirstComment.query.filter_by(group_id=ch.id).count() == 1
    b.send_message.assert_awaited_once()
    kw = b.send_message.await_args.kwargs
    assert kw['reply_to_message_id'] == 77 and kw['text'] == '欢迎讨论'
    assert kw['allow_sending_without_reply'] is False


def test_first_comment_permission_error_does_not_break_discussion_processing(pair, flask_app, capsys):
    b = bot(); b.send_message.side_effect = Forbidden('blocked')
    with flask_app.app_context():
        ch, group = models(pair); ch.config = json.dumps({'first_comment_enabled': True, 'first_comment_text': 'hello'}); db.session.commit()
        run(routes.handle_channel_pin(Update(1, message=forwarded(ch, group)), context(b)))
        assert ChannelFirstComment.query.filter_by(group_id=ch.id).one().status == 'failed'
        assert routes.ChannelMessageStats.query.filter_by(group_id=ch.id).count() == 1
    assert 'Forbidden' in capsys.readouterr().out


def test_missing_link_probe_logs_without_sending():
    b = bot(); run(check_first_comment_link(b, -100123456, next(_numbers)))
    b.send_message.assert_not_awaited()


@pytest.mark.parametrize('order', ['source_first', 'discussion_first'])
def test_linked_discussion_comment_button_last_row_with_real_thread(order, pair, flask_app):
    b = bot()
    with flask_app.app_context():
        ch, group = models(pair); enable_buttons(ch)
        b.get_chat.return_value = NS(linked_chat_id=int(group.chat_id))
        msg, forward = post(ch), forwarded(ch, group)
        if order == 'source_first':
            run(routes._maybe_attach_channel_post_buttons(b, msg, msg.chat, ch, None))
            b.edit_message_reply_markup.assert_not_awaited()  # Keep native comments until root known.
            record_forward(ch, group, forward)
            run(resume_pending(b, ch.id, 42))
        else:
            record_forward(ch, group, forward)
            run(routes._maybe_attach_channel_post_buttons(b, msg, msg.chat, ch, None))
    rows = b.edit_message_reply_markup.await_args.kwargs['reply_markup'].inline_keyboard
    assert rows[0][0].text == '快捷'
    assert len(rows[-1]) == 1 and rows[-1][0].text == '评论'
    assert rows[-1][0].url == 'https://t.me/audit_disc/77?thread=77'


@pytest.mark.parametrize('existing', [False, True])
def test_comment_shortcut_no_link_or_existing_original_keyboard(existing, pair, flask_app):
    b = bot()
    with flask_app.app_context():
        ch, group = models(pair); enable_buttons(ch)
        msg = post(ch, reply_markup=KEYBOARD if existing else None)
        if existing: record_forward(ch, group, forwarded(ch, group))
        run(routes._maybe_attach_channel_post_buttons(b, msg, msg.chat, ch, None))
        if existing: run(resume_pending(b, ch.id, 42))
    if existing:
        b.edit_message_reply_markup.assert_not_awaited(); b.get_chat.assert_not_awaited()
    else:
        rows = b.edit_message_reply_markup.await_args.kwargs['reply_markup'].inline_keyboard
        assert len(rows) == 1 and rows[0][0].text == '快捷'


@pytest.mark.parametrize('url', ['https://t.me/audit_disc/77?thread=77', 'https://t.me/audit_channel/42?comment=900'])
def test_configured_same_post_comment_link_is_not_duplicated(url, pair, flask_app):
    b = bot()
    with flask_app.app_context():
        ch, group = models(pair); enable_buttons(ch, [{'text': '评论', 'url': url}])
        record_forward(ch, group, forwarded(ch, group))
        msg = post(ch); run(routes._maybe_attach_channel_post_buttons(b, msg, msg.chat, ch, None))
    rows = b.edit_message_reply_markup.await_args.kwargs['reply_markup'].inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1


def test_private_discussion_url_and_repeat_does_not_replace_applied_keyboard(pair, flask_app):
    b = bot()
    with flask_app.app_context():
        ch, group = models(pair); enable_buttons(ch)
        forward = forwarded(ch, group, chat=Chat(int(group.chat_id), 'supergroup'))
        record_forward(ch, group, forward)
        msg = post(ch)
        run(routes._maybe_attach_channel_post_buttons(b, msg, msg.chat, ch, None))
        run(routes._maybe_attach_channel_post_buttons(b, msg, msg.chat, ch, None))
        run(resume_pending(b, ch.id, 42))
        url = b.edit_message_reply_markup.await_args.kwargs['reply_markup'].inline_keyboard[-1][0].url
        assert url == f'https://t.me/c/{str(group.chat_id)[4:]}/77?thread=77'
    b.edit_message_reply_markup.assert_awaited_once()


def test_userbot_rule_loading_and_cached_queued_delivery_stop_for_paused_channel(pair, flask_app, monkeypatch):
    from app.userbot.manager import UserbotManager
    with flask_app.app_context():
        ch, _ = models(pair); ch.is_active = False; db.session.commit()
        db.session.add(UserbotSyncRule(source_chat_id=ch.chat_id, target_chat_ids='["-100333"]', enabled=True)); db.session.commit()
        cid = ch.chat_id
    manager = UserbotManager(); manager.flask_app = flask_app
    monkeypatch.setattr(manager, '_own_bot_ids', lambda: [])
    rules, _, _ = manager.get_rules()
    assert not any(str(r['source_chat_id']) == cid for r in rules)
    item = us.UbMessage(chat_id=int(cid), message_id=9, text='hello', html='hello', sender_id=7, sender_is_bot=True)
    b = bot(); plans = [{'target_chat_id': '-100333'}]
    send = AsyncMock(); monkeypatch.setattr(us, 'deliver_single', send); monkeypatch.setattr(us, 'deliver_album', send)
    run(us.process_item(b, flask_app, item, plans))
    run(us.deliver_to_plans(b, flask_app, item, plans))
    key = ('userbot', cid, 'album')
    us.USERBOT_MEDIA_GROUP_BUFFERS[key] = {'messages': [item], 'bot': b, 'flask_app': flask_app, 'plans': plans}
    run(us.flush_userbot_album(key))
    send.assert_not_awaited()


@pytest.mark.parametrize('clone', [None, 991])
@pytest.mark.parametrize('enabled', [False, True])
def test_pin_service_notice_resolves_source_channel_explicit_setting(clone, enabled, pair, flask_app):
    b = bot()
    with flask_app.app_context():
        ch, group = models(pair); ch.clone_id = group.clone_id = clone
        ch.config = json.dumps({'channel_settings_configured': True, 'channel_pin_notice_configured': True})
        db.session.add_all([OtherSettings(group_id=ch.id, auto_delete_pin_msg=enabled),
                            OtherSettings(group_id=group.id, auto_delete_pin_msg=not enabled)])
        db.session.commit()
        msg = Message(99, NOW, Chat(int(group.chat_id), 'supergroup'), pinned_message=forwarded(ch, group))
        msg.set_bot(b)
        run(routes.handle_auto_delete_messages(Update(1, message=msg), context(b, clone)))
    assert b.delete_message.await_count == int(enabled)


@pytest.mark.parametrize('mode', ['main', 'clone'])
def test_real_dispatcher_first_comment_and_post_shortcuts_cooperate(mode, pair, flask_app, monkeypatch):
    app = dispatcher(mode, flask_app, monkeypatch)
    send, edit = AsyncMock(return_value=NS(message_id=900)), AsyncMock(return_value=True)
    monkeypatch.setattr(ExtBot, 'send_message', send)
    monkeypatch.setattr(ExtBot, 'edit_message_reply_markup', edit)
    with flask_app.app_context():
        ch, group = models(pair); ch.clone_id = group.clone_id = 991 if mode == 'clone' else None
        ch.config = json.dumps({'first_comment_enabled': True, 'first_comment_text': '欢迎讨论'})
        db.session.commit(); enable_buttons(ch)
        monkeypatch.setattr(ExtBot, 'get_chat', AsyncMock(return_value=NS(linked_chat_id=int(group.chat_id))))
        async def flow():
            await app.process_update(Update(1, channel_post=post(ch)))
            await app.process_update(Update(2, message=forwarded(ch, group)))
        run(flow())
    send.assert_awaited_once()
    assert send.await_args.kwargs['reply_to_message_id'] == 77
    assert send.await_args.kwargs['text'] == '欢迎讨论'
    rows = edit.await_args.kwargs['reply_markup'].inline_keyboard
    assert [row[0].text for row in rows] == ['快捷', '评论']


@pytest.mark.parametrize('method', ['copy_message', 'copy_messages'])
def test_id_only_ordinary_copy_buttons_preserve_explicit_markup(method, pair, flask_app):
    b = bot()
    with flask_app.app_context():
        ch, _ = models(pair); enable_buttons(ch)
        run(ops._after_send(b, None, ch.chat_id, method, {}, MessageId(42), False))
        assert b.edit_message_reply_markup.await_count == 1
        run(ops._after_send(b, None, ch.chat_id, method, {'reply_markup': KEYBOARD}, MessageId(43), False))
        assert b.edit_message_reply_markup.await_count == 1


def test_channel_pause_and_sync_output_skip_own_observation_only_hook(pair, flask_app):
    b = bot()
    with flask_app.app_context():
        ch, _ = models(pair); enable_buttons(ch)
        from app.services.sync_loop_guard import mark_sync_output
        mark_sync_output(ch.chat_id, 42)
        run(ops._after_send(b, None, ch.chat_id, 'send_message', {}, post(ch), False))
        ch.is_active = False; db.session.commit()
        run(ops._after_send(b, None, ch.chat_id, 'send_message', {}, post(ch, 43), False))
    b.edit_message_reply_markup.assert_not_awaited()


def test_channel_control_api_enforces_login_channel_type_and_clone_scope(pair, flask_app, client):
    gid, disc = pair
    assert client.post('/core/api/set_channel_auto_reply', json={'group_id': gid, 'enabled': True}).status_code == 403
    with client.session_transaction() as session: session['logged_in'] = True
    assert client.post('/core/api/set_channel_auto_reply', json={'group_id': disc, 'enabled': True}).status_code == 404
    with client.session_transaction() as session: session['clone_id'] = 991
    assert client.post('/core/api/set_channel_auto_reply', json={'group_id': gid, 'enabled': True}).json['status'] == 'error'
    assert client.post('/core/api/save_other_settings', json={'group_id': gid, 'first_comment_enabled': True, 'first_comment_text': 'x'}).json['status'] == 'error'


def test_channel_deletion_cleans_new_operational_tables(pair, flask_app):
    with flask_app.app_context():
        ch, group = models(pair)
        db.session.add(ChannelFirstComment(group_id=ch.id, discussion_chat_id=group.chat_id, post_key='message:42'))
        record_forward(ch, group, forwarded(ch, group))
        routes._delete_group_cascade(ch.id); db.session.commit()
        assert ChannelFirstComment.query.filter_by(group_id=pair[0]).count() == 0
        assert ChannelDiscussionPost.query.filter_by(group_id=pair[0]).count() == 0


def test_album_shortcut_uses_shared_first_discussion_root(pair, flask_app, monkeypatch):
    b = bot(); monkeypatch.setattr(routes, '_CHANNEL_POST_BUTTON_FLUSH_DELAY', 0)
    with flask_app.app_context():
        ch, group = models(pair); enable_buttons(ch)
        b.get_chat.return_value = NS(linked_chat_id=int(group.chat_id))
        async def flow():
            for mid in (41, 42):
                msg = post(ch, mid, text=None, caption='caption' if mid == 42 else None, media_group_id='source-album')
                await routes._maybe_attach_channel_post_buttons(b, msg, msg.chat, ch, None)
            for mid, source in ((77, 41), (78, 42)):
                await routes.handle_channel_pin(Update(mid, message=forwarded(ch, group, mid=mid, source=source,
                                                    media_group_id='discussion-album')), context(b))
        run(flow())
    b.edit_message_reply_markup.assert_awaited_once()
    kw = b.edit_message_reply_markup.await_args.kwargs
    assert kw['message_id'] == 42
    assert kw['reply_markup'].inline_keyboard[-1][0].url == 'https://t.me/audit_disc/77?thread=77'
