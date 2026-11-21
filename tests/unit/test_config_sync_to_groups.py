"""Copy auto-reply / scheduled-message config to other groups and follow enable/pause."""

import json
from datetime import datetime
from uuid import uuid4

import pytest

from app.models import AutoReply, BotGroup, ConfigSyncLink, ScheduledMessage
from app.services.config_sync_service import (
    MAX_SYNC_TARGETS,
    auto_reply_fingerprint,
    scheduled_message_fingerprint,
)
from app.utils import get_beijing_now


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


def _sync(client, source_id, kind, target_ids):
    return client.post('/core/api/sync_config_to_groups', json={
        'source_group_id': source_id,
        'kind': kind,
        'target_group_ids': target_ids,
    })


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
        assert data['can_be_source'] is True
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
    def test_copies_all_fields_follows_status_and_updates_links(self, db, admin):
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

        r = _sync(admin, source.id, 'auto_replies', [dest.id])
        assert r.status_code == 200
        result = r.get_json()['results'][0]
        assert result['error'] is None
        assert result['added'] == 2
        assert result['updated'] == 0

        rows = AutoReply.query.filter_by(group_id=dest.id).order_by(AutoReply.id.asc()).all()
        assert len(rows) == 2
        first, second = rows
        assert first.is_active is True
        assert second.is_active is False
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
        # Sync enables the target master switch so the copied rule is usable.
        db.session.refresh(dest)
        db.session.refresh(source)
        assert _conf(dest)['auto_reply_open'] is True
        assert _conf(source)['auto_reply_open'] is True
        assert AutoReply.query.filter_by(group_id=source.id, is_active=True).count() == 1
        assert ConfigSyncLink.query.filter_by(
            kind='auto_replies', source_group_id=source.id, target_group_id=dest.id
        ).count() == 2

        # Repeat sync updates already-linked rows instead of inserting duplicates.
        r2 = _sync(admin, source.id, 'auto_replies', [dest.id]).get_json()
        assert r2['results'][0]['added'] == 0
        assert r2['results'][0]['updated'] == 2
        assert AutoReply.query.filter_by(group_id=dest.id).count() == 2

    def test_native_identical_config_is_not_claimed(self, db, admin):
        source = _group(db, title='源', config={'auto_reply_open': True})
        dest = _group(db, title='目标', config={'auto_reply_open': True})
        _reply(db, source.id, trigger_keyword='同样词', content='源文案')
        native = _reply(db, dest.id, trigger_keyword='同样词', content='源文案', is_active=True)
        # Same keyword already exists on dest with different content — still copy.
        _reply(db, source.id, trigger_keyword='同样词', content='不同文案')

        data = _sync(admin, source.id, 'auto_replies', [dest.id]).get_json()
        assert data['results'][0]['added'] == 2
        assert data['results'][0]['updated'] == 0
        rows = AutoReply.query.filter_by(group_id=dest.id).all()
        assert len(rows) == 3
        db.session.refresh(native)
        assert native.is_active is True
        assert native.content == '源文案'
        assert ConfigSyncLink.query.filter_by(kind='auto_replies', target_item_id=native.id).count() == 0
        assert auto_reply_fingerprint(native) == auto_reply_fingerprint(
            AutoReply.query.filter_by(group_id=source.id, content='源文案').one()
        )

    def test_editing_source_updates_linked_target_content(self, db, admin):
        source = _group(db, title='源', config={'auto_reply_open': True})
        dest = _group(db, title='目标', config={'auto_reply_open': True})
        src = _reply(db, source.id, trigger_keyword='旧关键词', content='旧文案')
        assert _sync(admin, source.id, 'auto_replies', [dest.id]).get_json()['status'] == 'ok'

        response = admin.post('/core/api/save_auto_reply', json={
            'id': src.id,
            'group_id': source.id,
            'trigger_keyword': '新关键词',
            'media_type': 'text',
            'media_urls': [],
            'content': '新文案',
            'links': '[]',
            'delete_after': 8,
            'remark': '已更新',
        })
        assert response.get_json()['status'] == 'ok'
        copied = AutoReply.query.filter_by(group_id=dest.id).one()
        assert copied.trigger_keyword == '新关键词'
        assert copied.content == '新文案'
        assert copied.delete_after == 8
        assert copied.remark == '已更新'


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
        data = _sync(admin, source.id, 'scheduled_messages', [dest.id]).get_json()
        assert data['results'][0]['added'] == 1
        copied = ScheduledMessage.query.filter_by(group_id=dest.id).one()
        assert copied.is_active is True
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
        assert copied.fail_count == 0
        assert copied.id != src.id
        db.session.refresh(dest)
        db.session.refresh(src)
        assert _conf(dest)['scheduled_msg_open'] is True
        assert src.is_active is True
        assert src.message_thread_id == 99
        assert src.last_message_id == 555
        assert src.fail_count == 3

        # Enabling a recurring copy aligns the next slot and must not backfill immediately.
        from app.modules.core import routes
        now = get_beijing_now()
        assert copied.next_send_at is not None
        assert routes.scheduled_should_send(
            copied.last_sent_at, copied.next_send_at, copied.repeat_interval, now
        ) is False

        again = _sync(admin, source.id, 'scheduled_messages', [dest.id]).get_json()
        assert again['results'][0]['added'] == 0
        assert again['results'][0]['updated'] == 1
        db.session.refresh(copied)
        assert scheduled_message_fingerprint(copied) == scheduled_message_fingerprint(src)

    def test_sync_does_not_send_messages(self, db, admin, monkeypatch):
        source = _group(db, title='源', config={'scheduled_msg_open': True})
        dest = _group(db, title='目标', config={'scheduled_msg_open': True})
        _sched(db, source.id, start_time=BASE_NOW, is_active=True)
        sent = {'called': False}

        async def _boom(*args, **kwargs):
            sent['called'] = True
            raise AssertionError('must not send')

        monkeypatch.setattr(admin.application, 'bot', None, raising=False)
        from app.modules.core import routes
        monkeypatch.setattr(routes, 'send_multi_media', _boom, raising=False)
        data = _sync(admin, source.id, 'scheduled_messages', [dest.id]).get_json()
        assert data['status'] == 'ok'
        assert sent['called'] is False

    def test_editing_source_updates_linked_target_content(self, db, admin):
        source = _group(db, title='源', config={'scheduled_msg_open': True})
        dest = _group(db, title='目标', config={'scheduled_msg_open': True})
        src = _sched(db, source.id, content='旧公告', repeat_interval=60)
        assert _sync(admin, source.id, 'scheduled_messages', [dest.id]).get_json()['status'] == 'ok'
        copied = ScheduledMessage.query.filter_by(group_id=dest.id).one()
        copied.last_sent_at = BASE_NOW
        copied.next_send_at = BASE_NOW.replace(hour=13)
        db.session.commit()

        response = admin.post('/core/api/save_scheduled_message', json={
            'id': src.id,
            'group_id': source.id,
            'media_type': 'text',
            'media_urls': [],
            'content': '新公告',
            'links': '[]',
            'repeat_interval': 60,
            'start_time': BASE_NOW.isoformat(),
            'stop_time': None,
            'delete_previous': False,
            'auto_pin': False,
            'target_type': 'group',
        })
        assert response.get_json()['status'] == 'ok'
        db.session.refresh(copied)
        assert copied.content == '新公告'
        assert copied.last_sent_at == BASE_NOW
        assert copied.next_send_at == BASE_NOW.replace(hour=13)


