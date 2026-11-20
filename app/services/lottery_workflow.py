"""One draw pipeline for commands/jobs, fenced claims and a durable announcement outbox.

No roster enumeration or userbot. Membership is checked with the owning bot.
A Telegram timeout after acceptance can duplicate the unacknowledged chunk on
retry: the Bot API has no idempotency key; never re-draw to recover delivery.
"""
from __future__ import annotations

import asyncio
import html
import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy import or_, and_
from telegram.error import BadRequest

from app import db
from app.models import BotGroup, GroupLottery, LotteryDelivery, LotteryMessageCount, GroupUser, LotteryEntry, UserPoints, PointsLog
from app.plugins import is_plugin_enabled
from app.utils import get_beijing_now

MAX_WINNERS = 100
DRAW_LEASE_SECONDS = 180
MEMBERSHIP_TIMEOUT = 120
DELIVERY_LEASE_SECONDS = 90
LABELS = {'message_count': '发言加权抽奖', 'message_rank': '发言排行抽奖', 'random': '随机抽奖', 'points': '积分抽奖'}


def validate_settings(data):
    """All new/edited lotteries use explicit Beijing-local, bounded intervals."""
    def stamp(key):
        value = data.get(key)
        if not value:
            raise ValueError('开始时间和开奖时间必须填写')
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            raise ValueError('时间格式无效') from None
        if dt.tzinfo is not None:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
        return dt
    start, end = stamp('start_time'), stamp('end_time')
    if start >= end:
        raise ValueError('开奖时间必须晚于开始时间')
    def integer(key, default, maximum):
        value = data.get(key, default)
        if isinstance(value, bool) or str(value).strip() != str(int(value)):
            raise ValueError('人数和发言门槛必须是整数')
        number = int(value)
        if not 1 <= number <= maximum:
            raise ValueError(f'{key} 必须在 1—{maximum} 之间')
        return number
    minimum = integer('min_messages', 10, 1000000)
    winners = integer('top_n_winners', 3, MAX_WINNERS)
    status = data.get('status', 'pending')
    if status not in ('pending', 'active'):
        raise ValueError('新建或编辑活动仅允许待开始、进行中；已结束活动不能重新开启')
    if len(data.get('lottery_name', '')) > 255 or len(data.get('prize_description') or '') > 10000:
        raise ValueError('名称最多255字，奖品描述最多10000字')
    return start, end, minimum, winners, status


def current_lotteries(group_id, now=None):
    now = now or get_beijing_now()
    return GroupLottery.query.filter(
        GroupLottery.group_id == group_id,
        GroupLottery.status.in_(['pending', 'active']),
        GroupLottery.start_time <= now, GroupLottery.end_time >= now,
    ).order_by(GroupLottery.end_time, GroupLottery.id).all()


