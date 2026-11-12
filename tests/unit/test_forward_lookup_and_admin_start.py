"""Tests for forwarded-user lookup auth and admin /start message loading."""

import json

from app.services.forward_lookup_auth import (
    FORWARD_LOOKUP_DENIED_TEXT,
    is_forward_lookup_authorized,
    user_is_any_clone_admin,
    user_is_recorded_group_admin,
)


class TestForwardLookupAuth:
    def test_global_admin_allowed(self):
        assert is_forward_lookup_authorized(100, admin_id=100) is True

    def test_clone_admin_allowed(self):
        assert is_forward_lookup_authorized(200, admin_id=1, is_clone_admin=True) is True

    def test_registered_group_admin_allowed(self):
        assert is_forward_lookup_authorized(300, admin_id=1, is_registered_group_admin=True) is True

    def test_ordinary_user_denied(self):
        assert is_forward_lookup_authorized(400, admin_id=1) is False
        assert '管理员' in FORWARD_LOOKUP_DENIED_TEXT
        # Rejection text must not mention the target user
        assert '用户ID' not in FORWARD_LOOKUP_DENIED_TEXT
        assert '积分' not in FORWARD_LOOKUP_DENIED_TEXT

    def test_recorded_group_admin_and_clone_admin(self, flask_app):
        from app import db
        from app.models import BotClone, BotGroup, GroupMember

        with flask_app.app_context():
            group = BotGroup(chat_id='-10011', title='g', type='supergroup', is_active=True)
            db.session.add(group)
            db.session.flush()
            db.session.add(GroupMember(
                group_id=group.id, user_id=555, first_name='Admin', status='administrator'
            ))
            db.session.add(GroupMember(
                group_id=group.id, user_id=556, first_name='Member', status='member'
            ))
            clone = BotClone(
                bot_token='x', clone_name='c', is_active=True,
                owner_user_id=777, admin_user_ids=json.dumps([778]),
            )
            db.session.add(clone)
            db.session.commit()

            assert user_is_recorded_group_admin(555) is True
            assert user_is_recorded_group_admin(556) is False
            assert user_is_any_clone_admin(777) is True
            assert user_is_any_clone_admin(778) is True
            assert user_is_any_clone_admin(556) is False


class TestAdminStartMessageLoad:
    def test_loads_admin_type_and_not_user_type(self, flask_app):
        from app import db
        from app.models import BotGroup, StartMessage
        from app.modules.core.routes import _load_start_message_payload

        with flask_app.app_context():
            group = BotGroup(
                chat_id='-10021', title='g', type='supergroup', is_active=True,
                config=json.dumps({'start_msg_open': True}),
            )
            db.session.add(group)
            db.session.flush()
            db.session.add(StartMessage(
                group_id=group.id,
                message_type='user',
                content='普通用户欢迎',
                is_active=True,
                links='[]',
            ))
            db.session.add(StartMessage(
                group_id=group.id,
                message_type='admin',
                content='管理员自定义内容',
                media_type='text',
                is_active=True,
                links=json.dumps([{'text': '后台', 'url': 'https://example.com'}]),
            ))
            db.session.commit()

            admin_payload = _load_start_message_payload(None, 'admin')
            user_payload = _load_start_message_payload(None, 'user')
            assert admin_payload is not None
            assert admin_payload['content'] == '管理员自定义内容'
            assert user_payload['content'] == '普通用户欢迎'

    def test_respects_start_msg_open_off(self, flask_app):
        from app import db
        from app.models import BotGroup, StartMessage
        from app.modules.core.routes import _load_start_message_payload

        with flask_app.app_context():
            group = BotGroup(
                chat_id='-10022', title='g2', type='supergroup', is_active=True,
                config=json.dumps({'start_msg_open': False}),
            )
            db.session.add(group)
            db.session.flush()
            db.session.add(StartMessage(
                group_id=group.id, message_type='admin', content='不该发出', is_active=True
            ))
            db.session.commit()
            assert _load_start_message_payload(None, 'admin') is None


class TestCmdStartKeepsLoginVerification:
    def test_admin_branch_still_sends_verify_link_and_custom_message(self):
        import inspect
        from app.modules.core.routes import cmd_start

        src = inspect.getsource(cmd_start)
        assert '欢迎，管理员' in src
        assert 'auth_verify' in src
        assert '_load_start_message_payload(clone_id, \'admin\')' in src
        # Custom content is sent in addition to, not instead of, the login message
        assert src.index('欢迎，管理员') < src.index('_send_start_message_payload')