@pytest.mark.usefixtures('db')
class TestFollowStatus:
    def test_follows_source_enable_and_pause(self, db, admin):
        source = _group(db, title='源', config={'auto_reply_open': True})
        dest = _group(db, title='目标', config={'auto_reply_open': True})
        src = _reply(db, source.id, content='跟随我', is_active=True)
        native = _reply(db, dest.id, content='本地规则', is_active=True)
        assert _sync(admin, source.id, 'auto_replies', [dest.id]).status_code == 200
        linked = AutoReply.query.filter_by(group_id=dest.id, content='跟随我').one()
        assert linked.is_active is True
        assert native.is_active is True

        r = admin.post('/core/api/toggle_auto_reply', json={'id': src.id})
        assert r.get_json()['status'] == 'ok'
        db.session.refresh(src)
        db.session.refresh(linked)
        db.session.refresh(native)
        assert src.is_active is False
        assert linked.is_active is False
        assert native.is_active is True

        refused = admin.post('/core/api/toggle_auto_reply', json={'id': linked.id})
        assert refused.get_json()['status'] == 'error'
        assert '源群' in refused.get_json()['msg']
        db.session.refresh(linked)
        assert linked.is_active is False

        admin.post('/core/api/toggle_auto_reply', json={'id': src.id})
        db.session.refresh(linked)
        assert linked.is_active is True

    def test_source_master_switch_only_affects_linked_rules(self, db, admin):
        source = _group(db, title='源', config={'auto_reply_open': True, 'scheduled_msg_open': False})
        dest = _group(db, title='目标', config={'auto_reply_open': True, 'scheduled_msg_open': False})
        _reply(db, source.id, is_active=True)
        native = _reply(db, dest.id, content='本地', is_active=True)
        _sync(admin, source.id, 'auto_replies', [dest.id])
        linked = AutoReply.query.filter(
            AutoReply.group_id == dest.id, AutoReply.id != native.id
        ).one()
        assert linked.is_active is True

        r = admin.post('/core/api/save_settings', json={
            'group_id': source.id,
            'config': {'auto_reply_open': False, 'scheduled_msg_open': False},
        })
        assert r.get_json()['status'] == 'ok'
        db.session.refresh(linked)
        db.session.refresh(native)
        db.session.refresh(dest)
        db.session.refresh(source)
        assert linked.is_active is False
        assert native.is_active is True
        assert _conf(dest)['auto_reply_open'] is False
        assert _conf(source)['auto_reply_open'] is False

        # Turning the source switch back on resumes only the linked rule.
        admin.post('/core/api/save_settings', json={
            'group_id': source.id,
            'config': {'auto_reply_open': True, 'scheduled_msg_open': False},
        })
        db.session.refresh(linked)
        assert linked.is_active is True

    def test_dest_master_follows_source(self, db, admin):
        source = _group(db, title='源', config={'auto_reply_open': True})
        dest = _group(db, title='目标', config={'auto_reply_open': False})
        src = _reply(db, source.id, is_active=True)
        _sync(admin, source.id, 'auto_replies', [dest.id])
        linked = AutoReply.query.filter_by(group_id=dest.id).one()
        assert linked.is_active is True
        db.session.refresh(dest)
        assert _conf(dest)['auto_reply_open'] is True

        admin.post('/core/api/toggle_auto_reply', json={'id': src.id})
        admin.post('/core/api/toggle_auto_reply', json={'id': src.id})
        db.session.refresh(dest)
        db.session.refresh(linked)
        assert linked.is_active is True
        assert _conf(dest)['auto_reply_open'] is True

        # A local target setting remains local until the source master setting changes.
        admin.post('/core/api/save_settings', json={
            'group_id': dest.id,
            'config': {'auto_reply_open': False, 'scheduled_msg_open': False},
        })
        admin.post('/core/api/toggle_auto_reply', json={'id': src.id})
        db.session.refresh(dest)
        db.session.refresh(linked)
        assert _conf(dest)['auto_reply_open'] is False
        assert linked.is_active is False

    def test_scheduled_toggle_follows_without_backfill(self, db, admin):
        from app.modules.core import routes
        source = _group(db, title='源', config={'scheduled_msg_open': True})
        dest = _group(db, title='目标', config={'scheduled_msg_open': True})
        src = _sched(db, source.id, start_time=BASE_NOW, repeat_interval=180, is_active=True)
        assert _sync(admin, source.id, 'scheduled_messages', [dest.id]).status_code == 200
        copied = ScheduledMessage.query.filter_by(group_id=dest.id).one()
        assert copied.is_active is True

        admin.post('/core/api/toggle_scheduled_message', json={'id': src.id})
        db.session.refresh(copied)
        assert copied.is_active is False

        refused = admin.post('/core/api/toggle_scheduled_message', json={'id': copied.id})
        assert refused.get_json()['status'] == 'error'
        db.session.refresh(copied)
        assert copied.is_active is False

        admin.post('/core/api/toggle_scheduled_message', json={'id': src.id})
        db.session.expire_all()
        copied = ScheduledMessage.query.filter_by(group_id=dest.id).one()
        assert copied.is_active is True
        assert copied.next_send_at is not None
        assert routes.scheduled_should_send(
            copied.last_sent_at, copied.next_send_at, copied.repeat_interval, get_beijing_now()
        ) is False

        # Batch enable/pause on the dest skips followed rows.
        paused = admin.post('/core/api/batch_scheduled_messages', json={
            'ids': [copied.id],
            'action': 'pause',
        }).get_json()
        assert paused['status'] == 'ok'
        assert paused['skipped_followed'] == 1
        assert paused['count'] == 0
        db.session.refresh(copied)
        assert copied.is_active is True

    def test_refuse_follower_as_source_and_source_as_target(self, db, admin):
        source = _group(db, title='甲', config={'auto_reply_open': True})
        follower = _group(db, title='乙', config={'auto_reply_open': True})
        other = _group(db, title='丙', config={'auto_reply_open': True})
        _reply(db, source.id, content='源规则')
        _reply(db, follower.id, content='乙本地')
        _reply(db, other.id, content='丙规则')
        assert _sync(admin, source.id, 'auto_replies', [follower.id]).status_code == 200

        blocked = _sync(admin, follower.id, 'auto_replies', [other.id])
        assert blocked.status_code == 400
        assert '不能再作为源' in blocked.get_json()['msg']
        assert AutoReply.query.filter_by(group_id=other.id, content='乙本地').count() == 0

        listed = admin.get(f'/core/api/config_sync_targets/{follower.id}?kind=auto_replies').get_json()
        assert listed['can_be_source'] is False
        assert '不能再作为源' in listed['source_block_reason']

        # A group that already has outgoing links cannot receive (no chains / cycles).
        receive = _sync(admin, other.id, 'auto_replies', [source.id]).get_json()
        assert receive['results'][0]['error'] == '目标群已作为源同步到其他群，不能再接收跟随'
        assert AutoReply.query.filter_by(group_id=source.id).count() == 1

        targets = admin.get(f'/core/api/config_sync_targets/{other.id}?kind=auto_replies').get_json()
        by_id = {t['id']: t for t in targets['targets']}
        assert by_id[source.id]['can_receive'] is False
        assert '不能再接收' in by_id[source.id]['block_reason']
        assert by_id[follower.id]['can_receive'] is True

    def test_resync_updates_content_without_duplicate(self, db, admin):
        source = _group(db, title='源', config={'auto_reply_open': True})
        dest = _group(db, title='目标', config={'auto_reply_open': True})
        src = _reply(db, source.id, content='旧文案', is_active=True)
        _sync(admin, source.id, 'auto_replies', [dest.id])
        src.content = '新文案'
        src.is_active = False
        db.session.commit()
        data = _sync(admin, source.id, 'auto_replies', [dest.id]).get_json()
        assert data['results'][0]['added'] == 0
        assert data['results'][0]['updated'] == 1
        rows = AutoReply.query.filter_by(group_id=dest.id).all()
        assert len(rows) == 1
        assert rows[0].content == '新文案'
        assert rows[0].is_active is False

    def test_delete_detaches_without_cascade_deleting_the_other_row(self, db, admin):
        source = _group(db, title='源', config={'auto_reply_open': True})
        dest = _group(db, title='目标', config={'auto_reply_open': True})
        src = _reply(db, source.id, content='可删', is_active=True)
        _sync(admin, source.id, 'auto_replies', [dest.id])
        linked = AutoReply.query.filter_by(group_id=dest.id).one()
        admin.post('/core/api/delete_auto_reply', json={'id': src.id})
        assert AutoReply.query.get(src.id) is None
        leftover = AutoReply.query.get(linked.id)
        assert leftover is not None
        assert ConfigSyncLink.query.filter(
            (ConfigSyncLink.source_item_id == src.id) | (ConfigSyncLink.target_item_id == linked.id)
        ).count() == 0
        # After detach the leftover row can be toggled locally.
        r = admin.post('/core/api/toggle_auto_reply', json={'id': leftover.id})
        assert r.get_json()['status'] == 'ok'


