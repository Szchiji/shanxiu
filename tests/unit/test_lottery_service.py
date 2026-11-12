"""
tests/unit/test_lottery_service.py
------------------------------------
Unit tests for app.services.lottery_service.

These tests do not touch the database or the Telegram API.
"""

import pytest
from app.services.lottery_service import (
    pick_winners_random,
    pick_winner_weighted,
    pick_winners_top_n,
    validate_lottery_draw,
    run_lottery_draw,
)


# ---------------------------------------------------------------------------
# pick_winners_random
# ---------------------------------------------------------------------------

class TestPickWinnersRandom:
    def test_correct_count(self):
        pool = list(range(1, 101))
        winners = pick_winners_random(pool, 3)
        assert len(winners) == 3

    def test_no_duplicates(self):
        pool = list(range(1, 101))
        winners = pick_winners_random(pool, 10)
        assert len(winners) == len(set(winners))

    def test_count_exceeds_pool(self):
        pool = [1, 2]
        winners = pick_winners_random(pool, 5)
        assert len(winners) == 2

    def test_empty_pool_returns_empty(self):
        assert pick_winners_random([], 3) == []

    def test_zero_count_returns_empty(self):
        assert pick_winners_random([1, 2, 3], 0) == []

    def test_negative_count_returns_empty(self):
        assert pick_winners_random([1, 2, 3], -1) == []

    def test_winners_are_subset_of_pool(self):
        pool = [10, 20, 30, 40, 50]
        winners = pick_winners_random(pool, 3)
        assert all(w in pool for w in winners)

    def test_single_element_pool(self):
        assert pick_winners_random([42], 1) == [42]


# ---------------------------------------------------------------------------
# pick_winner_weighted
# ---------------------------------------------------------------------------

class TestPickWinnerWeighted:
    def test_returns_valid_user(self):
        participants = [(1, 5), (2, 10), (3, 1)]
        winner = pick_winner_weighted(participants)
        assert winner in (1, 2, 3)

    def test_empty_returns_none(self):
        assert pick_winner_weighted([]) is None

    def test_single_participant_always_wins(self):
        for _ in range(20):
            assert pick_winner_weighted([(99, 100)]) == 99

    def test_zero_weight_falls_back_to_uniform(self):
        participants = [(1, 0), (2, 0), (3, 0)]
        winner = pick_winner_weighted(participants)
        assert winner in (1, 2, 3)

    def test_dominant_weight_wins_most_often(self):
        """A participant with 99 % weight should win almost every time."""
        participants = [(1, 9900), (2, 100)]
        wins = {1: 0, 2: 0}
        for _ in range(1000):
            w = pick_winner_weighted(participants)
            wins[w] += 1
        assert wins[1] > 900, f"Dominant winner expected >900/1000 wins, got {wins[1]}"


# ---------------------------------------------------------------------------
# pick_winners_top_n
# ---------------------------------------------------------------------------

class TestPickWinnersTopN:
    def test_correct_count(self):
        participants = [(1, 10), (2, 30), (3, 20)]
        assert pick_winners_top_n(participants, 2) == [2, 3]

    def test_count_exceeds_participants(self):
        participants = [(1, 5), (2, 3)]
        result = pick_winners_top_n(participants, 10)
        assert set(result) == {1, 2}

    def test_empty_returns_empty(self):
        assert pick_winners_top_n([], 3) == []

    def test_zero_count_returns_empty(self):
        assert pick_winners_top_n([(1, 5)], 0) == []

    def test_tie_broken_by_user_id_ascending(self):
        participants = [(3, 10), (1, 10), (2, 10)]
        result = pick_winners_top_n(participants, 1)
        assert result == [1]

    def test_single_winner(self):
        participants = [(5, 100), (3, 50), (7, 200)]
        assert pick_winners_top_n(participants, 1) == [7]


# ---------------------------------------------------------------------------
# validate_lottery_draw
# ---------------------------------------------------------------------------

