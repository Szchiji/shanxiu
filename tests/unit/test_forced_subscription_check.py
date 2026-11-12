"""Forced-subscription scheduled job must not use detached ORM instances."""

import asyncio
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm.exc import DetachedInstanceError

from app.services.forced_subscription_check import (
    collect_due_forced_subscriptions,
    snapshot_forced_subscription,
)


NOW = datetime(2026, 11, 12, 18, 40, 0)


def _run(coro):
    # Match other scheduler tests: reuse the existing loop so later
    # get_event_loop().run_until_complete() tests still work.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError('closed')
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _unique_chat_id():
    return f'-100{uuid.uuid4().int % 10**10:010d}'


def _add_group(db, **kw):
    from app.models import BotGroup
    fields = dict(
        chat_id=_unique_chat_id(),
        title='forced-sub-group',
        type='supergroup',
        is_active=True,
    )
    fields.update(kw)
    group = BotGroup(**fields)
    db.session.add(group)
    db.session.commit()
    return group


def _add_sub(db, group, **kw):
    from app.models import ForcedChannelSubscription
    fields = dict(
        group_id=group.id,
        enabled=True,
        channel_id='-100999001',
        check_interval=3600,
        unsubscribe_action='mute',
        reward_points=5,
        last_checked_at=None,
    )
    fields.update(kw)
    row = ForcedChannelSubscription(**fields)
    db.session.add(row)
    db.session.commit()
    return row


class TestSnapshotSurvivesSessionClose:
    def test_leaked_orm_instances_raise_after_context_exit(self, flask_app):
        from app import db
        from app.models import ForcedChannelSubscription

        with flask_app.app_context():
            group = _add_group(db)
            _add_sub(db, group, channel_id='-100111', last_checked_at=None)
            rows = ForcedChannelSubscription.query.filter_by(
                group_id=group.id, enabled=True
            ).all()
            for settings in rows:
                settings.last_checked_at = NOW
            db.session.commit()
            leaked = rows

        with pytest.raises(DetachedInstanceError):
            _ = leaked[0].channel_id

    def test_snapshot_is_usable_after_commit_and_context_exit(self, flask_app):
        from app import db
        from app.models import ForcedChannelSubscription

        with flask_app.app_context():
            group = _add_group(db)
            row = _add_sub(
                db, group,
                channel_id='-100111',
                unsubscribe_action='kick',
                reward_points=8,
            )
            snap = snapshot_forced_subscription(row)
            row_id = row.id
            group_id = group.id
            row.last_checked_at = NOW
            db.session.commit()
            db.session.expunge(row)

        assert snap == {
            'id': row_id,
            'group_id': group_id,
            'channel_id': '-100111',
            'unsubscribe_action': 'kick',
            'reward_points': 8,
        }
        # Accessing the expunged ORM object fails; the snapshot does not.
        with pytest.raises(DetachedInstanceError):
            _ = row.channel_id
        assert snap['channel_id'] == '-100111'


class TestCollectDueForcedSubscriptions:
    def test_due_and_not_due_respect_per_group_interval(self, flask_app):
        from app import db
        from app.models import ForcedChannelSubscription

        with flask_app.app_context():
            ForcedChannelSubscription.query.update({'enabled': False})
            db.session.commit()

            due_group = _add_group(db)
            skip_group = _add_group(db)
            disabled_group = _add_group(db)
            first_run_group = _add_group(db)

            due_row = _add_sub(
                db, due_group,
                check_interval=3600,
                last_checked_at=NOW - timedelta(hours=2),
                channel_id='-100due',
            )
            skip_row = _add_sub(
                db, skip_group,
                check_interval=3600,
                last_checked_at=NOW - timedelta(minutes=30),
                channel_id='-100skip',
            )
            _add_sub(
                db, disabled_group,
                enabled=False,
                last_checked_at=None,
                channel_id='-100off',
            )
            first_row = _add_sub(
                db, first_run_group,
                check_interval=3600,
                last_checked_at=None,
                channel_id='-100first',
            )

            due = collect_due_forced_subscriptions(NOW)
            due_id = due_row.id
            skip_id = skip_row.id
            first_id = first_row.id
            due_group_id = due_group.id
            first_group_id = first_run_group.id

        ids = {item['id'] for item in due}
        channels = {item['channel_id'] for item in due}
        assert due_id in ids
        assert first_id in ids
        assert skip_id not in ids
        assert channels == {'-100due', '-100first'}
        assert all(isinstance(item, dict) for item in due)

        # Snapshots remain readable after the app context has closed.
        assert {item['group_id'] for item in due} == {due_group_id, first_group_id}

        with flask_app.app_context():
            refreshed_due = ForcedChannelSubscription.query.get(due_id)
            refreshed_skip = ForcedChannelSubscription.query.get(skip_id)
            refreshed_first = ForcedChannelSubscription.query.get(first_id)
            assert refreshed_due.last_checked_at == NOW
            assert refreshed_skip.last_checked_at == NOW - timedelta(minutes=30)
            assert refreshed_first.last_checked_at == NOW
            # Existing groups keep their configured interval.
            assert refreshed_due.check_interval == 3600
            assert refreshed_skip.check_interval == 3600

    def test_custom_interval_still_honored(self, flask_app):
        from app import db
        from app.models import ForcedChannelSubscription

        with flask_app.app_context():
            ForcedChannelSubscription.query.update({'enabled': False})
            db.session.commit()
            group = _add_group(db)
            row = _add_sub(
                db, group,
                check_interval=600,
                last_checked_at=NOW - timedelta(minutes=11),
                channel_id='-100custom',
            )
            due = collect_due_forced_subscriptions(NOW)
            assert [item['id'] for item in due] == [row.id]

            # Immediately collecting again at the same time should skip it.
            due_again = collect_due_forced_subscriptions(NOW)
            assert due_again == []


class TestScheduledJobDoesNotDetach:
    def test_job_checks_members_after_session_closes(self, flask_app, monkeypatch):
        from app import db
        from app.models import ForcedChannelSubscription, GroupUser
        from app.modules.core import routes

        monkeypatch.setattr(routes, 'global_flask_app', flask_app)

        with flask_app.app_context():
            ForcedChannelSubscription.query.update({'enabled': False})
            db.session.commit()
            group = _add_group(db, chat_id='-1001332469029')
            _add_sub(
                db, group,
                channel_id='-100888',
                unsubscribe_action='mute',
                last_checked_at=None,
                reward_points=0,
            )
            user = GroupUser(group_id=group.id, tg_id=555000111)
            db.session.add(user)
            db.session.commit()
            user_row_id = user.id
            group_id = group.id

        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status='left'))
        bot.restrict_chat_member = AsyncMock()
        bot.ban_chat_member = AsyncMock()
        bot.unban_chat_member = AsyncMock()
        monkeypatch.setattr(routes, '_get_bot_for_clone', lambda clone_id, fallback: bot)
        monkeypatch.setattr(routes, 'is_user_chat_owner', AsyncMock(return_value=False))

        _run(routes.check_channel_subscriptions(SimpleNamespace(bot=bot)))

        bot.get_chat_member.assert_awaited()
        assert bot.get_chat_member.await_args.args[0] == '-100888'
        assert bot.get_chat_member.await_args.args[1] == 555000111
        bot.restrict_chat_member.assert_awaited()

        with flask_app.app_context():
            sub = ForcedChannelSubscription.query.filter_by(group_id=group_id).first()
            assert sub.last_checked_at is not None
            refreshed_user = GroupUser.query.get(user_row_id)
            assert refreshed_user.is_channel_subscribed is False
