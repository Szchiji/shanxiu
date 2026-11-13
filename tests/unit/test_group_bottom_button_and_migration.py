"""群底部按钮输入提示保留，以及群升级后自动更新 chat_id / 重试推送。"""

import asyncio
import json
import re
from concurrent.futures import Future
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from telegram import Chat, Message, Update
from telegram.error import ChatMigrated, Forbidden
from telegram.ext import Application, filters

OLD_CHAT = '-5380043446'
NEW_CHAT = '-1003990285281'


def _unique_chat(prefix='-1008'):
    return prefix + f'{uuid4().int % 10**12:012d}'


def _login(client):
    with client.session_transaction() as session:
        session['logged_in'] = True
        session.pop('clone_id', None)


def _run_threadsafe(coro, loop):
    future = Future()
    try:
        future.set_result(asyncio.run(coro))
    except Exception as exc:
        future.set_exception(exc)
    return future


def _extract_buttons_data(html):
    match = re.search(r'let buttonsData = JSON.parse\((.*?)\);', html, re.S)
    assert match, 'buttonsData initializer missing from page'
    return json.loads(json.loads(match.group(1)))


@pytest.fixture()
def group_and_button(flask_app):
    from app import db
    from app.models import BotGroup, GroupBottomButton
    with flask_app.app_context():
        group = BotGroup(
            chat_id=_unique_chat('-5'),
            title='底部按钮测试群',
            type='group',
            is_active=True,
        )
        db.session.add(group)
        db.session.flush()
        button = GroupBottomButton(
            group_id=group.id,
            button_text='帮助',
            button_url='https://example.com/legacy',
            button_callback='legacy_cb',
            trigger_keyword='help,援助',
            input_field_placeholder='请输入您的问题...',
            button_order=0,
            row_position=0,
            is_active=True,
        )
        db.session.add(button)
        db.session.commit()
        ids = (group.id, button.id, group.chat_id)
    yield ids


class TestButtonsJsonAndSave:
    def test_buttons_json_includes_placeholder_and_survives_render(self, flask_app, client, group_and_button):
        gid, _bid, _chat = group_and_button
        _login(client)
        response = client.get(f'/core/group/{gid}/group_bottom_button')
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        data = _extract_buttons_data(html)
        assert len(data) == 1
        assert data[0]['input_field_placeholder'] == '请输入您的问题...'
        assert data[0]['button_text'] == '帮助'
        # 模拟页面刷新后再按 UI 提交（不含 url/callback/keyword）
        payload = {
            'id': data[0]['id'],
            'group_id': gid,
            'button_text': data[0]['button_text'],
            'input_field_placeholder': data[0]['input_field_placeholder'],
            'button_order': data[0]['button_order'],
            'row_position': data[0]['row_position'],
            'is_active': data[0]['is_active'],
        }
        saved = client.post('/core/api/save_group_bottom_button', json=payload)
        assert saved.get_json()['status'] == 'ok'
        rerender = client.get(f'/core/group/{gid}/group_bottom_button')
        again = _extract_buttons_data(rerender.get_data(as_text=True))
        assert again[0]['input_field_placeholder'] == '请输入您的问题...'

    def test_save_omits_legacy_fields_then_updates_when_present(self, flask_app, client, group_and_button):
        from app.models import GroupBottomButton
        gid, bid, _chat = group_and_button
        _login(client)
        omitted = client.post('/core/api/save_group_bottom_button', json={
            'id': bid,
            'group_id': gid,
            'button_text': '帮助改名',
            'input_field_placeholder': '新的占位提示',
            'button_order': 0,
            'row_position': 0,
            'is_active': True,
        })
        assert omitted.get_json()['status'] == 'ok'
        with flask_app.app_context():
            button = GroupBottomButton.query.get(bid)
            assert button.button_text == '帮助改名'
            assert button.input_field_placeholder == '新的占位提示'
            assert button.button_url == 'https://example.com/legacy'
            assert button.button_callback == 'legacy_cb'
            assert button.trigger_keyword == 'help,援助'

        present = client.post('/core/api/save_group_bottom_button', json={
            'id': bid,
            'group_id': gid,
            'button_text': '帮助改名',
            'input_field_placeholder': '新的占位提示',
            'button_order': 0,
            'row_position': 0,
            'is_active': True,
            'button_url': 'https://example.com/new',
            'button_callback': 'new_cb',
            'trigger_keyword': 'newkw',
        })
        assert present.get_json()['status'] == 'ok'
        with flask_app.app_context():
            button = GroupBottomButton.query.get(bid)
            assert button.button_url == 'https://example.com/new'
            assert button.button_callback == 'new_cb'
            assert button.trigger_keyword == 'newkw'


