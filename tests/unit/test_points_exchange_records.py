"""积分商城兑换记录快照、默认领取说明、深链兑换与后台展示。"""

import asyncio
import json
import logging
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from telegram.error import Forbidden


def _unique_chat(prefix='-1008'):
    return prefix + f'{uuid4().int % 10**12:012d}'


def _login(client, clone_id=None):
    with client.session_transaction() as session:
        session['logged_in'] = True
        if clone_id is None:
            session.pop('clone_id', None)
        else:
            session['clone_id'] = clone_id


def _tg_user(user_id=101, username='alice', first_name='Alice', last_name='Li'):
    return NS(id=user_id, username=username, first_name=first_name, last_name=last_name, is_bot=False)


@pytest.fixture()
def exchange_env(flask_app, monkeypatch):
    from app import db
    from app.models import BotGroup, PointsExchangeItem, UserPoints
    from app.modules.core import routes

    monkeypatch.setattr(routes, 'global_flask_app', flask_app)
    monkeypatch.setattr(routes, '_update_exchange_catalog_async', AsyncMock())
    monkeypatch.setattr(routes, '_delete_item_announcement_async', AsyncMock())
    for name in (
        'check_spam_protection', 'check_keyword_filter', 'check_forced_subscription',
    ):
        monkeypatch.setattr(routes, name, AsyncMock(return_value=False))
    for name in (
        'check_and_mute_expired_user', 'update_user_activity', 'track_message_statistics',
        'track_user_name_change', 'update_group_member', 'handle_sync_group_messages',
    ):
        monkeypatch.setattr(routes, name, AsyncMock())

    with flask_app.app_context():
        group = BotGroup(
            chat_id=_unique_chat(),
            title='积分测试群',
            type='supergroup',
            is_active=True,
            clone_id=None,
            config=json.dumps({
                'points_shop_enabled': True,
                'exchange_command_word': '兑换',
                'exchange_default_redemption_info': '请私信 @shop_admin 领取',
                'exchange_channel_id': '-100999',
                'auto_like': False,
                'auto_member_tag': False,
            }, ensure_ascii=False),
        )
        db.session.add(group)
        db.session.flush()
        item = PointsExchangeItem(
            group_id=group.id,
            item_name='测试商品',
            points_cost=30,
            stock=5,
            is_active=True,
            announcement_msg_id=888,
        )
        db.session.add(item)
        db.session.flush()
        points = UserPoints(group_id=group.id, user_id=101, points_balance=100)
        db.session.add(points)
        db.session.commit()
        ids = NS(gid=group.id, iid=item.id, chat_id=int(group.chat_id), uid=101)

    yield NS(app=flask_app, db=db, routes=routes, **ids.__dict__)

    with flask_app.app_context():
        from app.models import (
            GroupMember, PointsExchangeRecord, PointsLog, PointsExchangeItem, UserPoints, BotGroup,
        )
        PointsExchangeRecord.query.filter_by(group_id=ids.gid).delete()
        PointsLog.query.filter_by(group_id=ids.gid).delete()
        UserPoints.query.filter_by(group_id=ids.gid).delete()
        PointsExchangeItem.query.filter_by(group_id=ids.gid).delete()
        GroupMember.query.filter_by(group_id=ids.gid).delete()
        BotGroup.query.filter_by(id=ids.gid).delete()
        db.session.commit()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_startup_migration_adds_snapshot_columns(tmp_path):
    from app.models import ensure_points_exchange_record_schema

    engine = create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    with engine.connect() as conn:
        conn.execute(text('''
            CREATE TABLE points_exchange_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER,
                user_id INTEGER,
                item_id INTEGER,
                points_spent INTEGER NOT NULL,
                created_at TIMESTAMP
            )
        '''))
        conn.commit()

    db = NS(engine=engine)
    applied = ensure_points_exchange_record_schema(db)
    joined = '\n'.join(applied)
    assert 'user_username' in joined
    assert 'user_display_name' in joined
    assert 'item_name_snapshot' in joined
    assert 'IF NOT EXISTS' not in joined
    assert 'MODIFY' not in joined.upper()
    assert 'ix_bot_groups_chat_id' not in joined

    cols = {c['name'] for c in inspect(engine).get_columns('points_exchange_records')}
    assert {'user_username', 'user_display_name', 'item_name_snapshot'} <= cols

    assert ensure_points_exchange_record_schema(db) == []

    with engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO points_exchange_records "
            "(group_id, user_id, points_spent, user_username, user_display_name, item_name_snapshot) "
            "VALUES (1, 2, 10, 'alice', 'Alice', '旧商品')"
        ))
        conn.commit()
        row = conn.execute(text(
            'SELECT user_username, user_display_name, item_name_snapshot FROM points_exchange_records'
        )).one()
        assert row == ('alice', 'Alice', '旧商品')