def _claim(lottery_id, group_id, automatic):
    now = get_beijing_now()
    l = db.session.get(GroupLottery, lottery_id)
    if not l or (group_id is not None and l.group_id != group_id):
        return {'status': 'error', 'message': '抽奖不存在'}
    group = db.session.get(BotGroup, l.group_id)
    if not group or not group.is_active or not is_plugin_enabled(l.group_id, 'lottery'):
        return {'status': 'paused', 'message': '群组或抽奖功能已停用'}
    if l.lottery_type == 'points' and not is_plugin_enabled(l.group_id, 'points'):
        return {'status': 'paused', 'message': '积分功能已停用，暂缓开奖'}
    if l.status in ('ended', 'cancelled'):
        return {'status': 'ended', 'message': '抽奖已经结束，结果不会重新抽取'}
    if not l.start_time or not l.end_time or l.start_time >= l.end_time:
        message = '请在后台补齐合法开始时间、开奖时间后重试'
        l.draw_error = message
        db.session.commit()
        return {'status': 'error', 'message': message}
    if now < l.start_time:
        return {'status': 'error', 'message': '抽奖尚未开始'}
    if automatic and l.status != 'drawing' and now < l.end_time:
        return {'status': 'error', 'message': '尚未到开奖时间'}
    allowed = ['pending', 'active'] if automatic else ['active', 'pending']
    if l.status not in allowed + ['drawing']:
        return {'status': 'error', 'message': '抽奖状态无效'}
    token = secrets.token_hex(16)
    claimed = GroupLottery.query.filter(
        GroupLottery.id == lottery_id,
        GroupLottery.start_time <= now, GroupLottery.end_time > GroupLottery.start_time,
        or_(GroupLottery.status == 'drawing', GroupLottery.end_time <= now) if automatic else True,
        or_(GroupLottery.status.in_(allowed), and_(
            GroupLottery.status == 'drawing',
            or_(GroupLottery.draw_lease_until == None, GroupLottery.draw_lease_until <= now),
        )),
    ).update({'status': 'drawing', 'draw_token': token,
              'draw_lease_until': now + timedelta(seconds=DRAW_LEASE_SECONDS)}, synchronize_session=False)
    if not claimed:
        db.session.rollback()
        return {'status': 'busy', 'message': '正在开奖或等待资格核验重试，请勿重复操作'}
    # UPDATE and the shared message-counter's parent UPDATE serialize on this row.
    db.session.expire_all()
    l = GroupLottery.query.filter_by(id=lottery_id).with_for_update().one()
    if l.draw_candidates is None:
        if l.lottery_type == 'points':
            rows = LotteryEntry.query.filter_by(lottery_id=lottery_id, state='paid').all()
            l.draw_candidates = json.dumps([[int(row.user_id), 1] for row in rows])
        else:
            rows = LotteryMessageCount.query.filter_by(lottery_id=lottery_id).filter(
                LotteryMessageCount.message_count > 0).all()
            threshold = (l.min_messages or 10) if l.lottery_type == 'message_count' else 1
            l.draw_candidates = json.dumps([[int(row.user_id), row.message_count] for row in rows
                                           if row.message_count >= threshold])
    candidates = json.loads(l.draw_candidates)
    banned = {uid for (uid,) in db.session.query(GroupUser.tg_id).filter_by(
        group_id=l.group_id, is_banned=True).all()}
    result = {'status': 'claimed', 'token': token, 'chat_id': group.chat_id,
              'candidates': [(uid, count) for uid, count in candidates if uid not in banned]}
    db.session.commit()
    return result


async def _eligible(bot, chat_id, candidates):
    semaphore = asyncio.Semaphore(8)
    async def check(item):
        uid, count = item
        if uid == bot.id:
            return None
        async with semaphore:
            try:
                member = await asyncio.wait_for(bot.get_chat_member(chat_id=int(chat_id), user_id=uid), timeout=8)
            except BadRequest as error:
                # Only a definitive per-user absence is exclusion. Unknown access /
                # transport errors must fail the draw, never bias the candidate pool.
                if str(error).lower() in ('user not found', 'participant_id_invalid', 'user_id_invalid'):
                    return None
                raise
            if getattr(getattr(member, 'user', None), 'is_bot', False):
                return None
            status = member.status
            if status in ('creator', 'administrator', 'member') or (
                    status == 'restricted' and member.is_member):
                return uid, count
            if status in ('left', 'kicked', 'restricted'):
                return None
            raise ValueError('unknown membership status')
    # Bound task allocation too: no task per member in a huge group.
    eligible = []
    for offset in range(0, len(candidates), 64):
        tasks = [asyncio.create_task(check(x)) for x in candidates[offset:offset + 64]]
        try:
            chunk = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        eligible.extend(x for x in chunk if x is not None)
    return eligible


