"""
tests/unit/test_warning_service.py
----------------------------------
Unit tests for warning format helpers, escalation stub, and DB record/count.
"""

import pytest
from app.services.warning_service import (
    format_command_warn_reply,
    format_keyword_warn_reply,
    format_punishment_line,
    format_unwarn_reply,
    resolve_escalation,
    record_warning,
    count_warnings,
    clear_warnings,
    normalize_warn_settings,
)


class TestFormatHelpers:
    def test_command_reply_includes_count(self):
        text = format_command_warn_reply('Alice', '刷屏', 3)
        assert 'Alice' in text
        assert '刷屏' in text
        assert '累计警告: 3 次' in text
        assert '⚠️' in text

    def test_command_reply_defaults(self):
        text = format_command_warn_reply('', '', 1)
        assert '该用户' in text
        assert '违反群规' in text
        assert '累计警告: 1 次' in text

    def test_keyword_reply_includes_count(self):
        text = format_keyword_warn_reply(2)
        assert '禁止关键词' in text
        assert '2 次' in text


class TestResolveEscalation:
    def test_no_auto_punish_without_settings(self):
        """No UI/settings → never escalate (production-safe default)."""
        for n in (0, 1, 3, 5, 10, 100):
            action, label = resolve_escalation(n)
            assert action is None
            assert label is None
        action, label = resolve_escalation(5, {})
        assert action is None
        assert label is None

    def test_default_normalize_is_disabled(self):
        cfg = normalize_warn_settings(None)
        assert cfg['warn_auto_punish'] is False
        assert cfg['warn_threshold'] == 3
        assert cfg['warn_action'] == 'mute'

    def test_triggers_at_and_above_threshold(self):
        settings = {'warn_auto_punish': True, 'warn_threshold': 3, 'warn_action': 'ban'}
        assert resolve_escalation(2, settings) == (None, None)
        action, label = resolve_escalation(3, settings)
        assert action == 'ban'
        assert label == '封禁'
        action, _ = resolve_escalation(4, settings)
        assert action == 'ban'

    def test_mute_and_kick_actions(self):
        settings = {'warn_auto_punish': True, 'warn_threshold': 1, 'warn_action': 'mute', 'warn_mute_minutes': 15}
        action, label = resolve_escalation(1, settings)
        assert action == 'mute'
        assert label == '禁言'
        settings['warn_action'] = 'kick'
        action, label = resolve_escalation(1, settings)
        assert action == 'kick'
        assert '踢' in label

    def test_punishment_and_unwarn_text(self):
        assert '禁言 15 分钟' in format_punishment_line('mute', 15)
        assert '踢出' in format_punishment_line('kick')
        assert '封禁' in format_punishment_line('ban')
        text = format_command_warn_reply('Bob', '刷屏', 3, format_punishment_line('mute', 60))
        assert '累计警告: 3 次' in text
        assert '已执行处罚' in text
        kw = format_keyword_warn_reply(3, format_punishment_line('ban'))
        assert '3 次' in kw
        assert '封禁' in kw
        assert '没有警告' in format_unwarn_reply('Bob', 0)
        assert '2 次' in format_unwarn_reply('Bob', 2)


@pytest.mark.usefixtures('db')
class TestRecordAndCount:
    def _make_group(self, db, suffix=''):
        import uuid
        from app.models import BotGroup
        g = BotGroup(
            chat_id=f'-warn-{uuid.uuid4().hex[:12]}{suffix}',
            title='test-warn-group',
            type='supergroup',
            is_active=True,
        )
        db.session.add(g)
        db.session.commit()
        return g

    def test_record_increments_count(self, db):
        g = self._make_group(db)
        c1 = record_warning(g.id, 42, admin_id=7, reason='spam', source='command')
        assert c1 == 1
        c2 = record_warning(g.id, 42, admin_id=7, reason='again', source='command')
        assert c2 == 2
        assert count_warnings(g.id, 42) == 2
        assert count_warnings(g.id, 99) == 0

    def test_keyword_source_and_null_admin(self, db):
        from app.models import GroupWarning
        g = self._make_group(db)
        c = record_warning(g.id, 55, admin_id=None, reason='关键词过滤', source='keyword')
        assert c == 1
        row = GroupWarning.query.filter_by(group_id=g.id, user_id=55).first()
        assert row is not None
        assert row.source == 'keyword'
        assert row.admin_id is None
        assert row.reason == '关键词过滤'

    def test_counts_are_per_group(self, db):
        g1 = self._make_group(db)
        from app.models import BotGroup
        g2 = BotGroup(chat_id=f'-warn2-{__import__("uuid").uuid4().hex[:12]}', title='other', type='supergroup', is_active=True)
        db.session.add(g2)
        db.session.commit()
        record_warning(g1.id, 1, reason='a')
        record_warning(g1.id, 1, reason='b')
        record_warning(g2.id, 1, reason='c')
        assert count_warnings(g1.id, 1) == 2
        assert count_warnings(g2.id, 1) == 1

    def test_clear_warnings(self, db):
        g = self._make_group(db)
        record_warning(g.id, 7, reason='a')
        record_warning(g.id, 7, reason='b')
        record_warning(g.id, 8, reason='c')
        cleared = clear_warnings(g.id, 7)
        assert cleared == 2
        assert count_warnings(g.id, 7) == 0
        assert count_warnings(g.id, 8) == 1
        assert clear_warnings(g.id, 7) == 0