def test_startup_migration_noop_when_table_missing(tmp_path):
    from app.models import ensure_points_exchange_record_schema
    engine = create_engine(f'sqlite:///{tmp_path / "empty.db"}')
    assert ensure_points_exchange_record_schema(NS(engine=engine)) == []


# ---------------------------------------------------------------------------
# Helpers: snapshots, buttons, receipts, alerts
# ---------------------------------------------------------------------------

def test_snapshot_and_redemption_info_fallback(exchange_env):
    r = exchange_env.routes
    assert r._snapshot_exchange_user(_tg_user()) == ('alice', 'Alice Li')
    assert r._snapshot_exchange_user(_tg_user(username='@bob', first_name='Bob', last_name='')) == ('bob', 'Bob')
    assert r._snapshot_exchange_user(_tg_user(username=None, first_name='', last_name='')) == (None, None)

    item = NS(redemption_info='  商品说明  ')
    conf = {'exchange_default_redemption_info': '默认说明'}
    assert r._resolve_redemption_info(item, conf) == '商品说明'
    assert r._resolve_redemption_info(NS(redemption_info=''), conf) == '默认说明'
    assert r._resolve_redemption_info(NS(redemption_info=None), {}) == ''


def test_exchange_item_button_uses_deep_link_and_falls_back():
    from app.modules.core import routes
    url_btn = routes._exchange_item_button('兑换', 12, 34, 'shop_bot')
    assert url_btn.url == 'https://t.me/shop_bot?start=exchange_12_34'
    assert url_btn.callback_data is None

    cb_btn = routes._exchange_item_button('兑换', 12, 34, None)
    assert cb_btn.callback_data == 'exchange_redeem_34_12'
    assert cb_btn.url is None


def test_catalog_and_announcement_buttons_are_deep_links(exchange_env):
    r = exchange_env.routes
    with exchange_env.app.app_context():
        _text, buttons = r._build_exchange_catalog_sync(exchange_env.gid, bot_username='juice_bot')
    assert buttons
    btn = buttons[0][0]
    assert btn.url == f'https://t.me/juice_bot?start=exchange_{exchange_env.gid}_{exchange_env.iid}'
    assert btn.callback_data is None
    assert '打开机器人' in _text

    announce_btn = r._exchange_item_button(
        '🛍️ 兑换', exchange_env.gid, exchange_env.iid, 'juice_bot'
    )
    assert announce_btn.url == f'https://t.me/juice_bot?start=exchange_{exchange_env.gid}_{exchange_env.iid}'


@pytest.mark.asyncio
async def test_announcement_send_and_edit_use_deep_link_url(exchange_env):
    r = exchange_env.routes
    bot = NS(
        username='juice_bot',
        send_message=AsyncMock(return_value=NS(message_id=321)),
        edit_message_text=AsyncMock(),
        get_me=AsyncMock(return_value=NS(username='juice_bot')),
    )
    ptb_app = NS(bot=bot)

    await r._send_item_announcement_async(exchange_env.gid, exchange_env.iid, ptb_app)
    bot.send_message.assert_awaited()
    markup = bot.send_message.await_args.kwargs['reply_markup']
    sent_btn = markup.inline_keyboard[0][0]
    assert sent_btn.url == f'https://t.me/juice_bot?start=exchange_{exchange_env.gid}_{exchange_env.iid}'

    await r._edit_item_announcement_async(exchange_env.gid, exchange_env.iid, ptb_app)
    bot.edit_message_text.assert_awaited()
    edit_markup = bot.edit_message_text.await_args.kwargs['reply_markup']
    edit_btn = edit_markup.inline_keyboard[0][0]
    assert edit_btn.url == f'https://t.me/juice_bot?start=exchange_{exchange_env.gid}_{exchange_env.iid}'


