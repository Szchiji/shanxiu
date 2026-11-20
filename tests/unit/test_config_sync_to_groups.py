"""One-time copy of auto-reply / scheduled-message config to other groups."""

import json
from datetime import datetime
from uuid import uuid4

import pytest

from app.models import AutoReply, BotGroup, ScheduledMessage
from app.services.config_sync_service import (
    MAX_SYNC_TARGETS,
    auto_reply_fingerprint,
    scheduled_message_fingerprint,
)


PAYLOAD = '<img src=x onerror="auditInjected()">'
BASE_NOW = datetime(2026, 11, 20, 12, 0, 0)


def _login(client, clone_id=None):
    with client.session_transaction() as session:
        session['logged_in'] = True
        if clone_id is None:
            session.pop('clone_id', None)
        else:
            session['clone_id'] = clone_id
    return client


def _group(db, *, title='群', type='supergroup', clone_id=None, is_active=True, config=None):
    conf = config or {'auto_reply_open': False, 'scheduled_msg_open': False}
    group = BotGroup(
        chat_id='-100' + uuid4().hex[:16],
        title=title,
        type=type,
        is_active=is_active,
        clone_id=clone_id,
        config=json.dumps(conf, ensure_ascii=False),
    )
    db.session.add(group)
    db.session.commit()
    return group


def _reply(db, group_id, **kw):
    fields = dict(
        group_id=group_id,
        trigger_keyword='你好',
        media_type='text',
        content='欢迎',
        links='[]',
        delete_after=0,
        is_active=True,
    )
    fields.update(kw)
    item = AutoReply(**fields)
    db.session.add(item)
    db.session.commit()
    return item


def _sched(db, group_id, **kw):
    fields = dict(
        group_id=group_id,
        media_type='text',
        content='公告',
        links='[]',
        repeat_interval=60,
        delete_previous=False,
        auto_pin=False,
        is_active=True,
        start_time=BASE_NOW,
    )
    fields.update(kw)
    item = ScheduledMessage(**fields)
    db.session.add(item)
    db.session.commit()
    return item


def _conf(group):
    return json.loads(group.config or '{}')


@pytest.fixture()
def admin(client):
    return _login(client)


@pytest.mark.usefixtures('db')
class TestTargetList:
    def test_lists_same_bot_groups_excludes_self_and_channels(self, db, admin):
        source = _group(db, title='源群')
        keep = _group(db, title='目标甲')
        _group(db, title='目标频道', type='channel')
        _group(db, title='克隆群', clone_id=13)
        _group(db, title='普通群', type='group')
        r = admin.get(f'/core/api/config_sync_targets/{source.id}?kind=auto_replies')
        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'
        assert data['source_count'] == 0
        ids = {t['id'] for t in data['targets']}
        titles = {t['title'] for t in data['targets']}
        assert keep.id in ids
        assert '普通群' in titles
        assert source.id not in ids
        assert '目标频道' not in titles
        assert '克隆群' not in titles

    def test_source_count_ignores_search_and_counts_all_rows(self, db, admin):
        source = _group(db, title='源群')
        _reply(db, source.id, trigger_keyword='苹果')
        _reply(db, source.id, trigger_keyword='香蕉')
        page = admin.get(f'/core/group/{source.id}/auto_replies?search=苹果')
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert '同步到其他群' in body
        assert 'openSyncToGroupsModal' in body
        # Filtered list count must not be baked into the sync modal copy.
        assert '同步本群全部自动回复（共 1 条）' not in body
        data = admin.get(f'/core/api/config_sync_targets/{source.id}?kind=auto_replies').get_json()
        assert data['source_count'] == 2

    def test_clone_admin_cannot_list_other_bot_groups(self, db, client):
        source = _group(db, title='克隆源', clone_id=5)
        _group(db, title='克隆目标', clone_id=5)
        _group(db, title='主号群', clone_id=None)
        _login(client, clone_id=5)
        data = client.get(f'/core/api/config_sync_targets/{source.id}?kind=scheduled_messages').get_json()
        titles = {t['title'] for t in data['targets']}
        assert '克隆目标' in titles
        assert '主号群' not in titles

    def test_unauthenticated_rejected(self, db, client):
        source = _group(db)
        r = client.get(f'/core/api/config_sync_targets/{source.id}?kind=auto_replies')
        assert r.status_code == 403