def result_parts(lottery, winners):
    lines = ['🎉 抽奖结束！', f'活动：{lottery.lottery_name}', f'类型：{LABELS.get(lottery.lottery_type, "抽奖")}']
    lines.append('获奖者：' if winners else '无人符合条件，本期已结束。')
    if winners:
        lines.extend(f'用户 {int(uid)}' for uid in winners)
    if lottery.lottery_type == 'points':
        lines.append('正常开奖未中奖不退分；已确认离群/封禁的报名积分已原额退还。')
    if lottery.prize_description:
        lines.extend(['奖品：', lottery.prize_description])
    # Plain names/descriptions are escaped before entering HTML. Chunk escaped
    # fragments, never cut an entity or anchor; <3000 UTF-16 units INCLUDING tags.
    fragments = []
    for line in lines:
        fragments.extend(html.escape(line[i:i+400]) for i in range(0, len(line), 400))
    parts, current = [], ''
    for fragment in fragments:
        if current and len((current + '\n' + fragment).encode('utf-16-le')) // 2 > 3000:
            parts.append(current); current = ''
        current += ('\n' if current else '') + fragment
    if current:
        parts.append(current)
    return parts


def _finish(lottery_id, token, participants):
    locked = GroupLottery.query.filter_by(id=lottery_id, status='drawing', draw_token=token).update(
        {'draw_token': token}, synchronize_session=False)
    if not locked:
        db.session.rollback()
        return {'status': 'busy', 'message': '开奖任务已由其他工作者接管'}
    db.session.expire_all()
    l = GroupLottery.query.filter_by(id=lottery_id).with_for_update().one()
    group = db.session.get(BotGroup, l.group_id)
    if not group or not group.is_active or not is_plugin_enabled(l.group_id, 'lottery'):
        db.session.rollback()
        return {'status': 'paused', 'message': '群组或抽奖功能已停用，暂缓开奖'}
    if l.lottery_type == 'points' and not is_plugin_enabled(l.group_id, 'points'):
        db.session.rollback()
        return {'status': 'paused', 'message': '积分功能已停用，暂缓开奖'}
    # Filter application-level bans again after network I/O.
    banned = {uid for (uid,) in db.session.query(GroupUser.tg_id).filter_by(group_id=l.group_id, is_banned=True).all()}
    pool = {int(uid): int(count) for uid, count in participants if uid not in banned}
    from app.services.lottery_service import run_lottery_draw
    if l.lottery_type not in LABELS:
        db.session.rollback()
        return {'status': 'error', 'message': '抽奖类型无效，请联系管理员'}
    if l.lottery_type == 'message_count':
        pool = {uid: count for uid, count in pool.items() if count >= (l.min_messages or 10)}
    n = max(1, min(l.top_n_winners or 1, MAX_WINNERS))
    winners = run_lottery_draw(l.lottery_type, list(pool.items()), n)
    if l.lottery_type == 'points':
        # Definitive departures / bans do not consume an entry fee. Unknown API
        # errors never reach this transaction, so cannot be mistaken for leaving.
        for entry in LotteryEntry.query.filter_by(lottery_id=l.id, state='paid').order_by(LotteryEntry.user_id).all():
            if entry.user_id not in pool:
                _refund(entry, '开奖资格不符')
    l.winner_ids = json.dumps(winners)
    l.status = 'ended'
    l.draw_error = None
    l.draw_finished_at = get_beijing_now()
    l.draw_token = None
    l.draw_lease_until = None
    parts = json.dumps(result_parts(l, winners), ensure_ascii=False)
    targets = dict.fromkeys(str(x).strip() for x in (group.chat_id, l.announce_channel_id) if x and str(x).strip())
    for target in targets:
        db.session.add(LotteryDelivery(lottery_id=l.id, target_chat_id=target, parts=parts))
    db.session.commit()  # result and outbox are indivisible
    return {'status': 'drawn', 'winners': winners}


def _fail(lottery_id, token):
    GroupLottery.query.filter_by(id=lottery_id, status='drawing', draw_token=token).update({
        'draw_error': '无法完整确认候选人的当前群成员资格，本次未开奖；稍后自动重试',
        'draw_lease_until': get_beijing_now() + timedelta(seconds=60),
    }, synchronize_session=False)
    db.session.commit()