class TestApplyChatMigration:
    def test_updates_group_and_stored_copies_and_sets_supergroup(self, flask_app):
        from app import db
        from app.models import (
            BotGroup, ChannelForwardRule, ScheduledMessage, SyncGroupMessages,
            SyncMessageLog, UserbotSyncRule,
        )
        from app.services.chat_migration import apply_chat_migration

        old_id, new_id = _unique_chat('-5'), _unique_chat('-100')
        other_target = _unique_chat('-1009')
        with flask_app.app_context():
            group = BotGroup(chat_id=old_id, title='待升级群', type='group', is_active=True)
            db.session.add(group)
            db.session.flush()
            scheduled = ScheduledMessage(
                group_id=group.id, content='hi', target_type='channel',
                target_channel_id=old_id, is_active=True,
            )
            sync = SyncGroupMessages(
                source_group_id=group.id, target_group_id=old_id, enabled=True,
            )
            log = SyncMessageLog(source_group_id=group.id, target_group_id=old_id, status='success')
            rule = ChannelForwardRule(
                group_id=group.id, rule_name='fwd', target_chat_id=old_id, is_active=True,
            )
            ub = UserbotSyncRule(
                source_chat_id=old_id,
                target_chat_ids=json.dumps([old_id, other_target]),
                enabled=True,
            )
            db.session.add_all([scheduled, sync, log, rule, ub])
            db.session.commit()
            ids = dict(gid=group.id, sid=scheduled.id, sync_id=sync.id, log_id=log.id,
                       rid=rule.id, ubid=ub.id)

            result = apply_chat_migration(old_id, new_id)
            assert result['ok'] is True
            assert ids['gid'] in result['updated_group_ids']
            group = db.session.get(BotGroup, ids['gid'])
            assert group.chat_id == new_id
            assert group.type == 'supergroup'
            assert db.session.get(ScheduledMessage, ids['sid']).target_channel_id == new_id
            assert db.session.get(SyncGroupMessages, ids['sync_id']).target_group_id == new_id
            assert db.session.get(SyncMessageLog, ids['log_id']).target_group_id == new_id
            assert db.session.get(ChannelForwardRule, ids['rid']).target_chat_id == new_id
            ub = db.session.get(UserbotSyncRule, ids['ubid'])
            assert ub.source_chat_id == new_id
            assert json.loads(ub.target_chat_ids) == [new_id, other_target]

    def test_collision_does_not_overwrite_existing_group(self, flask_app):
        from app import db
        from app.models import BotGroup, SyncGroupMessages
        from app.services.chat_migration import apply_chat_migration

        old_id, new_id = _unique_chat('-5'), _unique_chat('-100')
        with flask_app.app_context():
            old_group = BotGroup(chat_id=old_id, title='旧群', type='group', is_active=True)
            new_group = BotGroup(chat_id=new_id, title='已占用新ID', type='supergroup', is_active=True)
            db.session.add_all([old_group, new_group])
            db.session.flush()
            sync = SyncGroupMessages(
                source_group_id=old_group.id, target_group_id=old_id, enabled=True,
            )
            db.session.add(sync)
            db.session.commit()
            old_gid, new_gid, sync_id = old_group.id, new_group.id, sync.id

            result = apply_chat_migration(old_id, new_id)
            assert result['ok'] is False
            assert result['reason'] == 'collision'
            assert result['existing_group_id'] == new_gid
            assert db.session.get(BotGroup, old_gid).chat_id == old_id
            assert db.session.get(BotGroup, old_gid).type == 'group'
            assert db.session.get(BotGroup, new_gid).chat_id == new_id
            # 其它指向旧 ID 的副本仍应更新
            assert db.session.get(SyncGroupMessages, sync_id).target_group_id == new_id

    def test_sync_target_unique_conflict_disables_stale_row(self, flask_app):
        from app import db
        from app.models import BotGroup, SyncGroupMessages
        from app.services.chat_migration import apply_chat_migration

        old_id, new_id = _unique_chat('-5'), _unique_chat('-100')
        with flask_app.app_context():
            source = BotGroup(chat_id=_unique_chat('-1001'), title='源', type='supergroup', is_active=True)
            db.session.add(source)
            db.session.flush()
            stale = SyncGroupMessages(source_group_id=source.id, target_group_id=old_id, enabled=True)
            current = SyncGroupMessages(source_group_id=source.id, target_group_id=new_id, enabled=True)
            db.session.add_all([stale, current])
            db.session.commit()
            stale_id, current_id = stale.id, current.id
            result = apply_chat_migration(old_id, new_id)
            assert result['ok'] is True
            stale = db.session.get(SyncGroupMessages, stale_id)
            current = db.session.get(SyncGroupMessages, current_id)
            assert stale.target_group_id == old_id
            assert stale.enabled is False
            assert current.target_group_id == new_id
            assert current.enabled is True


