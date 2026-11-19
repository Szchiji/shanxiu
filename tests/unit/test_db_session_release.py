"""Regression tests for SQLAlchemy session / connection pool release.

Production 2026-11-19: 15 connections stayed ``idle in transaction`` (14 last
queried group_bottom_button, 1 user_points) while bot handlers kept an app
context open across Telegram awaits, exhausting the 5+10 pool.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db_session import db_app_context, release_idle_db_session
from app.models import BotGroup, GroupBottomButton, UserPoints


_chat_seq = 0


def _make_group(db, title='leak-test-group'):
    global _chat_seq
    _chat_seq += 1
    group = BotGroup(
        chat_id=f'-1001999{_chat_seq:03d}',
        title=title,
        type='supergroup',
        is_active=True,
    )
    db.session.add(group)
    db.session.commit()
    return group


def _in_transaction(session):
    return session().in_transaction()


def test_group_bottom_button_read_releases_transaction(flask_app):
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        db.session.add(GroupBottomButton(
            group_id=group.id, button_text='菜单', button_url='https://example.com', is_active=True
        ))
        db.session.commit()

        rows = GroupBottomButton.query.filter_by(group_id=group.id, is_active=True).all()
        assert len(rows) == 1
        assert rows[0].button_text == '菜单'
        # Context is still open, as in on_message before an await, but the
        # read transaction must already have been released.
        assert not _in_transaction(db.session)
        assert not db.session.new and not db.session.dirty


def test_user_points_read_releases_transaction(flask_app):
    from app import db

    with flask_app.app_context():
        group = _make_group(db, title='points-group')
        db.session.add(UserPoints(group_id=group.id, user_id=123456, points_balance=7))
        db.session.commit()

        row = UserPoints.query.filter_by(group_id=group.id, user_id=123456).first()
        assert row.points_balance == 7
        assert not _in_transaction(db.session)


def test_dirty_session_is_not_auto_committed(flask_app):
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        pending = GroupBottomButton(
            group_id=group.id, button_text='未提交', is_active=True
        )
        db.session.add(pending)
        # A subsequent read must not commit the pending write (even though
        # autoflush may have moved it out of session.new).
        BotGroup.query.filter_by(id=group.id).first()
        assert _in_transaction(db.session)
        db.session.rollback()

    with flask_app.app_context():
        assert GroupBottomButton.query.filter_by(button_text='未提交').first() is None


def test_followup_write_after_released_read_still_commits(flask_app):
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        db.session.add(GroupBottomButton(group_id=group.id, button_text='旧', is_active=True))
        db.session.commit()

        button = GroupBottomButton.query.filter_by(group_id=group.id).first()
        assert not _in_transaction(db.session)
        button.button_text = '新'
        db.session.commit()
        group_id = group.id

    with flask_app.app_context():
        assert GroupBottomButton.query.filter_by(group_id=group_id).one().button_text == '新'


def test_with_for_update_keeps_transaction(flask_app):
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        db.session.add(UserPoints(group_id=group.id, user_id=1, points_balance=1))
        db.session.commit()

        locked = UserPoints.query.filter_by(group_id=group.id, user_id=1).with_for_update().first()
        assert locked is not None
        assert _in_transaction(db.session)
        db.session.rollback()


def test_db_app_context_releases_on_exception(flask_app):
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        db.session.add(GroupBottomButton(group_id=group.id, button_text='x', is_active=True))
        db.session.commit()

    with pytest.raises(RuntimeError, match='boom'):
        with db_app_context(flask_app) as session:
            session.query(GroupBottomButton).all()
            raise RuntimeError('boom')

    # A new context can check out a session again (no leaked checkout).
    with flask_app.app_context():
        assert GroupBottomButton.query.count() >= 1
        assert not _in_transaction(db.session)


@pytest.mark.asyncio
async def test_concurrent_handlers_release_before_await(flask_app):
    """Simulate many on_message tasks querying then awaiting I/O."""
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        db.session.add(GroupBottomButton(group_id=group.id, button_text='并', is_active=True))
        db.session.add(UserPoints(group_id=group.id, user_id=99, points_balance=3))
        db.session.commit()
        group_id = group.id

    results = []

    async def handler(kind):
        with flask_app.app_context():
            if kind == 'button':
                rows = GroupBottomButton.query.filter_by(group_id=group_id).all()
                assert rows and rows[0].button_text == '并'
            else:
                row = UserPoints.query.filter_by(group_id=group_id, user_id=99).first()
                assert row.points_balance == 3
            still_open = _in_transaction(db.session)
            await asyncio.sleep(0.05)
            results.append(still_open)

    await asyncio.gather(*[handler('button' if i % 2 == 0 else 'points') for i in range(20)])
    assert results == [False] * 20


def test_release_idle_helper_is_noop_when_dirty(flask_app):
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        db.session.add(GroupBottomButton(group_id=group.id, button_text='dirty', is_active=True))
        assert release_idle_db_session(db.session) is False
        assert _in_transaction(db.session)
        db.session.rollback()


def test_existing_transaction_is_not_auto_committed(flask_app):
    """flush() + later SELECT must not commit staged writes."""
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        pending = GroupBottomButton(group_id=group.id, button_text='staged', is_active=True)
        db.session.add(pending)
        db.session.flush()
        assert pending.id is not None
        assert _in_transaction(db.session)
        BotGroup.query.filter_by(id=group.id).first()
        assert _in_transaction(db.session)
        db.session.rollback()

    with flask_app.app_context():
        assert GroupBottomButton.query.filter_by(button_text='staged').first() is None


def test_bulk_update_is_not_auto_committed(flask_app):
    """Query.update must stay uncommitted until the caller commits."""
    from app import db

    with flask_app.app_context():
        group = _make_group(db)
        db.session.add(GroupBottomButton(group_id=group.id, button_text='旧文案', is_active=True))
        db.session.commit()
        updated = GroupBottomButton.query.filter_by(group_id=group.id).update(
            {'button_text': '新文案'},
            synchronize_session=False,
        )
        assert updated == 1
        assert _in_transaction(db.session)
        db.session.rollback()
        group_id = group.id

    with flask_app.app_context():
        assert GroupBottomButton.query.filter_by(group_id=group_id).one().button_text == '旧文案'