def test_receipt_and_alert_escape_and_length():
    from app.modules.core import routes
    result = {
        'item_name': '礼品 <b>hack</b>',
        'points_cost': 10,
        'balance': 90,
        'redemption_info': '请找 <script>x</script>',
        'user_id': 101,
        'user_username': 'al<ice',
        'user_display_name': 'Alice & Co',
        'group_title': '群 <b>A</b>',
        'created_at': datetime(2026, 11, 20, 12, 0, 0),
        'redeemer_display': 'Alice & Co (ID: 101)',
    }
    group_reply = routes._build_exchange_success_reply(result)
    assert '礼品 &lt;b&gt;hack&lt;/b&gt;' in group_reply
    assert '请找 &lt;script&gt;x&lt;/script&gt;' in group_reply
    assert '<script>' not in group_reply
    assert '领取说明' in group_reply

    receipt = routes._build_exchange_private_receipt(result)
    assert '群 &lt;b&gt;A&lt;/b&gt;' in receipt
    assert '@al&lt;ice' in receipt
    assert 'Alice &amp; Co' in receipt
    assert '2026-11-20 12:00:00' in receipt
    assert 'ID: <code>101</code>' in receipt

    result['item_name'] = '超长商品名称' * 40
    alert = routes._build_exchange_callback_alert(result)
    assert len(alert) <= 200
    assert '兑换成功' in alert


# ---------------------------------------------------------------------------
# Group command redemption
# ---------------------------------------------------------------------------

def _group_update(env, text, user=None):
    user = user or _tg_user(env.uid)
    message = NS(text=text, reply_html=AsyncMock(), message_id=1)
    update = NS(
        effective_chat=NS(id=env.chat_id, type='supergroup', title='积分测试群'),
        effective_user=user,
        effective_message=message,
        message=message,
    )
    context = NS(
        bot=NS(username='juice_bot', id=999, send_message=AsyncMock()),
        application=NS(bot_data={}, bot=NS(username='juice_bot')),
        job_queue=NS(run_once=lambda *a, **k: None),
    )
    return update, context, message


@pytest.mark.asyncio
async def test_group_command_redeems_and_uses_default_info(exchange_env):
    from app.models import PointsExchangeRecord, UserPoints
    env = exchange_env
    update, context, message = _group_update(env, f'兑换 {env.iid}')
    await env.routes.on_message(update, context)
    message.reply_html.assert_awaited()
    body = message.reply_html.await_args.args[0]
    assert '兑换成功' in body
    assert '测试商品' in body
    assert '请私信 @shop_admin 领取' in body
    assert '@alice' in body or 'Alice' in body

    with env.app.app_context():
        rec = PointsExchangeRecord.query.filter_by(group_id=env.gid, user_id=env.uid).one()
        assert rec.user_username == 'alice'
        assert rec.user_display_name == 'Alice Li'
        assert rec.item_name_snapshot == '测试商品'
        assert rec.points_spent == 30
        assert not hasattr(rec, 'redeem_code') or rec.__dict__.get('redeem_code', None) is None
        assert UserPoints.query.filter_by(group_id=env.gid, user_id=env.uid).one().points_balance == 70


@pytest.mark.asyncio
async def test_group_command_escapes_html_and_prefers_item_info(exchange_env):
    from app.models import PointsExchangeItem
    env = exchange_env
    with env.app.app_context():
        item = env.db.session.get(PointsExchangeItem, env.iid)
        item.item_name = 'A <b>X</b>'
        item.redemption_info = '找 <admin>'
        env.db.session.commit()
    update, context, message = _group_update(env, f'兑换 {env.iid}')
    await env.routes.on_message(update, context)
    body = message.reply_html.await_args.args[0]
    assert 'A &lt;b&gt;X&lt;/b&gt;' in body
    assert '找 &lt;admin&gt;' in body
    assert '请私信 @shop_admin 领取' not in body


