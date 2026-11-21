import json
from datetime import timedelta

import pytest

from app import db
from app.models import GroupLottery, LotterySigninRecord
from app.services import lottery_workflow as w
from tests.unit.test_lottery_workflow import env


@pytest.mark.asyncio
async def test_signin_records_are_idempotent_and_draw_uses_minimum_days(env):
    e = env
    now = w.get_beijing_now()
    lid = e.add('signin', counts=(), start_time=now - timedelta(days=3),
                end_time=now + timedelta(minutes=1), min_signin_days=2,
                top_n_winners=2)
    with e.app.app_context():
        for day in range(2):
            assert w.record_signin_lotteries(e.gid, 101, (now - timedelta(days=2-day)).date()) == 1
        # Same user/date is ignored on retry.
        assert w.record_signin_lotteries(e.gid, 101, (now - timedelta(days=2)).date()) == 0
        # One day is not enough for this activity.
        assert w.record_signin_lotteries(e.gid, 102, (now - timedelta(days=1)).date()) == 1
        db.session.commit()
        assert LotterySigninRecord.query.filter_by(lottery_id=lid, user_id=101).count() == 2
        assert LotterySigninRecord.query.filter_by(lottery_id=lid, user_id=102).count() == 1
        db.session.get(GroupLottery, lid).end_time = now - timedelta(minutes=1)
        db.session.commit()
    await w.draw_lottery(e.app, lid, e.bot, automatic=True)
    assert e.state(lid).winners == [101]
    assert json.loads(e.state(lid).snapshot) == [[101, 1]]


def test_signin_type_validation_and_backup_roundtrip(env):
    e = env
    start, end, minimum, winners, status, prizes = w.validate_settings({
        'lottery_type': 'signin',
        'min_signin_days': 3,
        'start_time': '2030-01-01T00:00',
        'end_time': '2030-01-02T00:00',
        'prizes': [{'name': '签到奖', 'winner_count': 1}],
    })
    assert (minimum, winners, status, prizes) == (10, 1, 'pending', [{'name': '签到奖', 'winner_count': 1}])
    with pytest.raises(ValueError):
        w.validate_settings({
            'lottery_type': 'signin', 'min_signin_days': 101,
            'start_time': '2030-01-01T00:00', 'end_time': '2030-01-02T00:00',
        })
    client = e.app.test_client()
    with client.session_transaction() as session:
        session['logged_in'] = True
    response = client.post('/core/api/save_group_lottery', json={
        'group_id': e.gid, 'lottery_name': '签到备份测试', 'lottery_type': 'signin',
        'min_signin_days': 3, 'start_time': '2030-01-01T00:00',
        'end_time': '2030-01-02T00:00', 'status': 'pending',
        'prizes': [{'name': '签到奖', 'winner_count': 1}],
    })
    assert response.json['status'] == 'ok'
    with e.app.app_context():
        row = db.session.get(GroupLottery, response.json['id'])
        assert row.min_signin_days == 3