async def draw_lottery(app, lottery_id, bot, *, group_id=None, automatic=False):
    if bot is None:
        return {'status': 'paused', 'message': '所属机器人未运行，暂缓开奖'}
    def call(func, *args):
        with app.app_context():
            # Buffer reads before returning the connection: a clean SELECT must
            # not be auto-released while its cursor is still being consumed.
            db.session.begin()
            return func(*args)
    claim = await asyncio.to_thread(call, _claim, lottery_id, group_id, automatic)
    if claim['status'] != 'claimed':
        return claim
    try:
        participants = await asyncio.wait_for(_eligible(bot, claim['chat_id'], claim['candidates']), MEMBERSHIP_TIMEOUT)
    except Exception:
        await asyncio.to_thread(call, _fail, lottery_id, claim['token'])
        return {'status': 'error', 'message': '无法完整确认群成员资格，未开奖；稍后会自动重试'}
    return await asyncio.to_thread(call, _finish, lottery_id, claim['token'], participants)


def due_lotteries(now=None):
    now = now or get_beijing_now()
    rows = db.session.query(GroupLottery.id, BotGroup.clone_id).join(BotGroup).filter(
        BotGroup.is_active == True,
        or_(and_(GroupLottery.status.in_(['pending', 'active']), GroupLottery.end_time <= now),
            and_(GroupLottery.status == 'drawing', or_(GroupLottery.draw_lease_until == None,
                                                      GroupLottery.draw_lease_until <= now))),
    ).order_by(GroupLottery.id).all()
    return [(lid, clone_id) for lid, clone_id in rows]


def _delivery_claim(delivery_id):
    now, token = get_beijing_now(), secrets.token_hex(16)
    row = db.session.get(LotteryDelivery, delivery_id)
    if not row:
        return None
    l = db.session.get(GroupLottery, row.lottery_id)
    g = db.session.get(BotGroup, l.group_id) if l else None
    if not g or not g.is_active or not is_plugin_enabled(g.id, 'lottery'):
        return None
    claimed = LotteryDelivery.query.filter(
        LotteryDelivery.id == delivery_id, LotteryDelivery.state != 'sent',
        or_(LotteryDelivery.retry_at == None, LotteryDelivery.retry_at <= now),
        or_(LotteryDelivery.state == 'pending', LotteryDelivery.lease_until <= now),
    ).update({'state': 'sending', 'claim_token': token,
              'lease_until': now + timedelta(seconds=DELIVERY_LEASE_SECONDS),
              'attempts': LotteryDelivery.attempts + 1}, synchronize_session=False)
    if not claimed:
        db.session.rollback(); return None
    db.session.expire_all()
    row = db.session.get(LotteryDelivery, delivery_id)
    result = {'token': token, 'chat_id': row.target_chat_id,
              'parts': json.loads(row.parts), 'next_part': row.next_part}
    db.session.commit()
    return result


def pending_deliveries(lottery_id=None):
    q = db.session.query(LotteryDelivery.id, BotGroup.clone_id).join(
        GroupLottery, LotteryDelivery.lottery_id == GroupLottery.id).join(
        BotGroup, GroupLottery.group_id == BotGroup.id).filter(
        LotteryDelivery.state != 'sent', BotGroup.is_active == True,
        or_(LotteryDelivery.retry_at == None, LotteryDelivery.retry_at <= get_beijing_now()),
    )
    if lottery_id is not None:
        q = q.filter(LotteryDelivery.lottery_id == lottery_id)
    return list(q.order_by(LotteryDelivery.id).all())


def _delivery_progress(delivery_id, token, part, message_id):
    row = LotteryDelivery.query.filter_by(id=delivery_id, claim_token=token, state='sending').with_for_update().first()
    if not row or row.next_part != part:
        db.session.rollback(); return False
    ids = json.loads(row.message_ids)
    ids.append(int(message_id))
    row.message_ids, row.next_part = json.dumps(ids), part + 1
    row.lease_until = get_beijing_now() + timedelta(seconds=DELIVERY_LEASE_SECONDS)
    row.last_error = None
    if row.next_part == len(json.loads(row.parts)):
        row.state, row.claim_token, row.lease_until = 'sent', None, None
    db.session.commit()
    return True


