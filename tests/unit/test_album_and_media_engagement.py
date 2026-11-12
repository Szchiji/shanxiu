"""Pure-media engagement: album dedupe and points/lottery counting."""

import uuid

import pytest

from app.services.album_dedupe import (
    album_should_count,
    caption_or_empty,
    should_skip_text_path_duplicate,
)
from app.services.group_engagement import award_speech_points, increment_lottery_message_counts


class TestAlbumShouldCount:
    def test_single_media_always_counts(self):
        cache = {}
        assert album_should_count(cache, -100, None) is True
        assert album_should_count(cache, -100, None) is True
        assert album_should_count(cache, -100, '') is True

    def test_album_counts_once(self):
        cache = {}
        assert album_should_count(cache, -100, 'abc', now=1.0) is True
        assert album_should_count(cache, -100, 'abc', now=1.5) is False
        assert album_should_count(cache, -100, 'abc', now=2.0) is False

    def test_different_albums_and_chats_are_independent(self):
        cache = {}
        assert album_should_count(cache, -100, 'a', now=1.0) is True
        assert album_should_count(cache, -100, 'b', now=1.0) is True
        assert album_should_count(cache, -200, 'a', now=1.0) is True

    def test_expired_album_can_count_again(self):
        cache = {}
        assert album_should_count(cache, -100, 'x', now=1.0, ttl=10) is True
        assert album_should_count(cache, -100, 'x', now=12.0, ttl=10) is True


class _Msg:
    def __init__(self, text=None, caption=None):
        self.text = text
        self.caption = caption


class TestCaptionGuards:
    def test_skip_when_message_has_text(self):
        assert should_skip_text_path_duplicate(_Msg(text='hello')) is True
        assert should_skip_text_path_duplicate(_Msg(text=None, caption='打卡')) is False
        assert should_skip_text_path_duplicate(_Msg()) is False

    def test_caption_or_empty(self):
        assert caption_or_empty(_Msg(caption='  hi ')) == 'hi'
        assert caption_or_empty(_Msg()) == ''
        # Pure media has no caption — must not look like a check-in trigger.
        assert caption_or_empty(_Msg(caption=None)) == ''


@pytest.mark.usefixtures('db')
class TestSpeechEngagement:
    def _setup(self, db):
        from app.models import BotGroup, PointsRule, GroupLottery
        from datetime import datetime, timedelta
        from app.utils import get_beijing_now
        g = BotGroup(
            chat_id=f'-eng-{uuid.uuid4().hex[:10]}',
            title='eng',
            type='supergroup',
            is_active=True,
        )
        db.session.add(g)
        db.session.commit()
        db.session.add(PointsRule(
            group_id=g.id, rule_name='发言', rule_type='message',
            points_amount=3, is_active=True,
        ))
        now = get_beijing_now()
        lot = GroupLottery(
            group_id=g.id,
            lottery_name='计票',
            lottery_type='message_count',
            min_messages=1,
            status='active',
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1),
        )
        db.session.add(lot)
        db.session.commit()
        return g, lot

    def test_media_awards_points_and_lottery_once(self, db):
        from app.models import LotteryMessageCount, UserPoints
        g, lot = self._setup(db)
        award_speech_points(g.id, 42)
        increment_lottery_message_counts(g.id, 42)
        # second album part must not be awarded if the caller uses album_should_count;
        # the helpers themselves increment when invoked — caller is responsible.
        # Simulate correct caller: only first part invokes helpers.
        pts = UserPoints.query.filter_by(group_id=g.id, user_id=42).first()
        assert pts is not None
        assert pts.points_balance == 3
        row = LotteryMessageCount.query.filter_by(lottery_id=lot.id, user_id=42).first()
        assert row.message_count == 1

        # Invoking again (as a second independent message) increments once more.
        award_speech_points(g.id, 42)
        increment_lottery_message_counts(g.id, 42)
        db.session.refresh(pts)
        db.session.refresh(row)
        assert pts.points_balance == 6
        assert row.message_count == 2