@pytest.mark.usefixtures('db')
class TestAutoReplySync:
    def test_copies_all_fields_disabled_and_dedups(self, db, admin):
        source = _group(db, title='源', config={'auto_reply_open': True})
        dest = _group(db, title='目标', config={'auto_reply_open': False})
        links = json.dumps([
            {'text': '官网', 'url': 'https://example.com', 'row': 0, 'order': 1},
            {'text': '客服', 'url': 'https://t.me/help', 'row': 1, 'order': 0},
        ], ensure_ascii=False)
        media = json.dumps([
            {'url': 'https://a/1.jpg', 'type': 'image'},
            {'url': 'https://a/2.mp4', 'type': 'video'},
        ])
        _reply(
            db, source.id,
            trigger_keyword='优惠,活动',
            media_type='media',
            media_url='https://a/1.jpg',
            media_urls=media,
            content='<b>欢迎</b>光临',
            links=links,
            delete_after=15,
            remark='备注A',
            is_active=True,
        )
        # Same keyword, different content — keyword is not a unique key.
        _reply(db, source.id, trigger_keyword='优惠,活动', content='另一份文案', is_active=False)

        r = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': [dest.id],
        })
        assert r.status_code == 200
        result = r.get_json()['results'][0]
        assert result['error'] is None
        assert result['added'] == 2
        assert result['skipped'] == 0

        rows = AutoReply.query.filter_by(group_id=dest.id).order_by(AutoReply.id.asc()).all()
        assert len(rows) == 2
        assert all(row.is_active is False for row in rows)
        first = rows[0]
        assert first.trigger_keyword == '优惠,活动'
        assert first.media_type == 'media'
        assert first.get_media_url_with_types() == [
            ('https://a/1.jpg', 'image'),
            ('https://a/2.mp4', 'video'),
        ]
        assert first.content == '<b>欢迎</b>光临'
        assert json.loads(first.links)[0]['text'] == '官网'
        assert first.delete_after == 15
        assert first.remark == '备注A'
        assert first.id != AutoReply.query.filter_by(group_id=source.id).first().id
        # Target master switch stays off; source rows stay as they were.
        db.session.refresh(dest)
        db.session.refresh(source)
        assert _conf(dest)['auto_reply_open'] is False
        assert _conf(source)['auto_reply_open'] is True
        assert AutoReply.query.filter_by(group_id=source.id, is_active=True).count() == 1

        # Repeat sync is idempotent.
        r2 = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': [dest.id],
        }).get_json()
        assert r2['results'][0]['added'] == 0
        assert r2['results'][0]['skipped'] == 2
        assert AutoReply.query.filter_by(group_id=dest.id).count() == 2

    def test_native_identical_config_is_skipped_without_keyword_uniqueness(self, db, admin):
        source = _group(db, title='源')
        dest = _group(db, title='目标')
        _reply(db, source.id, trigger_keyword='同样词', content='源文案')
        native = _reply(db, dest.id, trigger_keyword='同样词', content='源文案', is_active=True)
        # Same keyword already exists on dest with different content — still copy.
        _reply(db, source.id, trigger_keyword='同样词', content='不同文案')

        data = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': [dest.id],
        }).get_json()
        assert data['results'][0]['added'] == 1
        assert data['results'][0]['skipped'] == 1
        rows = AutoReply.query.filter_by(group_id=dest.id).all()
        assert len(rows) == 2
        db.session.refresh(native)
        assert native.is_active is True
        assert native.content == '源文案'
        assert auto_reply_fingerprint(native) == auto_reply_fingerprint(
            AutoReply.query.filter_by(group_id=source.id, content='源文案').one()
        )


