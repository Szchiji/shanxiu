"""Tests for the「过期自动禁言」switch, default compatibility, and backfill."""

import json

import pytest

from app.services.expired_mute import (
    BACKFILL_FLAG,
    backfill_auto_mute_expired_on,
    is_auto_mute_expired_enabled,
    parse_group_config_dict,
)


class TestIsAutoMuteExpiredEnabled:
    def test_missing_conf_defaults_on(self):
        assert is_auto_mute_expired_enabled(None) is True
        assert is_auto_mute_expired_enabled({}) is True
        assert is_auto_mute_expired_enabled({'other': 1}) is True

    def test_explicit_on(self):
        assert is_auto_mute_expired_enabled({'auto_mute_expired': True}) is True
        assert is_auto_mute_expired_enabled({'auto_mute_expired': 1}) is True

    def test_explicit_off(self):
        assert is_auto_mute_expired_enabled({'auto_mute_expired': False}) is False
        assert is_auto_mute_expired_enabled({'auto_mute_expired': 0}) is False


class TestGetGroupConfDefault:
    def test_default_system_is_on(self):
        from app.models import DEFAULT_SYSTEM
        assert DEFAULT_SYSTEM.get('auto_mute_expired') is True

    def test_get_group_conf_missing_key_is_on(self, flask_app):
        from types import SimpleNamespace
        from app.modules.core.routes import get_group_conf

        group = SimpleNamespace(config='{}')
        conf = get_group_conf(group)
        assert is_auto_mute_expired_enabled(conf) is True
        assert conf.get('auto_mute_expired') is True

    def test_get_group_conf_honors_explicit_off(self, flask_app):
        from types import SimpleNamespace
        from app.modules.core.routes import get_group_conf

        group = SimpleNamespace(config=json.dumps({'auto_mute_expired': False}))
        conf = get_group_conf(group)
        assert is_auto_mute_expired_enabled(conf) is False


class TestBackfill:
    def test_backfill_sets_missing_and_false_to_true_once(self, flask_app):
        from app import db
        from app.models import BotGroup, SystemConfig

        with flask_app.app_context():
            g_missing = BotGroup(chat_id='-1001', title='a', type='supergroup', is_active=True, config='{}')
            g_false = BotGroup(
                chat_id='-1002', title='b', type='supergroup', is_active=True,
                config=json.dumps({'auto_mute_expired': False, 'checkin_open': True}),
            )
            g_nested = BotGroup(
                chat_id='-1003', title='c', type='supergroup', is_active=True,
                config=json.dumps({'config': {'auto_mute_expired': False}}),
            )
            db.session.add_all([g_missing, g_false, g_nested])
            db.session.commit()

            # Reset the one-time flag so this test always exercises the first-run path,
            # even when the session-scoped DB was already used by earlier tests.
            existing_flag = SystemConfig.query.filter_by(key_name=BACKFILL_FLAG).first()
            if existing_flag:
                db.session.delete(existing_flag)
                db.session.commit()

            # Session-scoped test DB may already contain groups from earlier tests.
            n = backfill_auto_mute_expired_on(db.session)
            assert n >= 3
            assert SystemConfig.get_value(BACKFILL_FLAG) == '1'

            db.session.refresh(g_missing)
            db.session.refresh(g_false)
            db.session.refresh(g_nested)
            assert json.loads(g_missing.config)['auto_mute_expired'] is True
            conf_false = json.loads(g_false.config)
            assert conf_false['auto_mute_expired'] is True
            assert conf_false['checkin_open'] is True
            assert json.loads(g_nested.config)['config']['auto_mute_expired'] is True

            # Second run is a no-op, and later explicit False would be preserved
            # because the flag is already set. Simulate a post-migration uncheck:
            g_false.config = json.dumps({'auto_mute_expired': False, 'checkin_open': True})
            db.session.commit()
            n2 = backfill_auto_mute_expired_on(db.session)
            assert n2 == 0
            db.session.refresh(g_false)
            assert json.loads(g_false.config)['auto_mute_expired'] is False

    def test_parse_nested_config(self):
        root, conf, nested = parse_group_config_dict(json.dumps({'config': {'a': 1}}))
        assert nested is True
        assert conf == {'a': 1}
        root, conf, nested = parse_group_config_dict(json.dumps({'a': 1}))
        assert nested is False
        assert conf == {'a': 1}
