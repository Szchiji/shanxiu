"""Draw entry points must not mark a failed live draw as ended."""
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def lottery_env(flask_app, monkeypatch):
    from app import db
    from app.models import BotGroup, GroupLottery, GroupMember
    from app.modules.core import routes
    monkeypatch.setattr(routes, 'global_flask_app', flask_app)
    monkeypatch.setattr(routes, 'is_plugin_enabled', lambda *a: True)
    monkeypatch.setattr(routes, '_ensure_plugin_enabled', AsyncMock(return_value=True))
    monkeypatch.setattr(routes, 'is_user_admin_in_group', AsyncMock(return_value=True))
    monkeypatch.setattr(routes, '_sched_del', lambda *a: None)
    with flask_app.app_context():
        g = BotGroup(chat_id=str(-int(uuid.uuid4().int % 10**12)), title='live test', type='supergroup')
        db.session.add(g)
        db.session.flush()
        l = GroupLottery(group_id=g.id, lottery_name='live lottery', lottery_type='random',
                         pool_source='all_members', top_n_winners=1, status='active',
                         end_time=routes.get_beijing_now() - timedelta(minutes=1), winner_ids='[]')
        db.session.add(l)
        db.session.add(GroupMember(group_id=g.id, user_id=888, is_bot=False))
        db.session.commit()
        gid, lid, chat_id = g.id, l.id, int(g.chat_id)
    bot = NS(id=999, send_message=AsyncMock())
    context = NS(bot=bot, args=[str(lid)], application=NS(bot_data={}))
    update = NS(effective_chat=NS(id=chat_id, type='supergroup'), effective_user=NS(id=10),
                message=NS(reply_text=AsyncMock()))
    yield NS(app=flask_app, db=db, gid=gid, lid=lid, routes=routes, context=context, update=update)
    with flask_app.app_context():
        GroupLottery.query.filter_by(id=lid).delete()
        GroupMember.query.filter_by(group_id=gid).delete()
        BotGroup.query.filter_by(id=gid).delete()
        db.session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['scheduled', 'manual'])
async def test_failed_live_draw_keeps_active_and_exposes_error(lottery_env, monkeypatch, entry):
    from app.models import GroupLottery
    from app.services.lottery_roster import LiveRosterError
    e = lottery_env
    def fail(chat):
        raise LiveRosterError('全部成员开奖需要小号已登录且在该群；本次未开奖。')
    monkeypatch.setattr('app.services.lottery_roster.fetch_live_member_ids', fail)
    if entry == 'scheduled':
        await e.routes.run_lottery_draws(e.context)
        e.context.bot.send_message.assert_not_awaited()
    else:
        await e.routes.cmd_lottery_draw(e.update, e.context)
        assert '小号已登录' in e.update.message.reply_text.await_args.args[0]
    with e.app.app_context():
        l = e.db.session.get(GroupLottery, e.lid)
        assert l.status == 'active' and l.winner_ids == '[]'
        assert '小号已登录' in l.draw_error
        assert l.pool_source == 'all_members'
    client = e.app.test_client()
    with client.session_transaction() as s:
        s['logged_in'] = True
    body = client.get(f'/core/group/{e.gid}/group_lottery').get_data(as_text=True)
    assert '小号已登录' in body and '现场拉取' in body
    assert '使用已同步的成员表' not in body


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['scheduled', 'manual'])
async def test_successful_live_draw_clears_error_and_uses_current_roster(lottery_env, monkeypatch, entry):
    from app.models import GroupLottery
    e = lottery_env
    with e.app.app_context():
        e.db.session.get(GroupLottery, e.lid).draw_error = 'previous error'
        e.db.session.commit()
    monkeypatch.setattr('app.services.lottery_roster.fetch_live_member_ids', lambda chat: [777])
    if entry == 'scheduled':
        await e.routes.run_lottery_draws(e.context)
    else:
        await e.routes.cmd_lottery_draw(e.update, e.context)
    with e.app.app_context():
        l = e.db.session.get(GroupLottery, e.lid)
        assert l.status == 'ended' and json.loads(l.winner_ids) == [777]
        assert l.draw_error is None
        assert l.pool_source == 'all_members'