def _delivery_failed(delivery_id, token):
    LotteryDelivery.query.filter_by(id=delivery_id, claim_token=token, state='sending').update({
        'state': 'pending', 'claim_token': None, 'lease_until': None,
        'retry_at': get_beijing_now() + timedelta(seconds=60),
        'last_error': '发送未确认，稍后重发当前分段；网络超时可能造成该段重复',
    }, synchronize_session=False)
    db.session.commit()


async def deliver_results(app, bot_resolver, lottery_id=None):
    def call(func, *args):
        with app.app_context():
            # Buffer reads before returning the connection: a clean SELECT must
            # not be auto-released while its cursor is still being consumed.
            db.session.begin()
            return func(*args)
    for delivery_id, clone_id in await asyncio.to_thread(call, pending_deliveries, lottery_id):
        bot = bot_resolver(clone_id)
        if bot is None:
            continue
        claim = await asyncio.to_thread(call, _delivery_claim, delivery_id)
        if not claim:
            continue
        try:
            for index in range(claim['next_part'], len(claim['parts'])):
                message = await asyncio.wait_for(bot.send_message(
                    chat_id=claim['chat_id'], text=claim['parts'][index], parse_mode='HTML'), timeout=20)
                saved = await asyncio.to_thread(call, _delivery_progress, delivery_id, claim['token'], index, message.message_id)
                if not saved:
                    break
        except Exception:
            await asyncio.to_thread(call, _delivery_failed, delivery_id, claim['token'])


def points_button(lottery):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    if lottery.lottery_type != 'points' or lottery.status not in ('pending', 'active'):
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f'报名参加（扣 {lottery.points_cost} 积分，仅一次）', callback_data=f'lottery_join:{lottery.id}:{lottery.points_cost}')]])


def _refund(entry, reason):
    """Caller holds the parent lottery write lock; refund + ledger + state atomic."""
    if entry.state != 'paid':
        return
    # Leaving the group may have removed UserPoints. Recreate only the refund,
    # and use an upsert so concurrent refunds from different lotteries are safe.
    dialect = db.session.get_bind().dialect.name
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError('Unsupported points database')
    stmt = insert(UserPoints).values(group_id=entry.group_id, user_id=entry.user_id, points_balance=entry.amount)
    stmt = stmt.on_conflict_do_update(index_elements=['group_id', 'user_id'], set_={
        'points_balance': UserPoints.points_balance + entry.amount,
        'updated_at': get_beijing_now(),
    }).returning(UserPoints.points_balance)
    balance = db.session.execute(stmt).scalar_one()
    db.session.add(PointsLog(group_id=entry.group_id, user_id=entry.user_id, points_change=entry.amount,
                             balance_after=balance, reason=f'积分抽奖退款 #{entry.lottery_id}：{reason}'))
    entry.state, entry.refunded_at = 'refunded', get_beijing_now()


def cancel_paid_lottery(lottery_id):
    """Logical cancellation preserves entries/ledger and fences in-flight draws."""
    locked = GroupLottery.query.filter(GroupLottery.id == lottery_id,
        GroupLottery.status.in_(['pending', 'active', 'drawing', 'cancelled'])).update(
            {'updated_at': GroupLottery.updated_at}, synchronize_session=False)
    if not locked:
        db.session.rollback()
        return False
    db.session.expire_all()
    l = db.session.get(GroupLottery, lottery_id)
    for entry in LotteryEntry.query.filter_by(lottery_id=lottery_id, state='paid').order_by(LotteryEntry.user_id).all():
        _refund(entry, '活动取消')
    l.status, l.draw_token, l.draw_lease_until = 'cancelled', None, None
    l.draw_error = None
    db.session.commit()
    return True


