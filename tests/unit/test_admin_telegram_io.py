"""Offline: durable publication dedupe, ambiguity and nonblocking avatars."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from telegram.error import BadRequest, TimedOut
from app import db
from app.models import BotGroup, LotteryPublication
from app.services import lottery_publication as pub, telegram_photos as photos
from tests.unit.test_lottery_workflow import env


def valid(e, **kwargs):
    now = pub.get_beijing_now()
    return e.add(start_time=now-timedelta(minutes=1), end_time=now+timedelta(hours=1), **kwargs)


def enqueue(e, lid):
    from app.models import GroupLottery
    return e.call(lambda: pub.enqueue(db.session.get(GroupLottery, lid), db.session.get(BotGroup, e.gid)))


def test_route_enqueues_without_waiting_and_dedupes_tabs(env):
    e = env; lid = valid(e); coros = []
    e.mp.setattr(e.routes, 'global_bot_loop', object())
    e.mp.setattr(e.routes, 'global_ptb_app', NS(bot=e.bot))
    def submit(coro, loop):
        coros.append(coro)
        return NS(result=lambda **kw: pytest.fail('HTTP must not wait'))
    e.mp.setattr(asyncio, 'run_coroutine_threadsafe', submit)
    c = e.app.test_client()
    with c.session_transaction() as s: s['logged_in'] = True
    try:
        a = c.post('/core/api/publish_group_lottery', json={'id': lid})
        b = c.post('/core/api/publish_group_lottery', json={'id': lid})
        assert a.status_code == b.status_code == 202
        assert a.json['state'] == 'pending'
        assert c.get(f'/core/api/lottery_publication_status/{lid}').json['state'] == 'pending'
        assert len(coros) == 1
        e.bot.send_message.assert_not_awaited()
    finally:
        for coro in coros: coro.close()


@pytest.mark.asyncio
async def test_success_durable_after_reload_no_republish(env):
    e = env; lid = valid(e)
    _, token = enqueue(e, lid)
    await pub.send(e.app, lid, token, e.bot)
    assert e.call(pub.status, lid)['state'] == 'sent'
    assert enqueue(e, lid)[1] is None
    await pub.send(e.app, lid, token, e.bot)
    e.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('exc', [TimedOut('offline'), TimeoutError(), RuntimeError('unexpected')])
async def test_ambiguous_send_no_blind_retry(env, exc):
    e = env; lid = valid(e); e.bot.send_message.side_effect = exc
    _, token = enqueue(e, lid)
    await pub.send(e.app, lid, token, e.bot)
    assert e.call(pub.status, lid)['state'] == 'unknown'
    assert enqueue(e, lid)[1] is None
    e.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_target_not_resent_after_definite_failure(env):
    e = env; lid = valid(e, announce_channel_id='-100999')
    e.bot.send_message.side_effect = [NS(message_id=1), BadRequest('rejected')]
    _, token = enqueue(e, lid)
    await pub.send(e.app, lid, token, e.bot)
    state = e.call(pub.status, lid)
    assert state['state'] == 'failed' and state['completed'] == 1
    e.bot.send_message.reset_mock(side_effect=True)
    e.bot.send_message.return_value = NS(message_id=2)
    _, token = enqueue(e, lid)
    await pub.send(e.app, lid, token, e.bot)
    assert e.call(pub.status, lid)['state'] == 'sent'
    e.bot.send_message.assert_awaited_once()
    assert e.bot.send_message.await_args.kwargs['chat_id'] == '-100999'


@pytest.mark.asyncio
async def test_background_send_does_not_hold_db_transaction(env):
    e = env; lid = valid(e); _, token = enqueue(e, lid)
    async def inspect(**kw):
        with e.app.app_context():
            assert not db.session().in_transaction()
        return NS(message_id=1)
    e.bot.send_message.side_effect = inspect
    await pub.send(e.app, lid, token, e.bot)
    assert e.call(pub.status, lid)['state'] == 'sent'


@pytest.mark.parametrize('state,expected', [('pending', 'failed'), ('sending', 'unknown')])
def test_restart_stale_work_is_not_automatically_replayed(env, state, expected):
    e = env; lid = valid(e); enqueue(e, lid)
    def stale():
        row = db.session.get(LotteryPublication, lid)
        row.state = state; row.updated_at = pub.get_beijing_now()-timedelta(seconds=100)
        db.session.commit()
    e.call(stale)
    assert e.call(pub.status, lid)['state'] == expected
    if expected == 'unknown': assert enqueue(e, lid)[1] is None


@pytest.mark.asyncio
async def test_deleted_or_cancelled_activity_not_sent(env):
    from app.models import GroupLottery
    e = env; lid = valid(e); _, token = enqueue(e, lid)
    def cancel():
        db.session.get(GroupLottery, lid).status = 'cancelled'; db.session.commit()
    e.call(cancel)
    await pub.send(e.app, lid, token, e.bot)
    e.bot.send_message.assert_not_awaited()


def test_publication_read_and_write_clone_isolation(env):
    e = env; lid = valid(e)
    c = e.app.test_client()
    assert c.get(f'/core/api/lottery_publication_status/{lid}').status_code == 403
    assert c.post('/core/api/publish_group_lottery', json={'id': lid}).status_code == 403
    with c.session_transaction() as s: s['logged_in'] = True; s['clone_id'] = 99
    assert c.get(f'/core/api/lottery_publication_status/{lid}').json['status'] == 'error'
    assert c.post('/core/api/publish_group_lottery', json={'id': lid}).json['status'] == 'error'
    assert e.call(lambda: LotteryPublication.query.count()) == 0


def test_photo_negative_ttl_capacity_and_singleflight(monkeypatch):
    clock = [10.0]; monkeypatch.setattr(photos.time, 'monotonic', lambda: clock[0])
    cache = photos.PhotoCache()
    assert cache.lookup_or_claim('a')[0] == 'fetch'
    assert cache.lookup_or_claim('a')[0] == 'pending'
    cache.complete('a', None)
    assert cache.lookup_or_claim('a')[0] == 'missing'
    clock[0] += photos.FAILURE_TTL + 1
    assert cache.lookup_or_claim('a')[0] == 'fetch'
    for i in range(photos.MAX_ENTRIES + 10): cache.complete(i, b'pic')
    assert len(cache.entries) == photos.MAX_ENTRIES
    for i in range(photos.MAX_INFLIGHT): cache.lookup_or_claim(('pending', i))
    assert len(cache.inflight) == photos.MAX_INFLIGHT
    assert cache.lookup_or_claim('overflow')[0] == 'busy'


def test_photo_route_immediate_coalesced_default_and_clone_guard(env):
    e = env; coros = []; e.mp.setattr(photos, 'cache', photos.PhotoCache())
    e.mp.setattr(e.routes, 'global_bot_loop', object())
    e.mp.setattr(e.routes, 'global_ptb_app', NS(bot=e.bot))
    e.mp.setattr(asyncio, 'run_coroutine_threadsafe', lambda coro, loop: coros.append(coro))
    c = e.app.test_client()
    with c.session_transaction() as s: s['logged_in'] = True
    try:
        for _ in range(2):
            res = c.get('/core/api/tg-photo?id=-100173&type=group')
            assert res.status_code == 200 and res.headers['X-Avatar-State'] == 'pending'
            assert b'<svg' in res.data
        assert len(coros) == 1
        with c.session_transaction() as s: s['clone_id'] = 12
        assert c.get('/core/api/tg-photo?id=-100173&type=group').status_code == 403
    finally:
        for coro in coros: coro.close()


@pytest.mark.asyncio
async def test_photo_total_timeout_cancels_fetch_and_negative_caches(env):
    e = env; e.mp.setattr(photos, 'cache', photos.PhotoCache()); e.mp.setattr(photos, 'TIMEOUT', 0.01)
    stopped = []
    async def hang(*a, **kw):
        try: await asyncio.Event().wait()
        finally: stopped.append(True)
    e.bot.get_chat = hang
    key = (999, 'group', '-100173')
    assert photos.cache.lookup_or_claim(key)[0] == 'fetch'
    await photos.load(key, e.bot, '-100173', 'group')
    assert stopped and photos.cache.lookup_or_claim(key)[0] == 'missing'


def test_photo_snapshot_failure_no_stuck_flight(env):
    e = env; e.mp.setattr(photos, 'cache', photos.PhotoCache())
    e.mp.setattr(e.routes, 'global_bot_loop', object())
    e.mp.setattr(e.routes, 'global_ptb_app', NS(bot=e.bot))
    def fail(*a): raise RuntimeError('loop stopped')
    e.mp.setattr(asyncio, 'run_coroutine_threadsafe', fail)
    c = e.app.test_client()
    with c.session_transaction() as s: s['logged_in'] = True
    res = c.get('/core/api/tg-photo?id=123&type=user')
    assert res.headers['X-Avatar-State'] == 'missing'
    assert not photos.cache.inflight


def test_concurrent_enqueues_share_database_slot(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    e = env; lid = valid(e); gate = Barrier(2)
    original_status = pub.status
    def racing_status(ident):
        result = original_status(ident)
        if result['state'] == 'idle': gate.wait(timeout=5)
        return result
    e.mp.setattr(pub, 'status', racing_status)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue(e, lid), range(2)))
    assert sum(token is not None for _, token in results) == 1
    assert e.call(lambda: LotteryPublication.query.count()) == 1


@pytest.mark.asyncio
async def test_cancelled_send_and_real_deadline_are_unknown(env):
    e = env
    for cancellation in (True, False):
        lid = valid(e); _, token = enqueue(e, lid)
        if cancellation:
            e.bot.send_message.side_effect = asyncio.CancelledError()
        else:
            e.mp.setattr(pub, 'SEND_TIMEOUT', 0.01)
            async def hang(**kw): await asyncio.Event().wait()
            e.bot.send_message.side_effect = hang
        await pub.send(e.app, lid, token, e.bot)
        assert e.call(pub.status, lid)['state'] == 'unknown'
        assert enqueue(e, lid)[1] is None


@pytest.mark.asyncio
async def test_database_checkpoint_failure_never_replays(env):
    e = env; lid = valid(e); _, token = enqueue(e, lid)
    def checkpoint_error(*a, **kw): raise RuntimeError('offline checkpoint')
    e.mp.setattr(pub, 'finish', checkpoint_error)
    await pub.send(e.app, lid, token, e.bot)
    assert enqueue(e, lid)[1] is None
    e.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_avatar_ready_uses_small_photo_and_bounded_size(env):
    e = env
    e.bot.get_chat = AsyncMock(return_value=NS(photo=NS(small_file_id='small')))
    e.bot.get_file = AsyncMock(return_value=NS(file_size=3, download_as_bytearray=AsyncMock(return_value=b'pic')))
    assert await photos.fetch_photo(e.bot, '-100173', 'group') == b'pic'
    e.bot.get_file.assert_awaited_once_with('small')
    e.bot.get_file.return_value.file_size = photos.MAX_BYTES + 1
    assert await photos.fetch_photo(e.bot, '-100173', 'group') is None
    assert e.bot.get_file.return_value.download_as_bytearray.await_count == 1


@pytest.mark.asyncio
async def test_success_preserves_due_activation_not_future_activation(env):
    from app.models import GroupLottery
    e = env
    for future in (False, True):
        now = pub.get_beijing_now()
        lid = e.add(start_time=now+timedelta(minutes=5) if future else now-timedelta(minutes=1),
                    end_time=now+timedelta(hours=1), status='pending')
        _, token = enqueue(e, lid)
        await pub.send(e.app, lid, token, e.bot)
        assert e.call(lambda: db.session.get(GroupLottery, lid).status) == ('pending' if future else 'active')
