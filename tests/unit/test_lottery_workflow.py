"""Offline coverage of the real shared draw/outbox pipeline (no Telegram/userbot)."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from flask import Flask
from app import db
from app.models import BotGroup, GroupLottery, GroupUser, GroupMember, LotteryMessageCount, LotteryDelivery
from app.services import lottery_workflow as w
from app.services.group_engagement import increment_lottery_message_counts as increment


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'lotteries.sqlite'),
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='test-only', TESTING=True)
    db.init_app(app)
    from app.modules.core import routes
    app.register_blueprint(routes.core_bp)
    monkeypatch.setattr(routes, 'global_flask_app', app)
    monkeypatch.setattr(routes, '_sched_del', lambda *a: None)
    monkeypatch.setattr(routes, '_ensure_plugin_enabled', AsyncMock(return_value=True))
    monkeypatch.setattr(routes, 'is_user_admin_in_group', AsyncMock(return_value=True))
    # Any unexpected attempt to enumerate Telegram participants fails this test.
    monkeypatch.setattr('app.services.lottery_roster.fetch_live_member_ids', lambda *a: pytest.fail('no live roster'))
    with app.app_context():
        db.create_all()
        g = BotGroup(chat_id='-100173', title='test', type='supergroup', is_active=True)
        db.session.add(g); db.session.commit(); gid = g.id
    bot = NS(id=999, get_chat_member=AsyncMock(return_value=NS(status='member')),
             send_message=AsyncMock(return_value=NS(message_id=173)))
    def call(fn, *a, **kw):
        with app.app_context():
            return fn(*a, **kw)
    def add(typ='random', counts=((101, 10),), **kw):
        with app.app_context():
            now = w.get_beijing_now()
            opts = dict(group_id=gid, lottery_name='test', lottery_type=typ, status='active',
                        start_time=now-timedelta(hours=1), end_time=now-timedelta(minutes=1),
                        min_messages=10, top_n_winners=3, pool_source='all_members')
            opts.update(kw); row=GroupLottery(**opts); db.session.add(row); db.session.flush()
            for uid, count in counts:
                db.session.add(LotteryMessageCount(lottery_id=row.id,group_id=row.group_id,user_id=uid,message_count=count))
            db.session.commit(); return row.id
    def state(lid):
        with app.app_context():
            l=db.session.get(GroupLottery,lid)
            return NS(status=l.status,winners=json.loads(l.winner_ids or '[]'),
                      awards=json.loads(l.winner_awards) if l.winner_awards else None,
                      snapshot=l.draw_candidates,error=l.draw_error,token=l.draw_token,lease=l.draw_lease_until)
    e=NS(app=app, gid=gid, bot=bot, call=call, add=add, state=state, routes=routes, mp=monkeypatch)
    yield e
    with app.app_context(): db.session.remove(); db.drop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize('automatic',[True,False])
async def test_threshold_no_fallback_and_empty_notice(env, automatic):
    e=env; lid=e.add('message_count',counts=((101,1),))
    e.call(lambda: (db.session.add(GroupUser(group_id=e.gid,tg_id=202)),db.session.commit()))
    result=await w.draw_lottery(e.app,lid,e.bot,automatic=automatic)
    assert result['winners']==[] and e.state(lid).status=='ended'
    e.bot.get_chat_member.assert_not_awaited()
    await w.deliver_results(e.app,lambda _:e.bot)
    assert '无人符合条件' in e.bot.send_message.await_args.kwargs['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('old_pool',['certified','all_members',None])
async def test_random_only_period_speakers_not_certified_or_historical(env,old_pool):
    e=env; lid=e.add(counts=((101,1),(102,2),(103,50)),pool_source=old_pool,top_n_winners=100)
    with e.app.app_context():
        db.session.add(GroupUser(group_id=e.gid,tg_id=202))
        db.session.add(GroupMember(group_id=e.gid,user_id=203,status='member'));db.session.commit()
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert set(e.state(lid).winners)=={101,102,103}
    assert e.bot.get_chat_member.await_count==3


@pytest.mark.asyncio
@pytest.mark.parametrize('status,is_member,eligible',[
    ('member',False,True),('creator',False,True),('administrator',False,True),
    ('restricted',True,True),('restricted',False,False),('left',False,False),('kicked',False,False),
])
async def test_live_membership_policy(env,status,is_member,eligible):
    e=env;lid=e.add();e.bot.get_chat_member.return_value=NS(status=status,is_member=is_member)
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert e.state(lid).winners==([101] if eligible else [])


@pytest.mark.asyncio
async def test_local_ban_excluded_regardless_of_certification_expiry(env):
    e=env;lid=e.add(counts=((101,10),(102,10)))
    with e.app.app_context():
        db.session.add(GroupUser(group_id=e.gid,tg_id=101,is_banned=True))
        db.session.add(GroupUser(group_id=e.gid,tg_id=102,expiration_date=w.get_beijing_now()-timedelta(days=10)));db.session.commit()
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert e.state(lid).winners==[102]


@pytest.mark.asyncio
async def test_unknown_membership_defers_entire_draw_and_recovers_frozen_pool(env):
    e=env;lid=e.add(counts=((101,10),(102,10)))
    e.bot.get_chat_member.side_effect=TimeoutError('offline')
    result=await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert result['status']=='error' and e.state(lid).status=='drawing' and e.state(lid).winners==[]
    assert e.call(lambda:LotteryDelivery.query.count())==0
    with e.app.app_context():
        db.session.get(GroupLottery,lid).draw_lease_until=w.get_beijing_now()-timedelta(seconds=1);db.session.commit()
    e.bot.get_chat_member.side_effect=None
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert set(e.state(lid).winners)=={101,102}


@pytest.mark.asyncio
async def test_concurrent_claims_one_fixed_result(env):
    e=env;lid=e.add();entered=asyncio.Event();release=asyncio.Event()
    async def member(**kw):entered.set();await release.wait();return NS(status='member')
    e.bot.get_chat_member.side_effect=member
    first=asyncio.create_task(w.draw_lottery(e.app,lid,e.bot,automatic=True))
    await entered.wait()
    second=await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert second['status']=='busy'
    release.set();assert (await first)['status']=='drawn'
    assert (await w.draw_lottery(e.app,lid,e.bot,automatic=True))['status']=='ended'
    assert e.call(lambda:LotteryDelivery.query.count())==1


@pytest.mark.asyncio
async def test_old_worker_fenced_after_lease_takeover(env):
    e=env;lid=e.add();a=e.call(w._claim,lid,e.gid,True)
    with e.app.app_context():db.session.get(GroupLottery,lid).draw_lease_until=w.get_beijing_now()-timedelta(seconds=1);db.session.commit()
    b=e.call(w._claim,lid,e.gid,True)
    assert a['token']!=b['token']
    assert e.call(w._finish,lid,a['token'],[(101,10)])['status']=='busy'
    assert e.call(w._finish,lid,b['token'],[(101,10)])['status']=='drawn'


@pytest.mark.asyncio
async def test_counting_freezes_at_claim_for_all_types(env):
    e=env;now=w.get_beijing_now();lid=e.add(end_time=now+timedelta(hours=1))
    assert e.call(increment,e.gid,102)==1
    claim=e.call(w._claim,lid,e.gid,False)
    assert claim['status']=='claimed'
    assert e.call(increment,e.gid,103)==0
    assert {uid for uid,_ in json.loads(e.state(lid).snapshot)}=={101,102}


@pytest.mark.asyncio
async def test_parallel_counts_no_lost_updates(env):
    e=env;lid=e.add(counts=(),end_time=w.get_beijing_now()+timedelta(hours=1))
    await asyncio.gather(*(asyncio.to_thread(e.call,increment,e.gid,101) for _ in range(12)))
    assert e.call(lambda:LotteryMessageCount.query.filter_by(lottery_id=lid,user_id=101).one().message_count)==12


@pytest.mark.asyncio
async def test_per_target_outbox_retries_only_failed_channel(env):
    e=env;lid=e.add(announce_channel_id='-100174')
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    async def send(**kw):
        if kw['chat_id']=='-100174':raise TimeoutError('unknown')
        return NS(message_id=1)
    e.bot.send_message.side_effect=send
    await w.deliver_results(e.app,lambda _:e.bot)
    assert e.call(lambda:sorted((r.target_chat_id,r.state) for r in LotteryDelivery.query.all()))==[('-100173','sent'),('-100174','pending')]
    winners=e.state(lid).winners
    with e.app.app_context():
        for row in LotteryDelivery.query.all():row.retry_at=None
        db.session.commit()
    e.bot.send_message.reset_mock();e.bot.send_message.side_effect=None
    await w.deliver_results(e.app,lambda _:e.bot)
    e.bot.send_message.assert_awaited_once();assert e.bot.send_message.await_args.kwargs['chat_id']=='-100174'
    assert e.state(lid).winners==winners


@pytest.mark.asyncio
async def test_concurrent_delivery_workers_do_not_both_claim(env):
    e=env;lid=e.add();await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    entered=asyncio.Event();release=asyncio.Event()
    async def send(**kw):entered.set();await release.wait();return NS(message_id=1)
    e.bot.send_message.side_effect=send
    a=asyncio.create_task(w.deliver_results(e.app,lambda _:e.bot));await entered.wait()
    await w.deliver_results(e.app,lambda _:e.bot);release.set();await a
    assert e.bot.send_message.await_count==1


@pytest.mark.asyncio
async def test_interrupted_delivery_lease_and_chunk_progress_recover(env):
    e=env;lid=e.add(prize_description='😀<&>'*2200)
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    rowid=e.call(lambda:LotteryDelivery.query.one().id)
    claim=e.call(w._delivery_claim,rowid)
    assert len(claim['parts'])>2
    assert all(len(p.encode('utf-16-le'))//2<=3000 for p in claim['parts'])
    assert all('<&>' not in p for p in claim['parts'])
    assert e.call(w._delivery_progress,rowid,claim['token'],0,1)
    with e.app.app_context():db.session.get(LotteryDelivery,rowid).lease_until=w.get_beijing_now()-timedelta(seconds=1);db.session.commit()
    await w.deliver_results(e.app,lambda _:e.bot)
    assert e.bot.send_message.await_count==len(claim['parts'])-1
    assert e.call(lambda:db.session.get(LotteryDelivery,rowid).state)=='sent'


@pytest.mark.asyncio
async def test_group_and_plugin_pause_and_resume_due_results(env):
    e=env;lid=e.add()
    with e.app.app_context():db.session.get(BotGroup,e.gid).is_active=False;db.session.commit()
    assert (await w.draw_lottery(e.app,lid,e.bot,automatic=True))['status']=='paused'
    assert e.call(w.due_lotteries)==[]
    with e.app.app_context():db.session.get(BotGroup,e.gid).is_active=True;db.session.commit()
    e.mp.setattr(w,'is_plugin_enabled',lambda *a:False)
    assert (await w.draw_lottery(e.app,lid,e.bot,automatic=True))['status']=='paused'
    e.mp.setattr(w,'is_plugin_enabled',lambda *a:True)
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert e.state(lid).status=='ended'
    await w.deliver_results(e.app,lambda _:None);e.bot.send_message.assert_not_awaited()
    await w.deliver_results(e.app,lambda _:e.bot);e.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_rank_stable_ties_and_no_candidate_truncation(env):
    e=env;lid=e.add('message_rank',counts=((200,10),(100,10)),top_n_winners=1)
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert e.state(lid).winners==[100]


@pytest.mark.asyncio
async def test_no_10_activity_limit_random_included(env):
    e=env;now=w.get_beijing_now()
    lids=[e.add(counts=(),end_time=now+timedelta(hours=1)) for _ in range(12)]
    assert e.call(increment,e.gid,101)==12
    assert e.call(lambda:LotteryMessageCount.query.count())==12


@pytest.mark.parametrize('bounds',[
    {'start_time':None}, {'end_time':None},
    {'start_time':w.get_beijing_now()+timedelta(days=1)},
    {'end_time':w.get_beijing_now()-timedelta(days=1)},
])
def test_same_counter_rejects_missing_or_outside_window(env,bounds):
    e=env;e.add(counts=(),**bounds)
    assert e.call(increment,e.gid,101)==0


@pytest.mark.parametrize('changes',[{'top_n_winners':0},{'top_n_winners':101},{'top_n_winners':-1},
    {'min_messages':0},{'status':'ended'},{'start_time':None},{'end_time':'invalid'},
    {'start_time':'2030-01-02T00:00','end_time':'2030-01-01T00:00'}])
def test_validation_rejects_invalid_settings(changes):
    d=dict(start_time='2030-01-01T00:00',end_time='2030-01-02T00:00');d.update(changes)
    with pytest.raises((ValueError,TypeError)):w.validate_settings(d)


def test_offset_normalized_to_beijing():
    start,end,*_=w.validate_settings(dict(start_time='2030-01-01T00:00Z',end_time='2030-01-01T01:00+00:00'))
    assert start.hour==8 and end.hour==9 and start.tzinfo is None


@pytest.mark.asyncio
async def test_manual_cross_clone_or_group_denied_and_no_bot_means_no_draw(env):
    e=env;lid=e.add()
    assert (await w.draw_lottery(e.app,lid,e.bot,group_id=e.gid+1))['status']=='error'
    assert (await w.draw_lottery(e.app,lid,None,automatic=True))['status']=='paused'
    assert e.state(lid).status=='active'


def test_ended_edit_and_delete_with_pending_outbox_rejected(env):
    e=env;lid=e.add(status='ended',winner_ids='[101]')
    c=e.app.test_client()
    with c.session_transaction() as s:s['logged_in']=True
    d=dict(id=lid,group_id=e.gid,lottery_name='reopen',lottery_type='random',status='active',start_time='2030-01-01T00:00',end_time='2030-01-02T00:00')
    assert c.post('/core/api/save_group_lottery',json=d).json['status']=='error'
    assert e.state(lid).winners==[101]
    with e.app.app_context():db.session.add(LotteryDelivery(lottery_id=lid,target_chat_id='-100173',parts='["test"]'));db.session.commit()
    assert c.post('/core/api/delete_group_lottery',json={'id':lid}).json['status']=='error'


@pytest.mark.asyncio
async def test_chinese_keyword_reuses_current_list_no_join_button(env):
    e=env;now=w.get_beijing_now();lid=e.add(end_time=now+timedelta(hours=1),status='pending')
    for n in ('check_spam_protection','check_keyword_filter','check_forced_subscription','check_and_mute_expired_user','update_user_activity','track_message_statistics','track_user_name_change','update_group_member','handle_sync_group_messages'):
        e.mp.setattr(e.routes,n,AsyncMock(return_value=False))
    message=NS(text='抽奖',reply_text=AsyncMock(return_value=NS(message_id=1)))
    update=NS(effective_chat=NS(id=-100173,type='supergroup'),effective_user=NS(id=101,is_bot=False),effective_message=message,message=message)
    ctx=NS(application=NS(bot_data={}),bot=e.bot)
    await e.routes.on_message(update,ctx)
    text=message.reply_text.await_args.args[0]
    assert '随机抽奖' in text and '已达到发言条件' in text and '不要求认证' in text
    assert message.reply_text.await_args.kwargs.get('reply_markup') is None


@pytest.mark.asyncio
async def test_old_ended_records_not_reannounced(env):
    e=env;lid=e.add(status='ended',winner_ids='[101]')
    assert e.call(w.due_lotteries)==[]
    await w.deliver_results(e.app,lambda _:e.bot)
    e.bot.send_message.assert_not_awaited()

from app.models import LotteryEntry, UserPoints, PointsLog


def funded_lottery(e, *, balance=100, cost=10, **kw):
    lid=e.add('points',counts=(),end_time=w.get_beijing_now()+timedelta(hours=1),points_cost=cost,**kw)
    with e.app.app_context():
        db.session.add(UserPoints(group_id=e.gid,user_id=101,points_balance=balance));db.session.commit()
    return lid


async def join(e,lid,uid=101,cost=10,clone_id=None,source=-100173):
    return await w.join_points_lottery(e.app,e.bot,lid,clone_id,source,uid,cost)


@pytest.mark.asyncio
async def test_points_entry_once_balance_ledger_and_no_speech(env):
    e=env;lid=funded_lottery(e)
    assert '报名成功' in await join(e,lid)
    assert '已经报名' in await join(e,lid)
    assert e.call(lambda:UserPoints.query.one().points_balance)==90
    assert e.call(lambda:LotteryEntry.query.count())==1
    assert e.call(lambda:[p.points_change for p in PointsLog.query.all()])==[-10]
    with e.app.app_context():db.session.get(GroupLottery,lid).end_time=w.get_beijing_now()-timedelta(seconds=1);db.session.commit()
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert e.state(lid).winners==[101]
    assert e.call(lambda:UserPoints.query.one().points_balance)==90


@pytest.mark.asyncio
async def test_concurrent_duplicate_entry_single_deduction(env):
    e=env;lid=funded_lottery(e)
    # Warm the new engine's initial result processors before parallel SQLite readers.
    await join(e,lid)
    replies=await asyncio.gather(*(join(e,lid) for _ in range(8)))
    assert all('已经报名' in reply for reply in replies)
    assert e.call(lambda:UserPoints.query.one().points_balance)==90
    assert e.call(lambda:LotteryEntry.query.count())==1


@pytest.mark.asyncio
async def test_two_concurrent_first_clicks_and_other_lotteries_cannot_overdraw(env):
    e=env;lid=funded_lottery(e,balance=10)
    other=e.add('points',counts=(),points_cost=10,end_time=w.get_beijing_now()+timedelta(hours=1))
    entered=asyncio.Event();release=asyncio.Event()
    async def member(**kw):entered.set();await release.wait();return NS(status='member')
    e.bot.get_chat_member.side_effect=member
    first=asyncio.create_task(join(e,lid));await entered.wait()
    second=asyncio.create_task(join(e,other));release.set()
    replies=await asyncio.gather(first,second)
    assert sum('报名成功' in reply for reply in replies)==1
    assert e.call(lambda:UserPoints.query.one().points_balance)==0
    assert e.call(lambda:LotteryEntry.query.count())==1


@pytest.mark.asyncio
@pytest.mark.parametrize('balance',[0,9])
async def test_insufficient_balance_never_records_entry(env,balance):
    e=env;lid=funded_lottery(e,balance=balance)
    assert '积分不足' in await join(e,lid)
    assert e.call(lambda:LotteryEntry.query.count())==0
    assert e.call(lambda:PointsLog.query.count())==0
    assert e.call(lambda:UserPoints.query.one().points_balance)==balance


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['left','banned','unknown','foreign_clone','foreign_source','future','ended','points_off','lottery_off','inactive','wrong_cost'])
async def test_rejected_entry_never_charges(env,kind):
    e=env;lid=funded_lottery(e);params={}
    if kind=='left':e.bot.get_chat_member.return_value=NS(status='left')
    if kind=='unknown':e.bot.get_chat_member.side_effect=TimeoutError('unknown')
    if kind=='foreign_clone':params['clone_id']=99
    if kind=='foreign_source':params['source']=-100999
    if kind=='wrong_cost':params['cost']=20
    if kind in ('points_off','lottery_off'):
        e.mp.setattr(w,'is_plugin_enabled',lambda gid,name:name != ('points' if kind=='points_off' else 'lottery'))
    with e.app.app_context():
        if kind=='banned':db.session.add(GroupUser(group_id=e.gid,tg_id=101,is_banned=True))
        if kind=='future':db.session.get(GroupLottery,lid).start_time=w.get_beijing_now()+timedelta(minutes=5)
        if kind=='ended':db.session.get(GroupLottery,lid).status='ended'
        if kind=='inactive':db.session.get(BotGroup,e.gid).is_active=False
        db.session.commit()
    reply=await join(e,lid,**params)
    assert '报名成功' not in reply
    assert e.call(lambda:UserPoints.query.one().points_balance)==100
    assert e.call(lambda:LotteryEntry.query.count())==0


@pytest.mark.asyncio
async def test_draw_wins_race_against_inflight_membership_entry_without_charging(env):
    e=env;lid=funded_lottery(e);entered=asyncio.Event();release=asyncio.Event()
    async def member(**kw):entered.set();await release.wait();return NS(status='member')
    e.bot.get_chat_member.side_effect=member
    task=asyncio.create_task(join(e,lid));await entered.wait()
    result=await w.draw_lottery(e.app,lid,e.bot,group_id=e.gid)
    assert result['status']=='drawn' and result['winners']==[]
    release.set();assert '已截止' in await task
    assert e.call(lambda:UserPoints.query.one().points_balance)==100


@pytest.mark.asyncio
async def test_cancellation_refunds_once_and_fences_draw(env):
    e=env;lid=funded_lottery(e);await join(e,lid)
    claim=e.call(w._claim,lid,e.gid,False)
    assert e.call(w.cancel_paid_lottery,lid)
    assert e.call(w.cancel_paid_lottery,lid)
    assert e.state(lid).status=='cancelled'
    assert e.call(w._finish,lid,claim['token'],[(101,1)])['status']=='busy'
    assert e.call(lambda:UserPoints.query.one().points_balance)==100
    assert e.call(lambda:[p.points_change for p in PointsLog.query.order_by(PointsLog.id)])==[-10,10]
    assert e.call(lambda:LotteryEntry.query.one().state)=='refunded'


@pytest.mark.asyncio
async def test_cancellation_transaction_rollback_never_partial_refund(env):
    e=env;lid=funded_lottery(e);await join(e,lid)
    original=w._refund
    def fail(entry,reason):original(entry,reason);raise RuntimeError('crash before commit')
    e.mp.setattr(w,'_refund',fail)
    with pytest.raises(RuntimeError):e.call(w.cancel_paid_lottery,lid)
    assert e.call(lambda:UserPoints.query.one().points_balance)==90
    assert e.call(lambda:LotteryEntry.query.one().state)=='paid'
    e.mp.setattr(w,'_refund',original)
    assert e.call(w.cancel_paid_lottery,lid)
    assert e.call(lambda:UserPoints.query.one().points_balance)==100


@pytest.mark.asyncio
async def test_departed_entry_refunded_but_current_loser_not_refunded(env):
    e=env;lid=funded_lottery(e,top_n_winners=1);await join(e,lid)
    with e.app.app_context():
        for uid in [102,103]:db.session.add(UserPoints(group_id=e.gid,user_id=uid,points_balance=100))
        db.session.commit()
    await join(e,lid,102);await join(e,lid,103)
    async def member(**kw):return NS(status='left' if kw['user_id']==101 else 'member')
    e.bot.get_chat_member.side_effect=member
    await w.draw_lottery(e.app,lid,e.bot,group_id=e.gid)
    assert len(e.state(lid).winners)==1 and e.state(lid).winners[0] in [102,103]
    assert e.call(lambda:dict((p.user_id,p.points_balance) for p in UserPoints.query.all()))=={101:100,102:90,103:90}
    assert e.call(lambda:LotteryEntry.query.filter_by(user_id=101).one().state)=='refunded'


@pytest.mark.asyncio
async def test_unknown_membership_neither_draws_nor_refunds_paid_entry(env):
    e=env;lid=funded_lottery(e);await join(e,lid)
    e.bot.get_chat_member.side_effect=TimeoutError('not known absent')
    await w.draw_lottery(e.app,lid,e.bot,group_id=e.gid)
    assert e.state(lid).status=='drawing'
    assert e.call(lambda:UserPoints.query.one().points_balance)==90
    assert e.call(lambda:LotteryEntry.query.one().state)=='paid'
    assert e.call(lambda:LotteryDelivery.query.count())==0


@pytest.mark.asyncio
async def test_fee_and_rules_locked_after_entry_and_api_delete_refunds(env):
    e=env;lid=funded_lottery(e);await join(e,lid)
    client=e.app.test_client()
    with client.session_transaction() as session:session['logged_in']=True
    data=dict(id=lid,group_id=e.gid,lottery_name='new price',lottery_type='random',points_cost=20,
        start_time='2030-01-01T00:00',end_time='2030-01-02T00:00',status='pending')
    assert client.post('/core/api/save_group_lottery',json=data).json['status']=='error'
    assert client.post('/core/api/delete_group_lottery',json={'id':lid}).json['status']=='ok'
    assert client.post('/core/api/delete_group_lottery',json={'id':lid}).json['status']=='ok'
    assert e.call(lambda:UserPoints.query.one().points_balance)==100
    assert e.state(lid).status=='cancelled'


@pytest.mark.asyncio
async def test_refund_recreates_deleted_account_and_stays_on_leave(env):
    e=env;lid=funded_lottery(e);await join(e,lid)
    with e.app.app_context():UserPoints.query.delete();db.session.commit()
    assert e.call(w.cancel_paid_lottery,lid)
    assert e.call(lambda:UserPoints.query.one().points_balance)==10
    update=NS(effective_chat=NS(id=-100173,type='supergroup'),message=NS(left_chat_member=NS(id=101,is_bot=False)))
    context=NS(application=NS(bot_data={}),bot=e.bot)
    await e.routes.handle_left_chat_member(update,context)
    assert e.call(lambda:UserPoints.query.one().points_balance)==10


@pytest.mark.asyncio
async def test_refund_and_result_outbox_commit_together(env):
    e=env;lid=funded_lottery(e);await join(e,lid)
    e.bot.get_chat_member.return_value=NS(status='left')
    await w.draw_lottery(e.app,lid,e.bot,group_id=e.gid)
    assert e.state(lid).status=='ended' and e.state(lid).winners==[]
    assert e.call(lambda:UserPoints.query.one().points_balance)==100
    assert e.call(lambda:LotteryDelivery.query.count())==1


def test_points_button_snapshots_visible_cost(env):
    e=env;lid=funded_lottery(e,cost=20)
    with e.app.app_context():
        markup=w.points_button(db.session.get(GroupLottery,lid))
        assert markup.inline_keyboard[0][0].callback_data==f'lottery_join:{lid}:20'
        assert '扣 20 积分' in markup.inline_keyboard[0][0].text


@pytest.mark.asyncio
async def test_points_callback_feedback_uses_own_clone_and_source(env):
    e=env;lid=funded_lottery(e)
    query=NS(data=f'lottery_join:{lid}:10',from_user=NS(id=101,is_bot=False),message=NS(chat=NS(id=-100173)),answer=AsyncMock())
    await e.routes.handle_lottery_join(NS(callback_query=query),NS(bot=e.bot,application=NS(bot_data={})))
    assert '报名成功' in query.answer.await_args.args[0]
    assert query.answer.await_args.kwargs['show_alert'] is True

@pytest.mark.asyncio
async def test_simultaneous_first_click_same_entry_one_ledger(env):
    e=env;lid=funded_lottery(e);first_entered=asyncio.Event();both_entered=asyncio.Event();release=asyncio.Event();calls=[]
    async def member(**kw):
        calls.append(kw);first_entered.set()
        if len(calls)==2:both_entered.set()
        await release.wait();return NS(status='member')
    e.bot.get_chat_member.side_effect=member
    first=asyncio.create_task(join(e,lid));await first_entered.wait()
    second=asyncio.create_task(join(e,lid));await both_entered.wait();release.set()
    replies=await asyncio.gather(first,second)
    assert sum('报名成功' in reply for reply in replies)==1
    assert sum('已经报名' in reply for reply in replies)==1
    assert e.call(lambda:UserPoints.query.one().points_balance)==90
    assert e.call(lambda:PointsLog.query.count())==1


@pytest.mark.asyncio
async def test_first_entry_wins_draw_race_then_included(env):
    e=env;lid=funded_lottery(e);await join(e,lid)
    claim=e.call(w._claim,lid,e.gid,False)
    assert claim['candidates']==[(101,1)]
    assert e.call(w._finish,lid,claim['token'],claim['candidates'])['winners']==[101]


@pytest.mark.asyncio
async def test_cancel_during_join_no_charge(env):
    e=env;lid=funded_lottery(e);entered=asyncio.Event();release=asyncio.Event()
    async def member(**kw):entered.set();await release.wait();return NS(status='member')
    e.bot.get_chat_member.side_effect=member
    task=asyncio.create_task(join(e,lid));await entered.wait()
    assert e.call(w.cancel_paid_lottery,lid)
    release.set();assert '已截止' in await task
    assert e.call(lambda:UserPoints.query.one().points_balance)==100


@pytest.mark.asyncio
async def test_unconfirmed_success_click_retry_does_not_recharge(env):
    e=env;lid=funded_lottery(e)
    await join(e,lid)  # simulate callback answer lost after DB commit
    assert '已经报名' in await join(e,lid)
    assert e.call(lambda:UserPoints.query.one().points_balance)==90


def test_schema_upgrade_preserves_history_and_is_idempotent(env):
    from sqlalchemy import text, inspect
    from app.services.lottery_schema import ensure_lottery_schema
    e=env;lid=e.add(status='ended',winner_ids='[101]')
    with e.app.app_context():
        # Exercise migration from the actual previous table shape.
        for column in ['points_cost','draw_token','draw_lease_until','draw_candidates','draw_finished_at']:
            db.session.execute(text(f'ALTER TABLE group_lottery DROP COLUMN {column}'))
        db.session.commit()
        ensure_lottery_schema(db);ensure_lottery_schema(db)
        assert {c['name'] for c in inspect(db.engine).get_columns('group_lottery')} >= {'draw_token','points_cost','draw_candidates'}
        row=db.session.execute(text('SELECT status,winner_ids,points_cost FROM group_lottery WHERE id=:id'),{'id':lid}).one()
        assert tuple(row)==('ended','[101]',10)
        assert LotteryEntry.query.count()==0 and LotteryDelivery.query.count()==0


def test_future_creation_forces_pending_and_points_roundtrip(env):
    e=env;c=e.app.test_client()
    with c.session_transaction() as s:s['logged_in']=True
    data=dict(group_id=e.gid,lottery_name='future',lottery_type='points',points_cost=25,status='active',
        start_time='2030-01-01T00:00',end_time='2030-01-02T00:00')
    result=c.post('/core/api/save_group_lottery',json=data).json
    assert result['status']=='ok'
    assert e.state(result['id']).status=='pending'
    with e.app.app_context():
        from app.services.backup_service import export_group_config
        exported=export_group_config(e.gid)
        assert exported['lotteries'][0]['points_cost']==25

@pytest.mark.asyncio
async def test_bots_never_in_member_pool(env):
    e=env;lid=e.add();e.bot.get_chat_member.return_value=NS(status='member',user=NS(is_bot=True))
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert e.state(lid).winners==[]


def test_time_and_type_edits_cannot_reuse_old_speech_counts(env):
    e=env;lid=e.add();c=e.app.test_client()
    with c.session_transaction() as s:s['logged_in']=True
    data=dict(id=lid,group_id=e.gid,lottery_name='changed',lottery_type='random',status='pending',
        start_time='2030-01-01T00:00',end_time='2030-01-02T00:00')
    assert c.post('/core/api/save_group_lottery',json=data).json['status']=='error'

@pytest.mark.asyncio
async def test_group_delete_cannot_erase_paid_entry_or_ledger(env):
    e=env;lid=funded_lottery(e);await join(e,lid)
    with e.app.app_context():
        with pytest.raises(ValueError,match='资金记录'):
            e.routes._delete_group_cascade(e.gid)
        db.session.rollback()
        assert LotteryEntry.query.count()==1 and PointsLog.query.count()==1
        assert UserPoints.query.one().points_balance==90


def _prizes(*rows):
    return json.dumps([{'name': name, 'winner_count': count} for name, count in rows], ensure_ascii=False)


@pytest.mark.asyncio
async def test_legacy_message_count_without_prizes_still_draws_one(env):
    e=env;lid=e.add('message_count',counts=((101,10),(102,20),(103,30)),top_n_winners=3,prize_description='旧奖品')
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    assert len(e.state(lid).winners)==1 and e.state(lid).winners[0] in {101,102,103}
    assert e.state(lid).awards[0]['prize_name']=='旧奖品'


@pytest.mark.asyncio
@pytest.mark.parametrize('typ,counts,expect',[
    ('message_count',((101,10),(102,20),(103,30)),None),
    ('random',((101,1),(102,1),(103,1)),None),
    ('message_rank',((101,5),(102,9),(103,9)),[102,103]),
])
async def test_multi_prize_assignment_and_no_replacement(env,typ,counts,expect):
    e=env
    lid=e.add(typ,counts=counts,prizes=_prizes(('一等奖',1),('二等奖',1)))
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    state=e.state(lid)
    assert len(state.winners)==2 and len(set(state.winners))==2
    assert [a['prize_name'] for a in state.awards]==['一等奖','二等奖']
    if expect:
        assert state.winners==expect
    parts=e.call(lambda:json.loads(__import__('app.models',fromlist=['LotteryDelivery']).LotteryDelivery.query.one().parts))
    assert any('用户 '+str(state.winners[0])+' — 一等奖' in part for part in parts)
    assert any('用户 '+str(state.winners[1])+' — 二等奖' in part for part in parts)


@pytest.mark.asyncio
async def test_points_multi_prize_assignment(env):
    e=env;lid=funded_lottery(e,top_n_winners=2,prizes=_prizes(('A',1),('B',1)))
    with e.app.app_context():
        db.session.add(UserPoints(group_id=e.gid,user_id=102,points_balance=100));db.session.commit()
    await join(e,lid,101);await join(e,lid,102)
    await w.draw_lottery(e.app,lid,e.bot,group_id=e.gid)
    state=e.state(lid)
    assert set(state.winners)=={101,102}
    assert [a['prize_name'] for a in state.awards]==['A','B']


@pytest.mark.asyncio
async def test_insufficient_participants_fill_in_order_and_retry_keeps_snapshot(env):
    e=env;lid=e.add('random',counts=((101,1),),prizes=_prizes(('一等奖',1),('二等奖',2)),prize_description='should not reassign')
    await w.draw_lottery(e.app,lid,e.bot,automatic=True)
    state=e.state(lid)
    assert state.winners==[101] and state.awards==[{'user_id':101,'prize_name':'一等奖','prize_index':0}]
    text='\n'.join(e.call(lambda:json.loads(__import__('app.models',fromlist=['LotteryDelivery']).LotteryDelivery.query.one().parts)))
    assert '二等奖 还缺2名' in text
    first=list(state.awards)
    e.bot.send_message.side_effect=RuntimeError('temporary send failure')
    await w.deliver_results(e.app,lambda _:e.bot)
    with e.app.app_context():
        for row in LotteryDelivery.query.all():row.retry_at=None
        db.session.commit()
    e.bot.send_message.reset_mock();e.bot.send_message.side_effect=None
    await w.deliver_results(e.app,lambda _:e.bot)
    assert e.state(lid).awards==first and e.state(lid).winners==[101]
    assert '用户 101 — 一等奖' in e.bot.send_message.await_args.kwargs['text']


def test_ended_legacy_results_are_not_reassigned(env):
    e=env;lid=e.add(status='ended',winner_ids='[101,102]',prize_description='旧共享奖品',top_n_winners=2,prizes=None)
    parts=e.call(lambda:w.result_parts(db.session.get(GroupLottery,lid),[101,102]))
    text='\n'.join(parts)
    assert '用户 101' in text and '用户 102' in text
    assert '—' not in text
    assert '旧共享奖品' in text
    assert e.state(lid).awards is None and e.state(lid).winners==[101,102]


def test_prizes_validation_and_points_lock_includes_prize_rows(env):
    e=env
    start,end,minimum,winners,status,prizes=w.validate_settings(dict(
        start_time='2030-01-01T00:00',end_time='2030-01-02T00:00',lottery_type='random',
        prizes=[{'name':'A','winner_count':2},{'name':'B','winner_count':3}]))
    assert winners==5 and prizes==[{'name':'A','winner_count':2},{'name':'B','winner_count':3}]
    with pytest.raises(ValueError):
        w.validate_settings(dict(start_time='2030-01-01T00:00',end_time='2030-01-02T00:00',prizes=[{'name':'A','winner_count':101}]))
    with pytest.raises(ValueError):
        w.validate_settings(dict(start_time='2030-01-01T00:00',end_time='2030-01-02T00:00',prizes=[{'name':'','winner_count':1}]))
    lid=funded_lottery(e)
    with e.app.app_context():
        db.session.add(LotteryEntry(lottery_id=lid,group_id=e.gid,user_id=101,amount=10,state='paid'))
        db.session.commit()
    c=e.app.test_client();
    with c.session_transaction() as s:s['logged_in']=True
    data=dict(id=lid,group_id=e.gid,lottery_name='改奖',lottery_type='points',points_cost=10,
              start_time='2030-01-01T00:00',end_time='2030-01-02T00:00',status='pending',
              prizes=[{'name':'新奖','winner_count':2}])
    assert c.post('/core/api/save_group_lottery',json=data).json['status']=='error'