# ---------------------------------------------------------------------------
# /start deep-link redemption
# ---------------------------------------------------------------------------

def _start_update(user=None, clone_id=None):
    user = user or _tg_user()
    message = NS(reply_html=AsyncMock(), message_id=9)
    update = NS(
        effective_user=user,
        effective_chat=NS(id=user.id, type='private'),
        message=message,
    )
    bot = NS(username='juice_bot', send_message=AsyncMock(), id=999)
    context = NS(
        args=[],
        bot=bot,
        application=NS(bot_data={'clone_id': clone_id}, bot=bot),
    )
    return update, context, message


@pytest.mark.asyncio
async def test_start_exchange_redeems_in_private_chat(exchange_env):
    from app.models import PointsExchangeRecord, UserPoints
    env = exchange_env
    update, context, message = _start_update()
    context.args = [f'exchange_{env.gid}_{env.iid}']
    await env.routes.cmd_start(update, context)

    message.reply_html.assert_awaited_once()
    body = message.reply_html.await_args.args[0]
    assert '兑换成功' in body
    assert '积分测试群' in body
    assert '测试商品' in body
    assert '请私信 @shop_admin 领取' in body
    assert '@alice' in body
    assert 'Alice Li' in body
    assert 'ID: <code>101</code>' in body
    assert '消耗积分' in body
    # Receipt must not be scheduled for auto-delete (no third arg / no _sched_del call through reply)
    env.routes._update_exchange_catalog_async.assert_awaited()
    env.routes._delete_item_announcement_async.assert_not_awaited()

    with env.app.app_context():
        rec = PointsExchangeRecord.query.filter_by(group_id=env.gid, user_id=101).one()
        assert rec.user_username == 'alice'
        assert rec.user_display_name == 'Alice Li'
        assert rec.item_name_snapshot == '测试商品'
        assert UserPoints.query.filter_by(group_id=env.gid, user_id=101).one().points_balance == 70
        assert not hasattr(PointsExchangeRecord, 'redeem_code') or getattr(rec, 'redeem_code', None) is None
        assert not hasattr(PointsExchangeRecord, 'status') or getattr(rec, 'status', None) in (None, 'pending')


@pytest.mark.asyncio
async def test_start_exchange_sold_out_updates_catalog_and_deletes_announcement(exchange_env):
    from app.models import PointsExchangeItem
    env = exchange_env
    with env.app.app_context():
        item = env.db.session.get(PointsExchangeItem, env.iid)
        item.stock = 1
        env.db.session.commit()
    update, context, message = _start_update()
    context.args = [f'exchange_{env.gid}_{env.iid}']
    await env.routes.cmd_start(update, context)
    message.reply_html.assert_awaited()
    assert '兑换成功' in message.reply_html.await_args.args[0]
    env.routes._delete_item_announcement_async.assert_awaited()
    env.routes._update_exchange_catalog_async.assert_awaited()
    with env.app.app_context():
        item = env.db.session.get(PointsExchangeItem, env.iid)
        assert item.stock == 0
        assert item.is_active is False