class TestValidateLotteryDraw:
    def test_valid_message_count(self):
        ok, _ = validate_lottery_draw('message_count', [(1, 10)])
        assert ok is True

    def test_valid_message_rank(self):
        ok, _ = validate_lottery_draw('message_rank', [(1, 10)], top_n_winners=3)
        assert ok is True

    def test_unknown_type_fails(self):
        ok, reason = validate_lottery_draw('unknown', [(1, 1)])
        assert ok is False
        assert 'unknown' in reason.lower() or '未知' in reason

    def test_empty_participants_fails(self):
        ok, reason = validate_lottery_draw('message_count', [])
        assert ok is False
        assert reason

    def test_zero_top_n_fails(self):
        ok, reason = validate_lottery_draw('message_rank', [(1, 1)], top_n_winners=0)
        assert ok is False
        assert reason


# ---------------------------------------------------------------------------
# run_lottery_draw (integration of pure layer)
# ---------------------------------------------------------------------------

class TestRunLotteryDraw:
    def test_message_count_returns_one_winner(self):
        participants = [(1, 5), (2, 10), (3, 3)]
        winners = run_lottery_draw('message_count', participants)
        assert len(winners) == 1
        assert winners[0] in (1, 2, 3)

    def test_message_rank_returns_top_n(self):
        participants = [(1, 5), (2, 10), (3, 3)]
        winners = run_lottery_draw('message_rank', participants, top_n_winners=2)
        assert winners == [2, 1]

    def test_fallback_used_when_no_participants(self):
        fallback = [10, 20, 30]
        winners = run_lottery_draw('message_count', [], fallback_pool=fallback)
        assert len(winners) == 1
        assert winners[0] in fallback

    def test_fallback_rank_uses_top_n(self):
        fallback = [10, 20, 30]
        winners = run_lottery_draw('message_rank', [], top_n_winners=2, fallback_pool=fallback)
        assert len(winners) == 2
        assert all(w in fallback for w in winners)

    def test_no_participants_no_fallback_returns_empty(self):
        assert run_lottery_draw('message_count', []) == []

    def test_no_duplicates_in_winners(self):
        participants = [(i, i) for i in range(1, 20)]
        winners = run_lottery_draw('message_rank', participants, top_n_winners=10)
        assert len(winners) == len(set(winners))


# ---------------------------------------------------------------------------
# random lottery type
# ---------------------------------------------------------------------------

class TestRandomLotteryType:
    def test_valid_random(self):
        ok, _ = validate_lottery_draw('random', [(1, 1)], top_n_winners=2)
        assert ok is True

    def test_random_allows_empty_participants(self):
        ok, _ = validate_lottery_draw('random', [], top_n_winners=1)
        assert ok is True

    def test_random_zero_top_n_fails(self):
        ok, reason = validate_lottery_draw('random', [(1, 1)], top_n_winners=0)
        assert ok is False
        assert reason

    def test_run_random_from_participants(self):
        participants = [(10, 1), (20, 1), (30, 1)]
        winners = run_lottery_draw('random', participants, top_n_winners=2)
        assert len(winners) == 2
        assert all(w in (10, 20, 30) for w in winners)
        assert len(set(winners)) == 2

    def test_run_random_from_fallback(self):
        winners = run_lottery_draw('random', [], top_n_winners=2, fallback_pool=[1, 2, 3, 4])
        assert len(winners) == 2
        assert all(w in (1, 2, 3, 4) for w in winners)


# ---------------------------------------------------------------------------
# Random pool source (certified vs all members)
# ---------------------------------------------------------------------------

from app.services.lottery_service import (
    normalize_pool_source,
    load_random_pool,
    draw_random_winners,
    POOL_CERTIFIED,
    POOL_ALL_MEMBERS,
)


class TestNormalizePoolSource:
    def test_default_certified(self):
        assert normalize_pool_source(None) == POOL_CERTIFIED
        assert normalize_pool_source('') == POOL_CERTIFIED
        assert normalize_pool_source('certified') == POOL_CERTIFIED
        assert normalize_pool_source('unknown') == POOL_CERTIFIED

    def test_all_members(self):
        assert normalize_pool_source('all_members') == POOL_ALL_MEMBERS
        assert normalize_pool_source('group_members') == POOL_ALL_MEMBERS


