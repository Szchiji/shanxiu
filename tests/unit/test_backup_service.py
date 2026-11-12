"""Backup export / import: new sections, legacy files, no secret leakage."""

import json
import uuid

import pytest

from app.services.backup_service import (
    BACKUP_EXCLUDED_LABELS,
    BACKUP_INCLUDED_LABELS,
    export_group_config,
    import_group_config,
    sanitize_secrets,
)


def _group(db, suffix=''):
    from app.models import BotGroup
    g = BotGroup(
        chat_id=f'-backup-{uuid.uuid4().hex[:10]}{suffix}',
        title='backup-group',
        type='supergroup',
        is_active=True,
        config=json.dumps({'checkin_open': True, 'checkin_cmd': '打卡'}, ensure_ascii=False),
        fields_config=json.dumps([{'key': '昵称', 'label': '昵称'}], ensure_ascii=False),
    )
    db.session.add(g)
    db.session.commit()
    return g


class TestSanitizeSecrets:
    def test_strips_token_and_session_keys(self):
        raw = {
            'name': 'ok',
            'bot_token': '123:ABCDEF',
            'nested': {'api_hash': 'deadbeef', 'session_enc': 'SESS', 'keep': 1},
            'list': [{'token': 'x', 'title': 't'}],
            'announce_channel_id': '-1001',
        }
        cleaned = sanitize_secrets(raw)
        dumped = json.dumps(cleaned)
        assert '123:ABCDEF' not in dumped
        assert 'deadbeef' not in dumped
        assert 'SESS' not in dumped
        assert cleaned['name'] == 'ok'
        assert cleaned['announce_channel_id'] == '-1001'
        assert cleaned['nested']['keep'] == 1
        assert 'token' not in cleaned['list'][0]
        assert cleaned['list'][0]['title'] == 't'