@pytest.mark.usefixtures('db')
class TestScheduledSync:
    def test_preserves_user_config_and_clears_runtime_and_thread(self, db, admin):
        source = _group(db, title='源', config={'scheduled_msg_open': True})
        dest = _group(db, title='目标', config={'scheduled_msg_open': False})
        links = json.dumps([{'text': '点我', 'url': 'https://t.me/qiqi', 'row': 0, 'order': 0}])
        media = json.dumps(['https://cdn.example/a.jpg', 'https://cdn.example/b.jpg'])
        src = _sched(
            db, source.id,
            media_type='image',
            media_url='https://cdn.example/a.jpg',
            media_urls=media,
            content='<i>相册说明</i>',
            links=links,
            repeat_interval=180,
            delete_previous=True,
            auto_pin=True,
            start_time=BASE_NOW,
            stop_time=datetime(2026, 12, 1, 0, 0, 0),
            remark='早晚公告',
            is_active=True,
            message_thread_id=99,
            target_type='channel',
            target_channel_id='-100999',
            last_message_id=555,
            last_message_ids='[555,556]',
            last_sent_at=BASE_NOW,
            next_send_at=datetime(2026, 11, 20, 15, 0, 0),
            fail_count=3,
        )
        data = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'scheduled_messages',
            'target_group_ids': [dest.id],
        }).get_json()
        assert data['results'][0]['added'] == 1
        copied = ScheduledMessage.query.filter_by(group_id=dest.id).one()
        assert copied.is_active is False
        assert copied.media_type == 'image'
        assert copied.get_media_url_list() == ['https://cdn.example/a.jpg', 'https://cdn.example/b.jpg']
        assert copied.content == '<i>相册说明</i>'
        assert json.loads(copied.links)[0]['url'] == 'https://t.me/qiqi'
        assert copied.repeat_interval == 180
        assert copied.delete_previous is True
        assert copied.auto_pin is True
        assert copied.start_time == BASE_NOW
        assert copied.stop_time == datetime(2026, 12, 1, 0, 0, 0)
        assert copied.remark == '早晚公告'
        assert copied.message_thread_id is None
        assert copied.target_type == 'group'
        assert copied.target_channel_id is None
        assert copied.last_message_id is None
        assert copied.last_message_ids is None
        assert copied.last_sent_at is None
        assert copied.next_send_at is None
        assert copied.fail_count == 0
        assert copied.id != src.id
        db.session.refresh(dest)
        db.session.refresh(src)
        assert _conf(dest)['scheduled_msg_open'] is False
        assert src.is_active is True
        assert src.message_thread_id == 99
        assert src.last_message_id == 555
        assert src.fail_count == 3

        # Disabled copy must not fire even if start_time is already past.
        from app.modules.core import routes
        due = routes.scheduled_should_send(
            copied.last_sent_at, copied.next_send_at, copied.repeat_interval, BASE_NOW
        )
        # The helper itself may say due, but the scheduler skips inactive rows.
        assert copied.is_active is False
        assert ScheduledMessage.query.filter_by(group_id=dest.id, is_active=True).count() == 0

        again = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'scheduled_messages',
            'target_group_ids': [dest.id],
        }).get_json()
        assert again['results'][0]['added'] == 0
        assert again['results'][0]['skipped'] == 1
        assert scheduled_message_fingerprint(copied) == scheduled_message_fingerprint(src)

    def test_sync_does_not_send_messages(self, db, admin, monkeypatch):
        source = _group(db, title='源')
        dest = _group(db, title='目标')
        _sched(db, source.id, start_time=BASE_NOW, is_active=True)
        sent = {'called': False}

        async def _boom(*args, **kwargs):
            sent['called'] = True
            raise AssertionError('must not send')

        monkeypatch.setattr(admin.application, 'bot', None, raising=False)
        from app.modules.core import routes
        monkeypatch.setattr(routes, 'send_multi_media', _boom, raising=False)
        data = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'scheduled_messages',
            'target_group_ids': [dest.id],
        }).get_json()
        assert data['status'] == 'ok'
        assert sent['called'] is False
        assert ScheduledMessage.query.filter_by(group_id=dest.id).one().is_active is False