@pytest.mark.usefixtures('db')
class TestPermissionsAndLimits:
    def test_cross_clone_and_unknown_ids_rejected_without_leak(self, db, admin):
        source = _group(db, title='主号源')
        dest = _group(db, title='主号目标')
        other = _group(db, title='秘密克隆群', clone_id=8)
        _reply(db, source.id, content='不可泄露的文案')
        data = _sync(admin, source.id, 'auto_replies', [dest.id, other.id, 99999999]).get_json()
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
        r = _sync(client, source.id, 'auto_replies', [dest.id])
        body = r.get_data(as_text=True)
        assert r.status_code in (200, 403)
        assert '主号规则' not in body
        assert AutoReply.query.filter_by(group_id=dest.id).count() == 0

    def test_rejects_channel_target_and_self(self, db, admin):
        source = _group(db, title='源')
        channel = _group(db, title='频道', type='channel')
        _reply(db, source.id)
        data = _sync(admin, source.id, 'auto_replies', [source.id, channel.id]).get_json()
        errors = {row['error'] for row in data['results']}
        assert '不能同步到源群自己' in errors
        assert '只能同步到已添加的群组' in errors
        assert AutoReply.query.filter_by(group_id=channel.id).count() == 0

    def test_multi_target_and_duplicate_ids(self, db, admin):
        source = _group(db, title='源')
        a = _group(db, title='甲')
        b = _group(db, title='乙')
        _sched(db, source.id, content='一条')
        data = _sync(admin, source.id, 'scheduled_messages', [a.id, b.id, a.id]).get_json()
        assert [row['added'] for row in data['results']] == [1, 1]
        assert ScheduledMessage.query.filter_by(group_id=a.id).count() == 1
        assert ScheduledMessage.query.filter_by(group_id=b.id).count() == 1

    def test_empty_and_too_many_targets(self, db, admin):
        source = _group(db)
        _reply(db, source.id)
        empty = _sync(admin, source.id, 'auto_replies', [])
        assert empty.status_code == 400
        too_many = _sync(admin, source.id, 'auto_replies', list(range(1, MAX_SYNC_TARGETS + 2)))
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
            r = _sync(admin, source.id, 'auto_replies', [dest.id])
            assert r.status_code == 409
            assert '正在进行' in r.get_json()['msg']
            assert AutoReply.query.filter_by(group_id=dest.id).count() == 0
        finally:
            lock.release()

    def test_requires_login(self, db, client):
        source = _group(db)
        dest = _group(db)
        _reply(db, source.id)
        r = _sync(client, source.id, 'auto_replies', [dest.id])
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
            assert '跟随源规则当前的开启或停止' in body
            assert '再次同步会更新已关联' in body
            assert '同步后默认停用' not in body
            assert 'showConfirm' in body
            assert 'window.confirm' not in body.split('confirmSyncToGroups')[1][:800]
            assert f'（共 {1} 条）' not in body
            assert 'onerror="auditInjected()"' not in body
            assert 'btn-sync-config-to-groups' in body
            assert 'id="syncConfigToGroupsBtn"' in body
            assert 'id="syncConfigConfirmBtn"' in body
            # Dedicated class on the toolbar and modal confirm buttons only.
            assert body.count('class="btn btn-sm btn-sync-config-to-groups"') == 1
            assert 'id="syncConfigConfirmBtn"' in body
            assert 'btn-sync-config-to-groups" id="syncConfigConfirmBtn"' in body
            assert '#6f42c1' in body
            assert '.btn-sync-config-to-groups:hover' in body
            assert '.btn-sync-config-to-groups:focus' in body
            assert '.btn-sync-config-to-groups:disabled' in body
            assert '[data-bs-theme="dark"] .btn-sync-config-to-groups' in body
            # Export / import keep their original colors.
            assert 'btn btn-success btn-sm' in body
            assert 'btn btn-info btn-sm' in body
            assert 'btn-outline-primary btn-sm" id="syncConfigToGroupsBtn"' not in body
            assert 'btn-primary" id="syncConfigConfirmBtn"' not in body

    def test_dest_page_marks_follow_and_disables_toggle(self, db, admin):
        source = _group(db, title='源群甲', config={'auto_reply_open': True})
        dest = _group(db, title='目标乙', config={'auto_reply_open': True})
        _reply(db, source.id, content='跟随文案', is_active=True)
        _sync(admin, source.id, 'auto_replies', [dest.id])

        dest_page = admin.get(f'/core/group/{dest.id}/auto_replies').get_data(as_text=True)
        assert '跟随「源群甲」' in dest_page
        assert '请到源群修改开启或停止' in dest_page
        assert 'fa-link' in dest_page

        source_page = admin.get(f'/core/group/{source.id}/auto_replies').get_data(as_text=True)
        assert '已同步到 1 个群' in source_page

        settings = admin.get(f'/core/group/{dest.id}/settings').get_data(as_text=True)
        assert '本群自己的功能总开关' in settings