@pytest.mark.usefixtures('db')
class TestRandomPoolSources:
    def _setup(self, db):
        import uuid
        from app.models import BotGroup, GroupUser, GroupMember
        g = BotGroup(
            chat_id=f'-lot-{uuid.uuid4().hex[:10]}',
            title='lot',
            type='supergroup',
            is_active=True,
        )
        db.session.add(g)
        db.session.commit()
        # Certified users: 11, 12
        db.session.add(GroupUser(group_id=g.id, tg_id=11))
        db.session.add(GroupUser(group_id=g.id, tg_id=12))
        # Synced members: 11, 12, 21, plus a bot and a left user
        db.session.add(GroupMember(group_id=g.id, user_id=11, first_name='A', is_bot=False, status='member'))
        db.session.add(GroupMember(group_id=g.id, user_id=12, first_name='B', is_bot=False, status='member'))
        db.session.add(GroupMember(group_id=g.id, user_id=21, first_name='C', is_bot=False, status='member'))
        db.session.add(GroupMember(group_id=g.id, user_id=99, first_name='Bot', is_bot=True, status='member'))
        db.session.add(GroupMember(group_id=g.id, user_id=88, first_name='Gone', is_bot=False, status='left'))
        db.session.commit()
        return g

    def test_certified_pool_is_group_users_only(self, db):
        g = self._setup(db)
        pool = load_random_pool(g.id, 'certified')
        assert set(pool) == {11, 12}

    def test_all_members_uses_live_roster_not_stale_table(self, db, monkeypatch):
        g = self._setup(db)
        monkeypatch.setattr('app.services.lottery_roster.fetch_live_member_ids', lambda chat: [11, 12, 777])
        pool = load_random_pool(g.id, 'all_members', exclude_user_ids=[1000])
        assert set(pool) == {11, 12, 777}
        assert 21 not in pool
        assert 99 not in pool
        assert 88 not in pool

    def test_excludes_bot_id(self, db, monkeypatch):
        g = self._setup(db)
        monkeypatch.setattr('app.services.lottery_roster.fetch_live_member_ids', lambda chat: [11, 12, 21])
        pool = load_random_pool(g.id, 'all_members', exclude_user_ids=[21])
        assert 21 not in pool
        assert set(pool) == {11, 12}

    def test_legacy_missing_pool_source_uses_certified(self, db):
        g = self._setup(db)
        winners = draw_random_winners(g.id, None, 2)
        assert len(winners) == 2
        assert set(winners) <= {11, 12}

    def test_draw_from_all_members(self, db, monkeypatch):
        g = self._setup(db)
        monkeypatch.setattr('app.services.lottery_roster.fetch_live_member_ids', lambda chat: [777, 778, 779])
        winners = draw_random_winners(g.id, 'all_members', 3)
        assert set(winners) == {777, 778, 779}


    def test_missing_userbot_never_falls_back(self, db, monkeypatch):
        from app.services.lottery_roster import LiveRosterError
        from app.userbot.manager import userbot_manager
        g = self._setup(db)
        monkeypatch.setattr(userbot_manager, 'client', None)
        with pytest.raises(LiveRosterError, match='小号已登录'):
            draw_random_winners(g.id, 'all_members', 3)

    def test_unavailable_userbot_never_falls_back(self, db, monkeypatch):
        from app.services.lottery_roster import LiveRosterError
        g = self._setup(db)
        def fail(chat):
            raise LiveRosterError('小号无法访问该群，本次未开奖')
        monkeypatch.setattr('app.services.lottery_roster.fetch_live_member_ids', fail)
        with pytest.raises(LiveRosterError, match='未开奖'):
            draw_random_winners(g.id, 'all_members', 3)

    def test_certified_does_not_fetch_roster(self, db, monkeypatch):
        g = self._setup(db)
        def fail(chat):
            raise AssertionError('default must not fetch Telegram')
        monkeypatch.setattr('app.services.lottery_roster.fetch_live_member_ids', fail)
        assert set(draw_random_winners(g.id, None, 2)) == {11, 12}