@pytest.mark.usefixtures('db')
class TestPermissionsAndLimits:
    def test_cross_clone_and_unknown_ids_rejected_without_leak(self, db, admin):
        source = _group(db, title='主号源')
        dest = _group(db, title='主号目标')
        other = _group(db, title='秘密克隆群', clone_id=8)
        _reply(db, source.id, content='不可泄露的文案')
        data = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': [dest.id, other.id, 99999999],
        }).get_json()
        assert data['status'] == 'ok'
        by_id = {row['target_id']: row for row in data['results']}
        assert by_id[dest.id]['added'] == 1
        assert by_id[dest.id]['title'] == '主号目标'
        leaked = json.dumps(data, ensure_ascii=False)
        assert '秘密克隆群' not in leaked
        assert '不可泄露的文案' not in leaked
        assert by_id[other.id]['error'] == '无权或不存在'
        assert by_id[other.id]['title'] == ''
        assert by_id[99999999]['error'] == '无权或不存在'
        assert AutoReply.query.filter_by(group_id=other.id).count() == 0

    def test_clone_admin_cannot_use_main_source(self, db, client):
        source = _group(db, title='主号源')
        dest = _group(db, title='主号目标')
        _reply(db, source.id, content='主号规则')
        _login(client, clone_id=3)
        r = client.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': [dest.id],
        })
        body = r.get_data(as_text=True)
        assert r.status_code in (200, 403)
        assert '主号规则' not in body
        assert AutoReply.query.filter_by(group_id=dest.id).count() == 0

    def test_rejects_channel_target_and_self(self, db, admin):
        source = _group(db, title='源')
        channel = _group(db, title='频道', type='channel')
        _reply(db, source.id)
        data = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': [source.id, channel.id],
        }).get_json()
        errors = {row['error'] for row in data['results']}
        assert '不能同步到源群自己' in errors
        assert '只能同步到已添加的群组' in errors
        assert AutoReply.query.filter_by(group_id=channel.id).count() == 0

    def test_multi_target_and_duplicate_ids(self, db, admin):
        source = _group(db, title='源')
        a = _group(db, title='甲')
        b = _group(db, title='乙')
        _sched(db, source.id, content='一条')
        data = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'scheduled_messages',
            'target_group_ids': [a.id, b.id, a.id],
        }).get_json()
        assert [row['added'] for row in data['results']] == [1, 1]
        assert ScheduledMessage.query.filter_by(group_id=a.id).count() == 1
        assert ScheduledMessage.query.filter_by(group_id=b.id).count() == 1

    def test_empty_and_too_many_targets(self, db, admin):
        source = _group(db)
        _reply(db, source.id)
        empty = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': [],
        })
        assert empty.status_code == 400
        too_many = admin.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': list(range(1, MAX_SYNC_TARGETS + 2)),
        })
        assert too_many.status_code == 400
        assert '最多' in too_many.get_json()['msg']

    def test_in_progress_lock_rejects_repeat_click(self, db, admin):
        from app.services import config_sync_service as svc
        source = _group(db, title='源')
        dest = _group(db, title='目标')
        _reply(db, source.id)
        lock = svc.try_acquire_sync_lock(source.id, 'auto_replies')
        assert lock is not None
        try:
            r = admin.post('/core/api/sync_config_to_groups', json={
                'source_group_id': source.id,
                'kind': 'auto_replies',
                'target_group_ids': [dest.id],
            })
            assert r.status_code == 409
            assert '正在进行' in r.get_json()['msg']
            assert AutoReply.query.filter_by(group_id=dest.id).count() == 0
        finally:
            lock.release()

    def test_requires_login(self, db, client):
        source = _group(db)
        dest = _group(db)
        _reply(db, source.id)
        r = client.post('/core/api/sync_config_to_groups', json={
            'source_group_id': source.id,
            'kind': 'auto_replies',
            'target_group_ids': [dest.id],
        })
        assert r.status_code == 403
        assert AutoReply.query.filter_by(group_id=dest.id).count() == 0


@pytest.mark.usefixtures('db')
class TestFrontendPages:
    def test_both_pages_have_button_and_modal_copy(self, db, admin):
        source = _group(db, title=PAYLOAD)
        _reply(db, source.id, trigger_keyword=PAYLOAD, content=PAYLOAD, remark=PAYLOAD)
        _sched(db, source.id, content=PAYLOAD, remark=PAYLOAD, links=json.dumps([
            {'text': PAYLOAD, 'url': 'https://example.com'}
        ]))
        for page, label in (
            ('auto_replies', '自动回复'),
            ('scheduled_messages', '定时消息'),
        ):
            r = admin.get(f'/core/group/{source.id}/{page}')
            assert r.status_code == 200
            body = r.get_data(as_text=True)
            assert '同步到其他群' in body
            assert f'同步本群全部{label}' in body or 'SYNC_KIND_LABEL' in body
            assert '同步后默认停用，请到目标群确认后启用' in body
            assert 'showConfirm' in body
            assert 'window.confirm' not in body.split('confirmSyncToGroups')[1][:800]
            # Search/pagination count is not hardcoded; JS fills it from the API.
            assert f'（共 {1} 条）' not in body
            # Group title is escaped; the raw payload attribute must not appear unescaped.
            assert 'onerror="auditInjected()"' not in body
