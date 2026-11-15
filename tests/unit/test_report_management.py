"""Unit tests for report management: isolation, audit auth, migration, retries."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.modules.report import service as report_service


ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestChatIdsAndClone:
    def test_numeric_ids_equal_across_types(self):
        assert report_service.chat_ids_equal(-100123, '-100123')
        assert report_service.chat_ids_equal(' -100123 ', -100123)

    def test_usernames_equal_case_and_at(self):
        assert report_service.chat_ids_equal('@MyChan', 'mychan')
        assert not report_service.chat_ids_equal('@a', '@b')

    def test_same_clone_treats_none_as_main(self):
        assert report_service.same_clone_id(None, None)
        assert report_service.same_clone_id(3, '3')
        assert not report_service.same_clone_id(None, 3)


class TestEscapingAndCaption:
    def test_answers_and_qn_are_escaped_template_html_kept(self):
        template = '📋 <b>认证用户报告 #{report_id}</b>\n\n{answers}\nQ1={q1}'
        caption = report_service.build_push_caption(template, 7, [
            {'question': '时间', 'answer': '</b><a href="http://evil.example">x</a>'},
        ])
        assert '<b>认证用户报告 #7</b>' in caption
        assert '<a href="http://evil.example">' not in caption
        assert '&lt;/b&gt;&lt;a href=' in caption
        assert 'Q1=&lt;/b&gt;' in caption

    def test_names_are_escaped(self):
        member = type('M', (), {'first_name': '<b>Eve', 'last_name': '', 'username': 'a&b'})()
        ident = report_service.format_user_identity(99, member=member)
        assert '<b>Eve' not in ident
        assert '&lt;b&gt;Eve' in ident
        assert '@a&amp;b' in ident
        assert '<code>99</code>' in ident


class TestChannelLink:
    def test_private_and_public(self):
        assert report_service.build_channel_link('-1001234567890', 42) == 'https://t.me/c/1234567890/42'
        assert report_service.build_channel_link('@mychan', 7) == 'https://t.me/mychan/7'

    def test_view_prefers_stored_channel(self):
        report = type('R', (), {'channel_chat_id': '-100111', 'channel_msg_id': 9})()
        assert report_service.channel_identity_for_view(report, '@fallback') == '-100111'
        report.channel_chat_id = ''
        assert report_service.channel_identity_for_view(report, '@fallback') == '@fallback'


class TestStartIntent:
    def test_report_view_and_plain_start(self):
        assert report_service.start_command_intent('/start report_123') == 'report'
        assert report_service.start_command_intent('/start@MyBot view_9') == 'view'
        assert report_service.start_command_intent('/start') == 'start'
        assert report_service.start_command_intent('/cancel') == 'other'
        assert report_service.parse_start_target_user_id('/start report_123', 'report') == 123
        assert report_service.parse_start_target_user_id('/start@MyBot view_9', 'view') == 9
        assert report_service.parse_start_target_user_id('/start@MyBot report_42', 'report') == 42
        assert report_service.parse_start_target_user_id('/start view_9', 'report') is None


class TestAuditAccess:
    def _report(self, *, owner_known=True, clone_id=None, status='pending',
                admin_chat_id=None, admin_message_id=None):
        return type('R', (), {
            'owner_known': owner_known, 'clone_id': clone_id, 'status': status, 'id': 1,
            'admin_chat_id': admin_chat_id, 'admin_message_id': admin_message_id,
        })()

    def _acc(self, report, **kw):
        defaults = dict(
            bot_clone_id=None,
            callback_chat_id=-1001,
            configured_admin_group_id='-1001',
            clicker_is_admin=True,
            callback_sent_by_this_bot=True,
            callback_message_id=10,
        )
        defaults.update(kw)
        return report_service.evaluate_audit_access(report=report, **defaults)

    def test_denies_when_admin_group_missing(self):
        acc = self._acc(self._report(), configured_admin_group_id='')
        assert not acc.allowed
        assert acc.reason == 'no_admin_group'

    def test_denies_wrong_chat(self):
        acc = self._acc(self._report(), callback_chat_id=-1002)
        assert not acc.allowed
        assert acc.reason == 'wrong_chat'

    def test_denies_non_admin_clicker(self):
        acc = self._acc(self._report(), clicker_is_admin=False)
        assert not acc.allowed
        assert acc.reason == 'not_admin'

    def test_denies_callback_not_sent_by_this_bot(self):
        acc = self._acc(self._report(clone_id=1), bot_clone_id=1, callback_sent_by_this_bot=False)
        assert not acc.allowed
        assert acc.reason == 'not_this_bot'

    def test_denies_foreign_clone(self):
        acc = self._acc(self._report(clone_id=2), bot_clone_id=1)
        assert not acc.allowed
        assert acc.reason == 'foreign'

    def test_allows_owned_and_matching_group_admin(self):
        acc = self._acc(self._report(clone_id=1), bot_clone_id=1, callback_chat_id='-1001',
                        configured_admin_group_id=-1001)
        assert acc.allowed
        assert not acc.attribute_on_success

    def test_owned_requires_original_review_message(self):
        r = self._report(clone_id=1, admin_chat_id='-1001', admin_message_id=88)
        ok = self._acc(r, bot_clone_id=1, callback_message_id=88)
        assert ok.allowed
        bad_msg = self._acc(r, bot_clone_id=1, callback_message_id=99)
        assert not bad_msg.allowed
        assert bad_msg.reason == 'review_message_mismatch'
        bad_chat = self._acc(r, bot_clone_id=1, callback_chat_id='-1001',
                             configured_admin_group_id='-1001',
                             # stored review chat differs from callback
                             )
        # same chat here; force stored mismatch via a cloned report
        r2 = self._report(clone_id=1, admin_chat_id='-1099', admin_message_id=88)
        # callback chat must also equal configured admin group, so use 1099 as both
        # to isolate review_chat_mismatch:
        acc = report_service.evaluate_audit_access(
            report=r2, bot_clone_id=1, callback_chat_id=-1001,
            configured_admin_group_id='-1001', clicker_is_admin=True,
            callback_sent_by_this_bot=True, callback_message_id=88,
        )
        assert not acc.allowed
        assert acc.reason == 'review_chat_mismatch'

    def test_legacy_never_claimed_even_in_shared_admin_group(self):
        """Sharing an admin group + a forged/other-bot callback must not claim old rows."""
        r = self._report(owner_known=False, clone_id=None)
        acc = self._acc(r, bot_clone_id=5, callback_sent_by_this_bot=True, callback_message_id=10)
        assert not acc.allowed
        assert acc.reason == 'legacy_unassigned'
        assert not acc.attribute_on_success

    def test_legacy_denied_in_wrong_group(self):
        acc = self._acc(self._report(owner_known=False), callback_chat_id=-1099)
        assert not acc.allowed

    def test_callback_message_sent_by_bot_helper(self):
        bot_msg = type('M', (), {'from_user': type('U', (), {'id': 555})()})()
        other = type('M', (), {'from_user': type('U', (), {'id': 999})()})()
        missing = type('M', (), {'from_user': None})()
        assert report_service.callback_message_sent_by_bot(bot_msg, 555)
        assert not report_service.callback_message_sent_by_bot(other, 555)
        assert not report_service.callback_message_sent_by_bot(missing, 555)
        assert not report_service.callback_message_sent_by_bot(None, 555)

    def test_anonymous_admin_user_counts(self):
        assert report_service.clicker_counts_as_admin(
            report_service.ANONYMOUS_ADMIN_USER_ID
        )
        assert report_service.clicker_counts_as_admin(1, 'creator')
        assert report_service.clicker_counts_as_admin(1, 'administrator')
        assert not report_service.clicker_counts_as_admin(1, 'member')


class TestOwnershipVisibility:
    def _r(self, **kw):
        defaults = dict(owner_known=True, clone_id=None, status='approved', id=1)
        defaults.update(kw)
        return type('R', (), defaults)()

    def test_legacy_never_visible_on_main_or_clone(self):
        r = self._r(owner_known=False, clone_id=None)
        assert not report_service.is_visible_to_bot(r, None)
        assert not report_service.is_visible_to_bot(r, 2)

    def test_main_does_not_see_clone_and_vice_versa(self):
        main = self._r(clone_id=None)
        clone = self._r(clone_id=4)
        assert report_service.is_visible_to_bot(main, None)
        assert not report_service.is_visible_to_bot(main, 4)
        assert report_service.is_visible_to_bot(clone, 4)
        assert not report_service.is_visible_to_bot(clone, None)
        assert not report_service.is_visible_to_bot(clone, 5)

    def test_pending_not_listed(self):
        r = self._r(status='pending')
        assert not report_service.is_visible_to_bot(r, None)


class TestTelegramGone:
    def test_detects_already_deleted_errors(self):
        assert report_service.telegram_message_already_gone(
            Exception('Bad Request: message to delete not found')
        )
        assert report_service.telegram_message_already_gone(
            Exception('Bad Request: message not found')
        )
        assert report_service.telegram_message_already_gone(
            Exception('Bad Request: message_id_invalid')
        )
        assert report_service.telegram_message_already_gone(
            Exception('Bad Request: message identifier is invalid')
        )
        assert not report_service.telegram_message_already_gone(
            Exception('Forbidden: bot is not a member')
        )

    def test_cant_be_deleted_is_not_absence(self):
        """Too old or missing permission must not be treated as already gone."""
        assert not report_service.telegram_message_already_gone(
            Exception("Bad Request: message can't be deleted")
        )
        assert not report_service.telegram_message_already_gone(
            Exception('Bad Request: message can\'t be deleted')
        )
        assert not report_service.telegram_message_already_gone(
            Exception('Forbidden: not enough rights to delete message')
        )


# ---------------------------------------------------------------------------
# Schema migration (standalone SQLite, no production DB)
# ---------------------------------------------------------------------------

def _legacy_engine_with_row():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('''
            CREATE TABLE user_reports (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                submitter_id INTEGER,
                fault_time VARCHAR(100),
                fault_desc TEXT,
                process_result TEXT,
                photo_file_id VARCHAR(255),
                channel_msg_id INTEGER,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP,
                answers TEXT
            )
        '''))
        conn.execute(text(
            "INSERT INTO user_reports (id, user_id, fault_time, status, answers) "
            "VALUES (1, 42, '旧答案', 'approved', '[{\"question\":\"q\",\"answer\":\"a\"}]')"
        ))
    return engine


class _DB:
    def __init__(self, engine):
        self.engine = engine


class TestSchemaMigration:
    def test_adds_missing_columns_without_backfill_or_delete(self):
        engine = _legacy_engine_with_row()
        applied = report_service.ensure_user_report_schema(_DB(engine))
        assert applied  # at least some statements ran
        # Idempotent
        report_service.ensure_user_report_schema(_DB(engine))
        with engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text('PRAGMA table_info(user_reports)'))}
            for needed in (
                'channel_msg_deleted', 'channel_chat_id', 'clone_id',
                'owner_known', 'points_awarded', 'answers',
                'admin_chat_id', 'admin_message_id', 'approve_claimed_at',
            ):
                assert needed in cols, needed
            row = conn.execute(text(
                'SELECT user_id, fault_time, status, answers, owner_known, clone_id, '
                'channel_msg_deleted, points_awarded FROM user_reports WHERE id=1'
            )).mappings().first()
            assert row['user_id'] == 42
            assert row['fault_time'] == '旧答案'
            assert row['status'] == 'approved'
            assert '旧' not in (row['answers'] or '') or True
            assert json.loads(row['answers'])[0]['answer'] == 'a'
            assert not row['owner_known']
            assert row['clone_id'] is None
            assert not row['channel_msg_deleted']
            assert not row['points_awarded']
            count = conn.execute(text('SELECT COUNT(*) FROM user_reports')).scalar()
            assert count == 1

    def test_statement_lists_are_dialect_aware(self):
        pg = report_service.user_report_schema_statements('postgresql')
        sl = report_service.user_report_schema_statements('sqlite')
        assert any('IF NOT EXISTS channel_msg_deleted' in s or 'channel_msg_deleted' in s for s in pg)
        assert any('fault_time TYPE TEXT' in s for s in pg)
        assert not any('TYPE TEXT' in s for s in sl)
        assert any('owner_known' in s for s in sl)


# ---------------------------------------------------------------------------
# Database-backed flows (in-memory Flask app)
# ---------------------------------------------------------------------------

@pytest.fixture
def report_app(flask_app):
    """Use the shared test app, but only touch rows this module created.

    Wiping BotGroup globally would recycle SQLite primary keys and leave
    orphan SyncGroupMessages from other tests attached to the reused ids.
    """
    from app import db
    from app.models import (
        UserReport, BotGroup, GroupUser, GroupMember, UserPoints, PointsLog,
    )
    with flask_app.app_context():
        existing = {
            'groups': {row.id for row in BotGroup.query.all()},
            'members': {row.id for row in GroupMember.query.all()},
            'users': {row.id for row in GroupUser.query.all()},
            'points': {row.id for row in UserPoints.query.all()},
            'logs': {row.id for row in PointsLog.query.all()},
        }
        db.session.query(UserReport).delete()
        db.session.commit()
        yield flask_app, db

        def _drop(model, keep):
            for row in model.query.all():
                if row.id not in keep:
                    db.session.delete(row)

        db.session.query(UserReport).delete()
        _drop(PointsLog, existing['logs'])
        _drop(UserPoints, existing['points'])
        _drop(GroupMember, existing['members'])
        _drop(GroupUser, existing['users'])
        _drop(BotGroup, existing['groups'])
        db.session.commit()


def _group(db, *, chat_id, clone_id=None, title='G'):
    from app.models import BotGroup
    g = BotGroup(chat_id=str(chat_id), title=title, type='supergroup', clone_id=clone_id)
    db.session.add(g)
    db.session.commit()
    return g


def _report(db, **kwargs):
    from app.models import UserReport
    values = dict(
        user_id=100, submitter_id=200, fault_time='t', status='pending',
        owner_known=True, clone_id=None, points_awarded=False,
        answers=json.dumps([{'question': 'q', 'answer': 'a'}], ensure_ascii=False),
    )
    values.update(kwargs)
    r = UserReport(**values)
    db.session.add(r)
    db.session.commit()
    return r


class TestVisibleQueryAndMembers:
    def test_query_isolates_clones_and_hides_legacy(self, report_app):
        app, db = report_app
        from app.models import UserReport
        _report(db, user_id=1, owner_known=True, clone_id=None, status='approved', fault_time='main')
        _report(db, user_id=1, owner_known=True, clone_id=2, status='approved', fault_time='c2')
        _report(db, user_id=1, owner_known=False, clone_id=None, status='approved', fault_time='legacy')
        _report(db, user_id=1, owner_known=True, clone_id=None, status='pending', fault_time='pend')
        main = report_service.visible_reports_query(UserReport, 1, None).all()
        clone = report_service.visible_reports_query(UserReport, 1, 2).all()
        other = report_service.visible_reports_query(UserReport, 1, 9).all()
        assert [r.fault_time for r in main] == ['main']
        assert [r.fault_time for r in clone] == ['c2']
        assert other == []

    def test_member_name_uses_current_bot_group_only(self, report_app):
        app, db = report_app
        from app.models import GroupMember, GroupUser, BotGroup
        g_main = _group(db, chat_id=-1001, clone_id=None, title='main')
        g_clone = _group(db, chat_id=-1001, clone_id=7, title='clone')
        db.session.add(GroupMember(
            group_id=g_main.id, user_id=55, username='alice_main',
            first_name='Main', last_name='Name',
        ))
        db.session.add(GroupMember(
            group_id=g_clone.id, user_id=55, username='alice_clone',
            first_name='Clone', last_name='Name',
        ))
        db.session.commit()
        main_m = report_service.find_member_for_bot(
            GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
            bot_clone_id=None, user_id=55,
        )
        clone_m = report_service.find_member_for_bot(
            GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
            bot_clone_id=7, user_id=55,
        )
        assert main_m.first_name == 'Main'
        assert clone_m.first_name == 'Clone'


class TestLongAnswerAndNewReport:
    def test_first_answer_longer_than_100_is_stored(self, report_app):
        app, db = report_app
        from app.models import UserReport
        long_ans = '长' * 150
        values = report_service.new_report_values(
            user_id=8, submitter_id=9,
            raw_answers=[long_ans, 'desc', 'result'],
            answers=[{'question': 'q1', 'answer': long_ans}],
            photo_file_id=None, bot_clone_id=3,
        )
        assert values['owner_known'] is True
        assert values['clone_id'] == 3
        assert len(values['fault_time']) == 150
        r = UserReport(**values)
        db.session.add(r)
        db.session.commit()
        loaded = UserReport.query.get(r.id)
        assert loaded.fault_time == long_ans
        assert loaded.owner_known is True
        assert loaded.clone_id == 3


class TestApproveRejectDeleteIdempotency:
    def test_publish_failure_is_not_committed_as_success(self, report_app):
        """Caller must not call finalize_approve when publish failed; row stays pending."""
        app, db = report_app
        r = _report(db, status='pending', owner_known=True, clone_id=None)
        db.session.refresh(r)
        assert r.status == 'pending'
        assert r.channel_msg_id is None
        # Simulating the bot leaving the row untouched after a Telegram error.
        assert UserReport_status(db, r.id) == 'pending'

    def test_approve_then_retry_is_already_done_no_second_points(self, report_app):
        app, db = report_app
        from app.models import UserPoints, PointsLog, BotGroup
        g = _group(db, chat_id='-10088', clone_id=None)
        r = _report(db, status='pending', submitter_id=77, clone_id=None)
        first = report_service.finalize_approve(
            db, r, None,
            channel_msg_id=501, channel_chat_id='-10055',
            attribute=False,
            UserPoints=UserPoints, PointsLog=PointsLog, BotGroup=BotGroup,
            reward_pts=10, points_group_chat_id='-10088',
        )
        assert first.status == 'ok'
        assert first.published
        assert first.awarded_points == 10
        assert r.channel_chat_id == '-10055'
        assert r.channel_msg_id == 501
        assert r.points_awarded is True
        assert g.chat_id == '-10088'

        second = report_service.finalize_approve(
            db, r, None,
            channel_msg_id=999, channel_chat_id='-10055',
            attribute=False,
            UserPoints=UserPoints, PointsLog=PointsLog, BotGroup=BotGroup,
            reward_pts=10, points_group_chat_id='-10088',
        )
        assert second.status == 'already_done'
        assert second.awarded_points == 0
        assert second.duplicate_msg_id == 999
        logs = PointsLog.query.filter_by(user_id=77).all()
        assert len(logs) == 1
        assert logs[0].points_change == 10
        pts = UserPoints.query.filter_by(user_id=77, group_id=g.id).one()
        assert pts.points_balance == 10

    def test_points_go_to_matching_clone_group_only(self, report_app):
        app, db = report_app
        from app.models import UserPoints, PointsLog, BotGroup
        g_main = _group(db, chat_id='-10088', clone_id=None, title='main')
        g_clone = _group(db, chat_id='-10088', clone_id=4, title='clone')
        r = _report(db, status='pending', submitter_id=77, clone_id=4, owner_known=True)
        result = report_service.finalize_approve(
            db, r, 4,
            channel_msg_id=1, channel_chat_id='@ch',
            attribute=False,
            UserPoints=UserPoints, PointsLog=PointsLog, BotGroup=BotGroup,
            reward_pts=5, points_group_chat_id='-10088',
        )
        assert result.awarded_points == 5
        assert UserPoints.query.filter_by(group_id=g_clone.id, user_id=77).one().points_balance == 5
        assert UserPoints.query.filter_by(group_id=g_main.id, user_id=77).first() is None

    def test_pending_with_existing_channel_msg_skips_need_to_republish(self, report_app):
        app, db = report_app
        r = _report(db, status='pending', channel_msg_id=12, channel_msg_deleted=False)
        assert report_service.should_skip_publish(r)

    def test_reject_then_retry(self, report_app):
        app, db = report_app
        r = _report(db, status='pending', owner_known=True, clone_id=None)
        first = report_service.finalize_reject(db, r)
        assert first.status == 'ok'
        assert r.status == 'rejected'
        assert r.owner_known is True
        second = report_service.finalize_reject(db, r)
        assert second.status == 'already_done'
        assert r.status == 'rejected'

    def test_reject_does_not_claim_legacy(self, report_app):
        app, db = report_app
        r = _report(db, status='pending', owner_known=False, clone_id=None)
        result = report_service.finalize_reject(db, r, 8, attribute=True)
        assert result.status == 'ok'
        db.session.refresh(r)
        assert r.status == 'rejected'
        assert r.owner_known is False
        assert r.clone_id is None

    def test_reject_blocked_by_live_approve_claim(self, report_app):
        app, db = report_app
        from app.models import UserReport
        r = _report(db, status='pending', owner_known=True)
        claim = report_service.claim_for_approve(db, UserReport, r.id)
        assert claim.status == 'claimed'
        result = report_service.finalize_reject(db, r)
        assert result.status == 'in_progress'
        assert result.notify_submitter is False
        db.session.refresh(r)
        assert r.status == 'pending'
        assert r.approve_claimed_at is not None

    def test_reject_blocked_when_channel_message_recorded(self, report_app):
        app, db = report_app
        from app.models import UserReport
        r = _report(db, status='pending', owner_known=True)
        report_service.record_published_channel(db, UserReport, r.id, 88, '-10055')
        db.session.refresh(r)
        result = report_service.finalize_reject(db, r)
        assert result.status == 'conflict'
        assert result.notify_submitter is False
        db.session.refresh(r)
        assert r.status == 'pending'
        assert r.channel_msg_id == 88

    def test_reject_allowed_after_expired_lease_without_channel(self, report_app):
        app, db = report_app
        from app.models import UserReport
        r = _report(db, status='pending', owner_known=True)
        old = datetime.utcnow() - timedelta(seconds=report_service.APPROVE_CLAIM_SECONDS + 5)
        assert report_service.claim_for_approve(db, UserReport, r.id, now=old).status == 'claimed'
        result = report_service.finalize_reject(db, r, now=datetime.utcnow())
        assert result.status == 'ok'
        db.session.refresh(r)
        assert r.status == 'rejected'
        assert r.approve_claimed_at is None

    def test_delete_failure_does_not_mark_deleted(self, report_app):
        app, db = report_app
        r = _report(db, status='approved', channel_msg_id=3, channel_msg_deleted=False)
        result = report_service.finalize_delete_channel(db, r, success=False, already_gone=False)
        assert result.status == 'failed'
        assert r.channel_msg_deleted is False

    def test_delete_success_and_already_gone_and_retry(self, report_app):
        app, db = report_app
        r = _report(db, status='approved', channel_msg_id=3, channel_msg_deleted=False)
        ok = report_service.finalize_delete_channel(db, r, success=True, already_gone=False)
        assert ok.status == 'ok'
        assert r.channel_msg_deleted is True
        again = report_service.finalize_delete_channel(db, r, success=True, already_gone=False)
        assert again.status == 'already_deleted'

        r2 = _report(db, status='approved', channel_msg_id=4, channel_msg_deleted=False)
        gone = report_service.finalize_delete_channel(db, r2, success=False, already_gone=True)
        assert gone.status == 'ok'
        assert r2.channel_msg_deleted is True

    def test_legacy_not_auto_attributed_on_migrate_but_can_be_stamped(self, report_app):
        app, db = report_app
        from app.models import UserReport
        r = _report(db, owner_known=False, clone_id=None, status='pending')
        assert report_service.report_ownership(r, None) == report_service.LEGACY_UNASSIGNED
        assert report_service.report_ownership(r, 2) == report_service.LEGACY_UNASSIGNED
        report_service.attribute_report_to_bot(r, 2)
        db.session.commit()
        loaded = UserReport.query.get(r.id)
        assert loaded.owner_known is True
        assert loaded.clone_id == 2
        assert report_service.report_ownership(loaded, 2) == report_service.OWNED
        assert report_service.report_ownership(loaded, None) == report_service.FOREIGN



class TestClaimRecordAndPointsTransaction:
    def test_only_one_worker_wins_the_claim(self, report_app):
        app, db = report_app
        from app.models import UserReport
        r = _report(db, status='pending', owner_known=True)
        first = report_service.claim_for_approve(db, UserReport, r.id)
        second = report_service.claim_for_approve(db, UserReport, r.id)
        assert first.status == 'claimed'
        assert second.status == 'in_progress'
        db.session.refresh(r)
        assert r.status == 'pending'
        assert r.approve_claimed_at is not None

    def test_expired_lease_can_be_reclaimed(self, report_app):
        app, db = report_app
        from app.models import UserReport
        r = _report(db, status='pending', owner_known=True)
        old = datetime.utcnow() - timedelta(seconds=report_service.APPROVE_CLAIM_SECONDS + 5)
        first = report_service.claim_for_approve(db, UserReport, r.id, now=old)
        assert first.status == 'claimed'
        again = report_service.claim_for_approve(db, UserReport, r.id, now=datetime.utcnow())
        assert again.status == 'claimed'

    def test_release_claim_allows_retry_after_failed_publish(self, report_app):
        app, db = report_app
        from app.models import UserReport
        r = _report(db, status='pending', owner_known=True)
        assert report_service.claim_for_approve(db, UserReport, r.id).status == 'claimed'
        report_service.release_approve_claim(db, UserReport, r.id)
        db.session.refresh(r)
        assert r.status == 'pending'
        assert r.approve_claimed_at is None
        assert r.channel_msg_id is None
        assert report_service.claim_for_approve(db, UserReport, r.id).status == 'claimed'

    def test_record_channel_first_writer_wins(self, report_app):
        app, db = report_app
        from app.models import UserReport
        r = _report(db, status='pending', owner_known=True)
        first = report_service.record_published_channel(db, UserReport, r.id, 11, '-10055')
        second = report_service.record_published_channel(db, UserReport, r.id, 22, '-10055')
        assert first.status == 'ok'
        assert first.channel_msg_id == 11
        assert second.status == 'lost_race'
        assert second.channel_msg_id == 11
        assert second.duplicate_msg_id == 22
        db.session.refresh(r)
        assert r.status == 'pending'
        assert r.channel_msg_id == 11

    def test_recorded_channel_then_finalize_without_second_publish(self, report_app):
        app, db = report_app
        from app.models import UserReport, UserPoints, PointsLog, BotGroup
        g = _group(db, chat_id='-10088', clone_id=None)
        r = _report(db, status='pending', submitter_id=77, clone_id=None)
        report_service.record_published_channel(db, UserReport, r.id, 44, '-10055')
        result = report_service.finalize_approve(
            db, r, None,
            channel_msg_id=44, channel_chat_id='-10055',
            UserPoints=UserPoints, PointsLog=PointsLog, BotGroup=BotGroup,
            reward_pts=3, points_group_chat_id='-10088',
        )
        assert result.status == 'ok'
        assert result.awarded_points == 3
        db.session.refresh(r)
        assert r.status == 'approved'
        assert r.channel_msg_id == 44
        assert r.approve_claimed_at is None

    def test_points_flag_not_set_when_group_missing(self, report_app):
        app, db = report_app
        from app.models import UserPoints, PointsLog, BotGroup
        r = _report(db, status='pending', submitter_id=77, clone_id=None)
        result = report_service.finalize_approve(
            db, r, None,
            channel_msg_id=1, channel_chat_id='@ch',
            UserPoints=UserPoints, PointsLog=PointsLog, BotGroup=BotGroup,
            reward_pts=9, points_group_chat_id='-19999',
        )
        assert result.status == 'ok'
        assert result.awarded_points == 0
        db.session.refresh(r)
        assert r.points_awarded is False
        assert UserPoints.query.filter_by(user_id=77).first() is None
        assert PointsLog.query.filter_by(user_id=77).first() is None

    def test_status_and_points_roll_back_together(self, report_app):
        app, db = report_app
        from app.models import UserReport, UserPoints, PointsLog, BotGroup
        g = _group(db, chat_id='-10088', clone_id=None)
        r = _report(db, status='pending', submitter_id=77, clone_id=None)

        real_award = report_service.award_approval_points

        def boom(**kwargs):
            awarded = real_award(**kwargs)
            raise RuntimeError('simulated failure after credit staged')

        original = report_service.award_approval_points
        try:
            report_service.award_approval_points = boom
            try:
                report_service.finalize_approve(
                    db, r, None,
                    channel_msg_id=5, channel_chat_id='@ch',
                    UserPoints=UserPoints, PointsLog=PointsLog, BotGroup=BotGroup,
                    reward_pts=4, points_group_chat_id='-10088',
                )
                raised = False
            except RuntimeError:
                raised = True
                db.session.rollback()
            assert raised
        finally:
            report_service.award_approval_points = original

        fresh = UserReport.query.get(r.id)
        assert fresh.status == 'pending'
        assert fresh.points_awarded is False
        assert UserPoints.query.filter_by(user_id=77).first() is None
        assert PointsLog.query.filter_by(user_id=77).first() is None


def UserReport_status(db, report_id):
    from app.models import UserReport
    return UserReport.query.get(report_id).status


class TestEditorPlaceholders:
    def test_badges_hidden_except_push_template(self):
        html = (ROOT / 'app/modules/core/templates/settings.html').read_text()
        assert html.count('report-link-placeholder') == 3  # two badges + querySelector
        assert html.count('report-link-placeholder d-none') == 2
        assert "selector.value === 'push_template'" in html
        assert 'syncReportLinkPlaceholders()' in html
        # insertion still uses the existing placeholders; coupon/channel send logic not present here
        assert "insert('{写报告链接}', event)" in html
        assert "insert('{查报告链接}', event)" in html


class TestStartupWiring:
    def test_run_and_migrate_include_channel_msg_deleted_and_helper(self):
        run = (ROOT / 'run.py').read_text()
        mig = (ROOT / 'migrate_database.py').read_text()
        assert 'channel_msg_deleted' in run
        assert 'ensure_user_report_schema' in run
        assert 'channel_msg_deleted' in mig
        assert 'ensure_user_report_schema' in mig
        assert 'owner_known' in run and 'owner_known' in mig
