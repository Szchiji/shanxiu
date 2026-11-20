"""At-most-once manual announcement attempts. Never retry an ambiguous send.

Telegram has no idempotency key. A process loss after a send starts is UNKNOWN,
not a licence to send again. Completed parts are checkpointed individually.
"""
import asyncio
import json
import logging
import secrets
from datetime import timedelta
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, RetryAfter
from app import db
from app.models import BotGroup, GroupLottery, LotteryPublication
from app.services.lottery_workflow import activity_notice_parts, points_button, get_beijing_now

log = logging.getLogger(__name__)
SEND_TIMEOUT = 15
STALE_SECONDS = 60
MESSAGES = {
    'idle': '尚未发布', 'pending': '发送中', 'sending': '发送中',
    'sent': '公告已发送', 'failed': '发送明确失败，可重试未发送部分',
    'unknown': '送达状态不明，请先核对群/频道消息；为防重复，已禁止再次发送',
}


def status(lottery_id):
    row = db.session.get(LotteryPublication, lottery_id)
    if row is None:
        return {'state': 'idle', 'msg': MESSAGES['idle']}
    now = get_beijing_now()
    if row.state in ('pending', 'sending') and row.updated_at < now - timedelta(seconds=STALE_SECONDS):
        # CAS: a live worker may have checkpointed since this read.
        state = 'failed' if row.state == 'pending' else 'unknown'
        LotteryPublication.query.filter_by(lottery_id=lottery_id, state=row.state,
                                           updated_at=row.updated_at).update(
            {'state': state, 'last_error': MESSAGES[state]}, synchronize_session=False)
        db.session.commit()
        db.session.expire_all()
        row = db.session.get(LotteryPublication, lottery_id)
    return {'state': row.state, 'msg': MESSAGES[row.state], 'completed': row.next_part,
            'total': len(json.loads(row.payload)['messages'])}


def enqueue(lottery, group):
    """Call inside an authorized request context. Return (public state, token).

    A rejected request before any Telegram call may be retried; sent/unknown
    slots are never reset. Snapshots and progress survive application restarts.
    """
    prior = status(lottery.id)
    if prior['state'] not in ('idle', 'failed'):
        return prior, None
    token = secrets.token_hex(16)
    if prior['state'] == 'failed':
        n = LotteryPublication.query.filter_by(lottery_id=lottery.id, state='failed').update(
            {'state': 'pending', 'claim_token': token, 'updated_at': get_beijing_now(), 'last_error': None},
            synchronize_session=False)
        db.session.commit()
        return status(lottery.id), token if n else None
    parts = activity_notice_parts(lottery)
    targets = list(dict.fromkeys(str(x) for x in (group.chat_id, lottery.announce_channel_id) if x))
    markup = points_button(lottery)
    payload = {'messages': [{'chat_id': t, 'text': part,
                            'markup': markup.to_dict() if markup and i == len(parts)-1 else None}
                           for t in targets for i, part in enumerate(parts)]}
    row = LotteryPublication(lottery_id=lottery.id, group_id=group.id, clone_id=group.clone_id,
                             payload=json.dumps(payload, ensure_ascii=False), state='pending',
                             claim_token=token, updated_at=get_beijing_now())
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return status(lottery.id), None
    return status(lottery.id), token


def finish(lottery_id, token, state, next_part=None, message_id=None):
    row = LotteryPublication.query.filter_by(lottery_id=lottery_id, claim_token=token,
                                            state='sending').first()
    if not row:
        return False
    row.state = state
    row.updated_at = get_beijing_now()
    row.last_error = None if state in ('sending', 'sent') else MESSAGES[state]
    if next_part is not None:
        row.next_part = next_part
        ids = json.loads(row.message_ids)
        ids.append(message_id)
        row.message_ids = json.dumps(ids)
    db.session.commit()
    return True


def submission_failed(lottery_id, token):
    LotteryPublication.query.filter_by(lottery_id=lottery_id, claim_token=token, state='pending').update(
        {'state': 'failed', 'last_error': '后台发送未启动', 'updated_at': get_beijing_now()},
        synchronize_session=False)
    db.session.commit()


async def send(app, lottery_id, token, bot):
    """No ORM session/app context is held across network awaits."""
    try:
        with app.app_context():
            n = LotteryPublication.query.filter_by(lottery_id=lottery_id, claim_token=token,
                                                   state='pending').update(
                {'state': 'sending', 'updated_at': get_beijing_now()}, synchronize_session=False)
            db.session.commit()
            if not n:
                return
            row = db.session.get(LotteryPublication, lottery_id)
            payload, first, gid, clone_id = json.loads(row.payload), row.next_part, row.group_id, row.clone_id
        for index in range(first, len(payload['messages'])):
            with app.app_context():
                lottery = db.session.get(GroupLottery, lottery_id)
                group = db.session.get(BotGroup, gid)
                if (not lottery or not group or lottery.group_id != gid or group.clone_id != clone_id or not group.is_active
                        or lottery.status in ('ended', 'drawing', 'cancelled')):
                    finish(lottery_id, token, 'failed')
                    return
            item = payload['messages'][index]
            try:
                msg = await asyncio.wait_for(bot.send_message(
                    chat_id=item['chat_id'], text=item['text'], parse_mode='HTML',
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    reply_markup=InlineKeyboardMarkup.de_json(item['markup'], bot) if item['markup'] else None),
                    timeout=SEND_TIMEOUT)
            except (BadRequest, Forbidden, RetryAfter):
                with app.app_context():
                    finish(lottery_id, token, 'failed')
                return
            except (Exception, asyncio.CancelledError):
                with app.app_context():
                    finish(lottery_id, token, 'unknown')
                return
            with app.app_context():
                if not finish(lottery_id, token, 'sent' if index+1 == len(payload['messages']) else 'sending',
                              index+1, msg.message_id):
                    return
                if index + 1 == len(payload['messages']):
                    # Preserve the old successful-publish activation rule. CAS
                    # cannot overwrite a concurrent draw/cancel transition.
                    GroupLottery.query.filter(
                        GroupLottery.id == lottery_id, GroupLottery.status == 'pending',
                        GroupLottery.start_time <= get_beijing_now()).update(
                            {'status': 'active'}, synchronize_session=False)
                    db.session.commit()
        log.info('Lottery publication %s completed', lottery_id)
    except Exception:
        # E.g. checkpoint/database failure after Telegram accepted a message.
        # Leave sending durable; status turns it UNKNOWN, never auto-replays.
        log.exception('Lottery publication %s stopped; do not auto-retry', lottery_id)