@pytest.mark.asyncio
async def test_start_exchange_rejects_wrong_clone(exchange_env):
    from app import db
    from app.models import BotGroup, PointsExchangeItem, UserPoints, PointsExchangeRecord
    env = exchange_env
    with env.app.app_context():
        clone_group = BotGroup(
            chat_id=_unique_chat(),
            title='克隆群',
            type='supergroup',
            is_active=True,
            clone_id=13,
            config=json.dumps({'points_shop_enabled': True}, ensure_ascii=False),
        )
        db.session.add(clone_group)
        db.session.flush()
        item = PointsExchangeItem(group_id=clone_group.id, item_name='克隆商品', points_cost=5, stock=3, is_active=True)
        db.session.add(item)
        db.session.add(UserPoints(group_id=clone_group.id, user_id=101, points_balance=50))
        db.session.commit()
        clone_gid, clone_iid = clone_group.id, item.id

    try:
        # Main bot (clone_id=None) must reject a clone group's deep link
        update, context, message = _start_update(clone_id=None)
        context.args = [f'exchange_{clone_gid}_{clone_iid}']
        await env.routes.cmd_start(update, context)
        body = message.reply_html.await_args.args[0]
        assert '不属于本机器人' in body
        with env.app.app_context():
            assert PointsExchangeRecord.query.filter_by(group_id=clone_gid).count() == 0
            assert UserPoints.query.filter_by(group_id=clone_gid, user_id=101).one().points_balance == 50

        # Clone bot must reject the main group's deep link
        update, context, message = _start_update(clone_id=13)
        context.args = [f'exchange_{env.gid}_{env.iid}']
        await env.routes.cmd_start(update, context)
        assert '不属于本机器人' in message.reply_html.await_args.args[0]
        with env.app.app_context():
            assert PointsExchangeRecord.query.filter_by(group_id=env.gid).count() == 0
    finally:
        with env.app.app_context():
            UserPoints.query.filter_by(group_id=clone_gid).delete()
            PointsExchangeItem.query.filter_by(group_id=clone_gid).delete()
            BotGroup.query.filter_by(id=clone_gid).delete()
            db.session.commit()


@pytest.mark.asyncio
async def test_start_exchange_does_not_send_admin_welcome(exchange_env, monkeypatch):
    env = exchange_env
    monkeypatch.setenv('ADMIN_ID', '101')
    sched = MagicMock()
    monkeypatch.setattr(env.routes, '_sched_del', sched)
    update, context, message = _start_update(_tg_user(101))
    context.args = [f'exchange_{env.gid}_{env.iid}']
    await env.routes.cmd_start(update, context)
    body = message.reply_html.await_args.args[0]
    assert '兑换成功' in body
    assert '管理员' not in body
    sched.assert_not_called()


# ---------------------------------------------------------------------------
# Legacy callback (old channel posts)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_callback_redeems_sends_dm_and_survives_dm_failure(exchange_env, caplog):
    from app.models import PointsExchangeRecord
    env = exchange_env
    user = _tg_user(env.uid)
    query = NS(
        data=f'exchange_redeem_{env.iid}_{env.gid}',
        answer=AsyncMock(),
    )
    bot = NS(send_message=AsyncMock(), username='juice_bot')
    update = NS(callback_query=query, effective_user=user)
    context = NS(bot=bot, application=NS(bot_data={}, bot=bot))

    await env.routes.exchange_item_callback(update, context)
    query.answer.assert_awaited()
    alert = query.answer.await_args.args[0]
    assert len(alert) <= 200
    assert '兑换成功' in alert
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs['chat_id'] == env.uid
    assert bot.send_message.await_args.kwargs['parse_mode'] == 'HTML'
    voucher = bot.send_message.await_args.kwargs['text']
    assert '兑换成功' in voucher
    assert '测试商品' in voucher
    # Only the redeeming user is messaged — no admin notification
    assert bot.send_message.await_count == 1

    with env.app.app_context():
        rec = PointsExchangeRecord.query.filter_by(group_id=env.gid, user_id=env.uid).one()
        assert rec.user_username == 'alice'
        assert rec.item_name_snapshot == '测试商品'

    # DM failure must not roll back a second redemption after we top up points
    with env.app.app_context():
        from app.models import UserPoints
        UserPoints.query.filter_by(group_id=env.gid, user_id=env.uid).one().points_balance = 100
        env.db.session.commit()
    bot.send_message = AsyncMock(side_effect=Forbidden('bot can\'t initiate conversation'))
    context.bot = bot
    query.answer = AsyncMock()
    with caplog.at_level(logging.WARNING):
        await env.routes.exchange_item_callback(update, context)
    query.answer.assert_awaited()
    assert '兑换成功' in query.answer.await_args.args[0]
    with env.app.app_context():
        assert PointsExchangeRecord.query.filter_by(group_id=env.gid, user_id=env.uid).count() == 2


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------