def _entry_context(lottery_id, clone_id, source_chat_id):
    l = db.session.get(GroupLottery, lottery_id)
    if not l or l.lottery_type != 'points':
        raise ValueError('积分抽奖不存在')
    g = db.session.get(BotGroup, l.group_id)
    if not g or g.clone_id != clone_id or str(source_chat_id) not in {
            str(g.chat_id), str(l.announce_channel_id)}:
        raise ValueError('请在所属机器人的对应群组或公告频道报名')
    if not g.is_active or not is_plugin_enabled(g.id, 'lottery') or not is_plugin_enabled(g.id, 'points'):
        raise ValueError('群组、抽奖或积分功能已停用')
    return g, l


def _pay_entry(lottery_id, clone_id, source_chat_id, user_id, expected_cost):
    now = get_beijing_now()
    # Same parent lock as draw claims/cancellation. Recheck everything AFTER it.
    locked = GroupLottery.query.filter(
        GroupLottery.id == lottery_id, GroupLottery.lottery_type == 'points',
        GroupLottery.status.in_(['pending', 'active']),
        GroupLottery.start_time <= now, GroupLottery.end_time > now,
    ).update({'updated_at': GroupLottery.updated_at}, synchronize_session=False)
    if not locked:
        db.session.rollback(); return '报名未开始或已截止，本次未扣分'
    db.session.expire_all()
    g, l = _entry_context(lottery_id, clone_id, source_chat_id)
    existing = LotteryEntry.query.filter_by(lottery_id=l.id, user_id=user_id).first()
    if existing:
        db.session.rollback()
        return '本期已经报名过，不会重复扣分' if existing.state == 'paid' else '本期报名已退款，不能重复报名'
    if l.points_cost != expected_cost or not 1 <= l.points_cost <= 1000000:
        db.session.rollback(); return '报名费用已变化，请刷新活动后重试，本次未扣分'
    if GroupUser.query.filter_by(group_id=g.id,tg_id=user_id,is_banned=True).first():
        db.session.rollback(); return '你已被封禁，不能报名，本次未扣分'
    changed = UserPoints.query.filter_by(group_id=g.id,user_id=user_id).filter(
        UserPoints.points_balance >= l.points_cost).update(
            {'points_balance': UserPoints.points_balance-l.points_cost}, synchronize_session=False)
    if not changed:
        cost = l.points_cost
        db.session.rollback(); return f'积分不足，报名需要 {cost} 积分，本次未扣分'
    db.session.expire_all()
    balance = UserPoints.query.filter_by(group_id=g.id,user_id=user_id).one().points_balance
    db.session.add(LotteryEntry(lottery_id=l.id,group_id=g.id,user_id=user_id,amount=l.points_cost))
    db.session.add(PointsLog(group_id=g.id,user_id=user_id,points_change=-l.points_cost,
        balance_after=balance,reason=f'积分抽奖报名 #{l.id}'))
    cost = l.points_cost
    db.session.commit()
    return f'报名成功，已扣 {cost} 积分，剩余 {balance} 积分。未中奖不退分。'


async def join_points_lottery(app, bot, lottery_id, clone_id, source_chat_id, user_id, expected_cost=None):
    def context():
        with app.app_context():
            db.session.begin()
            g,l = _entry_context(lottery_id,clone_id,source_chat_id)
            now = get_beijing_now()
            if l.status not in ('pending','active') or not l.start_time or not l.end_time or not l.start_time <= now < l.end_time:
                raise ValueError('报名未开始或已截止，本次未扣分')
            if expected_cost is not None and l.points_cost != expected_cost:
                raise ValueError('报名费用已变化，请刷新活动后重试，本次未扣分')
            return g.chat_id, l.points_cost
    try:
        chat_id,cost = await asyncio.to_thread(context)
        members = await asyncio.wait_for(_eligible(bot,chat_id,[(user_id,1)]),10)
        if not members:
            return '须是当前群成员才能报名，本次未扣分'
        def pay():
            with app.app_context():
                return _pay_entry(lottery_id,clone_id,source_chat_id,user_id,cost)
        return await asyncio.to_thread(pay)
    except ValueError as error:
        return str(error)
    except Exception:
        return '无法确认报名状态，本次操作未确认；请查看积分记录或再次点击，系统不会重复扣分'