def _patch_push_runtime(monkeypatch, routes, flask_app, send_impl):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_impl))
    ptb_app = SimpleNamespace(bot=bot)
    monkeypatch.setattr(routes, 'global_flask_app', flask_app)
    monkeypatch.setattr(routes, 'global_bot_loop', object())
    monkeypatch.setattr(routes, 'global_ptb_app', ptb_app)
    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe', _run_threadsafe)
    return bot


class TestPushMigrationRetry:
    def test_chat_migrated_updates_id_retries_and_succeeds(self, flask_app, client, monkeypatch):
        from app import db
        from app.models import BotGroup, GroupBottomButton
        from app.modules.core import routes

        with flask_app.app_context():
            group = BotGroup(chat_id=_unique_chat('-5'), title='升级群', type='group', is_active=True)
            db.session.add(group)
            db.session.flush()
            db.session.add(GroupBottomButton(
                group_id=group.id, button_text='菜单', is_active=True,
                button_order=0, row_position=0,
            ))
            db.session.commit()
            gid, old_id = group.id, group.chat_id
        new_id = _unique_chat('-100')

        async def send_impl(**kwargs):
            cid = str(kwargs['chat_id'])
            if cid == old_id:
                raise ChatMigrated(int(new_id))
            assert cid == new_id
            return SimpleNamespace(message_id=88)

        bot = _patch_push_runtime(monkeypatch, routes, flask_app, send_impl)
        _login(client)
        response = client.post('/core/api/push_group_bottom_buttons', json={'group_id': gid})
        body = response.get_json()
        assert body['status'] == 'ok'
        assert new_id in body['msg']
        assert '升级为超级群' in body['msg']
        assert bot.send_message.await_count == 2
        with flask_app.app_context():
            group = db.session.get(BotGroup, gid)
            assert group.chat_id == new_id
            assert group.type == 'supergroup'

    def test_retry_failure_returns_new_id_in_error(self, flask_app, client, monkeypatch):
        from app import db
        from app.models import BotGroup, GroupBottomButton
        from app.modules.core import routes

        with flask_app.app_context():
            group = BotGroup(chat_id=_unique_chat('-5'), title='升级失败群', type='group', is_active=True)
            db.session.add(group)
            db.session.flush()
            db.session.add(GroupBottomButton(
                group_id=group.id, button_text='菜单', is_active=True,
                button_order=0, row_position=0,
            ))
            db.session.commit()
            gid, old_id = group.id, group.chat_id
        new_id = _unique_chat('-100')

        async def send_impl(**kwargs):
            cid = str(kwargs['chat_id'])
            if cid == old_id:
                raise ChatMigrated(int(new_id))
            raise Forbidden('bot is not a member')

        _patch_push_runtime(monkeypatch, routes, flask_app, send_impl)
        _login(client)
        body = client.post('/core/api/push_group_bottom_buttons', json={'group_id': gid}).get_json()
        assert body['status'] == 'error'
        assert new_id in body['msg']
        assert '升级为超级群' in body['msg']
        with flask_app.app_context():
            assert db.session.get(BotGroup, gid).chat_id == new_id

    def test_push_requires_group_access(self, flask_app, client, monkeypatch):
        from app import db
        from app.models import BotGroup, GroupBottomButton
        from app.modules.core import routes

        with flask_app.app_context():
            group = BotGroup(
                chat_id=_unique_chat('-5'), title='克隆群', type='group',
                is_active=True, clone_id=77,
            )
            db.session.add(group)
            db.session.flush()
            db.session.add(GroupBottomButton(
                group_id=group.id, button_text='菜单', is_active=True,
                button_order=0, row_position=0,
            ))
            db.session.commit()
            gid = group.id

        bot = _patch_push_runtime(monkeypatch, routes, flask_app, AsyncMock())
        with client.session_transaction() as session:
            session['logged_in'] = True
            session['clone_id'] = 99
        body = client.post('/core/api/push_group_bottom_buttons', json={'group_id': gid}).get_json()
        assert body['status'] == 'error'
        assert '无权' in body['msg']
        bot.send_message.assert_not_called()


