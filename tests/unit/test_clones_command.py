"""Main-bot /clones registration and access control."""

import inspect

from app.bot.handlers.commands import (
    clones_command_denial,
    public_clone_list_item,
)


class TestClonesAccess:
    def test_group_chat_denied(self):
        msg = clones_command_denial('supergroup', 1, 1)
        assert msg
        assert '私聊' in msg

    def test_non_admin_denied_without_secrets(self):
        msg = clones_command_denial('private', 99, 1)
        assert msg
        assert '管理员' in msg
        assert 'token' not in msg.lower()

    def test_admin_in_private_allowed(self):
        assert clones_command_denial('private', 42, 42) is None

    def test_zero_admin_id_denied(self):
        assert clones_command_denial('private', 0, 0)


class TestPublicCloneListItem:
    def test_omits_token_fields(self):
        class C:
            id = 3
            clone_name = 'demo'
            owner_user_id = 1
            is_active = True
            expiration_date = None
            description = 'x'
            bot_token = '123:SHOULD_NOT_APPEAR'
            get_bot_token = lambda self: '123:SHOULD_NOT_APPEAR'

        item = public_clone_list_item(C())
        dumped = str(item)
        assert '123:SHOULD_NOT_APPEAR' not in dumped
        assert 'bot_token' not in item
        assert item['id'] == 3
        assert item['clone_name'] == 'demo'
        assert item['expiration_date'] == '无期限'


class TestMainBotRegistersClones:
    def test_run_bot_registers_clones_and_unwarn(self):
        from app.modules.core import routes
        src = inspect.getsource(routes.run_bot)
        assert 'CommandHandler("clones"' in src
        assert 'CommandHandler("unwarn"' in src

    def test_clone_setup_also_registers_unwarn(self):
        from app.modules.core import routes
        src = inspect.getsource(routes.setup_clone_handlers)
        assert 'CommandHandler("clones"' in src
        assert 'CommandHandler("unwarn"' in src