def test_save_default_redemption_info(exchange_env, client):
    env = exchange_env
    _login(client)
    resp = client.post('/core/api/save_exchange_channel', json={
        'group_id': env.gid,
        'exchange_channel_id': '-1001',
        'exchange_command_word': '兑换',
        'exchange_default_redemption_info': '请到前台领取',
    })
    assert resp.get_json()['status'] == 'ok'
    with env.app.app_context():
        from app.models import BotGroup
        conf = env.routes.get_group_conf(env.db.session.get(BotGroup, env.gid))
        assert conf['exchange_default_redemption_info'] == '请到前台领取'
        assert conf['exchange_channel_id'] == '-1001'


def test_admin_records_show_snapshots_legacy_fallback_and_search(exchange_env, client):
    from app.models import GroupMember, PointsExchangeItem, PointsExchangeRecord
    env = exchange_env
    with env.app.app_context():
        rec_new = PointsExchangeRecord(
            group_id=env.gid, user_id=201, item_id=env.iid, points_spent=30,
            user_username='carol', user_display_name='Carol Wang',
            item_name_snapshot='测试商品',
            created_at=env.routes.get_beijing_now(),
        )
        rec_legacy = PointsExchangeRecord(
            group_id=env.gid, user_id=202, item_id=env.iid, points_spent=30,
            created_at=env.routes.get_beijing_now(),
        )
        rec_deleted = PointsExchangeRecord(
            group_id=env.gid, user_id=203, item_id=None, points_spent=15,
            user_username='dave', user_display_name='Dave',
            item_name_snapshot='已下架礼包',
            created_at=env.routes.get_beijing_now(),
        )
        env.db.session.add_all([rec_new, rec_legacy, rec_deleted])
        env.db.session.add(GroupMember(
            group_id=env.gid, user_id=202, username='legacy_user',
            first_name='Old', last_name='Member',
        ))
        env.db.session.commit()

    _login(client)
    html = client.get(f'/core/group/{env.gid}/points_shop').get_data(as_text=True)
    assert 'Carol Wang' in html
    assert '@carol' in html
    assert 'ID: 201' in html
    assert 'Old Member' in html
    assert '@legacy_user' in html
    assert 'ID: 202' in html
    assert '已下架礼包' in html
    assert '默认领取说明' in html
    assert 'redeem_code' not in html
    assert '标记已核销' not in html
    assert '已核销' not in html

    by_id = client.get(f'/core/group/{env.gid}/points_shop?q=201').get_data(as_text=True)
    assert 'Carol Wang' in by_id
    assert 'Old Member' not in by_id

    by_name = client.get(f'/core/group/{env.gid}/points_shop?q=legacy_user').get_data(as_text=True)
    assert 'Old Member' in by_name
    assert 'Carol Wang' not in by_name
    by_item = client.get(f'/core/group/{env.gid}/points_shop?q=已下架礼包').get_data(as_text=True)
    assert '已下架礼包' in by_item
    assert 'Carol Wang' not in by_item

    by_uid = client.get(f'/core/group/{env.gid}/points_shop?q=202').get_data(as_text=True)
    assert 'Old Member' in by_uid
    assert 'Carol Wang' not in by_uid


def test_admin_shop_page_is_clone_isolated(exchange_env, client):
    env = exchange_env
    _login(client, clone_id=99)
    resp = client.get(f'/core/group/{env.gid}/points_shop')
    assert resp.status_code == 403


def test_no_fulfillment_api_or_redeem_code_column():
    from app.models import PointsExchangeRecord
    from app.modules.core import routes
    assert not hasattr(PointsExchangeRecord, 'redeem_code')
    assert not hasattr(PointsExchangeRecord, 'fulfilled_at')
    assert not hasattr(PointsExchangeRecord, 'fulfilled_by')
    assert not hasattr(routes, 'api_update_exchange_record_status')