class TestMigrateHandler:
    def test_handler_records_new_id_from_service_message(self, flask_app):
        from app import db
        from app.models import BotGroup
        from app.services.chat_migration import handle_chat_migrated

        old_id, new_id = _unique_chat('-5'), _unique_chat('-100')
        with flask_app.app_context():
            group = BotGroup(chat_id=old_id, title='服务消息升级', type='group', is_active=True)
            db.session.add(group)
            db.session.commit()
            gid = group.id

        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(int(old_id), 'group', title='服务消息升级'),
            migrate_to_chat_id=int(new_id),
        )
        update = Update(1, message=message)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={
            'flask_app': flask_app, 'clone_id': None,
        }))
        asyncio.run(handle_chat_migrated(update, context))
        with flask_app.app_context():
            group = db.session.get(BotGroup, gid)
            assert group.chat_id == new_id
            assert group.type == 'supergroup'

    def test_handler_registered_on_main_and_clone(self, flask_app, monkeypatch):
        from app.modules.core import routes
        from app.services.chat_migration import handle_chat_migrated

        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(int(OLD_CHAT), 'group', title='x'),
            migrate_to_chat_id=int(NEW_CHAT),
        )
        update = Update(1, message=message)

        def _callbacks(app):
            found = []
            for group_handlers in app.handlers.values():
                for handler in group_handlers:
                    if handler.check_update(update):
                        found.append(handler.callback)
            return found

        clone_app = Application.builder().token('123:LOCAL_TEST_ONLY').build()
        routes.setup_clone_handlers(clone_app, flask_app, 991)
        assert handle_chat_migrated in _callbacks(clone_app)
        assert filters.StatusUpdate.MIGRATE.check_update(update)

        main_app = Application.builder().token('123:LOCAL_TEST_ONLY').build()

        class Registered(Exception):
            pass

        from telegram.ext import ApplicationBuilder
        monkeypatch.setenv('TG_BOT_TOKEN', '123:LOCAL_TEST_ONLY')
        monkeypatch.setattr(ApplicationBuilder, 'build', lambda self: main_app)
        monkeypatch.setattr(Application, 'initialize', AsyncMock(side_effect=Registered))
        for attr in ('global_bot_loop', 'global_flask_app', 'global_ptb_app'):
            monkeypatch.setattr(routes, attr, getattr(routes, attr))
        with pytest.raises(Registered):
            asyncio.run(routes.run_bot(flask_app))
        assert handle_chat_migrated in _callbacks(main_app)