@pytest.mark.usefixtures('db')
class TestExportImport:
    def test_export_includes_new_sections_and_docs(self, db):
        from app.models import (
            KeywordFilter, OtherSettings, GroupPluginSettings, ChannelCoupon,
        )
        g = _group(db)
        db.session.add(KeywordFilter(group_id=g.id, keyword='广告', action='delete'))
        db.session.add(OtherSettings(group_id=g.id, channel_auto_buttons=True))
        db.session.add(GroupPluginSettings(group_id=g.id, plugin_name='lottery', enabled=False))
        db.session.add(ChannelCoupon(group_id=g.id, coupon_name='券1', button_text='领', button_url='https://t.me/x'))
        db.session.commit()

        payload = export_group_config(g.id)
        assert payload['backup_version'] == 2
        assert payload['includes'] == BACKUP_INCLUDED_LABELS
        assert payload['excludes'] == BACKUP_EXCLUDED_LABELS
        assert payload['keyword_filters'][0]['keyword'] == '广告'
        assert payload['other_settings']['channel_auto_buttons'] is True
        assert payload['plugin_settings'][0]['plugin_name'] == 'lottery'
        assert payload['channel_coupons'][0]['coupon_name'] == '券1'
        assert 'config' in payload
        assert 'auto_replies' in payload
        # operational / secret collections must not be present
        for banned in (
            'users', 'group_users', 'user_points', 'points_log',
            'sync_logs', 'sync_message_logs', 'bot_token', 'session_enc',
        ):
            assert banned not in payload

    def test_export_does_not_leak_injected_secrets(self, db):
        g = _group(db)
        # Even if someone stuffed a token into group.config, export must strip it.
        g.config = json.dumps({'checkin_cmd': '打卡', 'bot_token': '999:LEAK', 'api_hash': 'HASH'}, ensure_ascii=False)
        db.session.commit()
        payload = export_group_config(g.id)
        dumped = json.dumps(payload)
        assert '999:LEAK' not in dumped
        assert 'HASH' not in dumped
        assert 'bot_token' not in dumped
        assert payload['config']['checkin_cmd'] == '打卡'

    def test_import_legacy_file_does_not_crash(self, db):
        src = _group(db, 'src')
        dest = _group(db, 'dst')
        from app.models import AutoReply, PointsRule, MemberLevel
        db.session.add(AutoReply(group_id=src.id, trigger_keyword='你好', content='欢迎'))
        db.session.add(PointsRule(group_id=src.id, rule_name='发言', rule_type='message', points_amount=2))
        db.session.add(MemberLevel(group_id=src.id, level_name='LV1', required_points=10))
        db.session.commit()

        # Legacy shape: only the original six keys.
        legacy = {
            'group_info': {'title': 'old', 'chat_id': '-1', 'type': 'supergroup'},
            'config': {'checkin_cmd': '签到打卡'},
            'fields_config': [{'key': '地区'}],
            'auto_replies': [{'trigger_keyword': '你好', 'content': '欢迎', 'is_active': True}],
            'scheduled_messages': [],
            'points_rules': [{'rule_name': '发言', 'rule_type': 'message', 'points_amount': 2, 'is_active': True}],
            'member_levels': [{'level_name': 'LV1', 'required_points': 10}],
        }
        result = import_group_config(dest.id, legacy)
        assert 'config' in result['imported']
        from app.models import BotGroup, AutoReply as AR, PointsRule as PR, MemberLevel as ML
        dest2 = BotGroup.query.get(dest.id)
        conf = json.loads(dest2.config)
        assert conf['checkin_cmd'] == '签到打卡'
        assert AR.query.filter_by(group_id=dest.id, trigger_keyword='你好').count() == 1
        assert PR.query.filter_by(group_id=dest.id, rule_name='发言').count() == 1
        assert ML.query.filter_by(group_id=dest.id, level_name='LV1').count() == 1

    def test_import_new_sections_roundtrip(self, db):
        from app.models import (
            KeywordFilter, ForcedChannelSubscription, GroupLottery, GroupBottomButton,
            SyncGroupMessages, ChannelForwardRule,
        )
        src = _group(db, 's2')
        dest = _group(db, 'd2')
        db.session.add(KeywordFilter(group_id=src.id, keyword='spam', filter_type='blacklist', action='warn'))
        db.session.add(ForcedChannelSubscription(group_id=src.id, enabled=True, channel_id='-1009', check_interval=1800))
        db.session.add(GroupLottery(
            group_id=src.id, lottery_name='周末抽奖', lottery_type='random',
            top_n_winners=2, pool_source='all_members', status='pending',
        ))
        db.session.add(GroupBottomButton(group_id=src.id, button_text='菜单', button_url='https://t.me/x', row_position=0))
        db.session.add(SyncGroupMessages(source_group_id=src.id, target_group_id='-10022', enabled=True, sync_media=True))
        db.session.add(ChannelForwardRule(group_id=src.id, rule_name='转发A', target_chat_id='-1003', source_keywords='合作'))
        db.session.commit()

        payload = export_group_config(src.id)
        import_group_config(dest.id, payload)

        assert KeywordFilter.query.filter_by(group_id=dest.id, keyword='spam').first().action == 'warn'
        sub = ForcedChannelSubscription.query.filter_by(group_id=dest.id).first()
        assert sub.enabled is True
        assert sub.channel_id == '-1009'
        lot = GroupLottery.query.filter_by(group_id=dest.id, lottery_name='周末抽奖').first()
        assert lot.lottery_type == 'random'
        assert lot.pool_source == 'all_members'
        assert GroupBottomButton.query.filter_by(group_id=dest.id, button_text='菜单').count() == 1
        assert SyncGroupMessages.query.filter_by(source_group_id=dest.id, target_group_id='-10022').first().enabled is True
        assert ChannelForwardRule.query.filter_by(group_id=dest.id, rule_name='转发A').first().target_chat_id == '-1003'

    def test_import_invalid_payload_raises(self, db):
        g = _group(db)
        with pytest.raises(ValueError):
            import_group_config(g.id, ['not', 'a', 'dict'])
