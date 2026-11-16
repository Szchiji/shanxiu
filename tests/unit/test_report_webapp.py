"""Unit tests for report Mini App security, sessions, visibility and admin list."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytest

from app.modules.report.web_security import (
    SecurityError,
    build_webapp_url,
    https_origin_from_url,
    mint_start_token,
    parse_start_token,
    public_https_origin_for_bot,
    sniff_image,
    validate_init_data,
)
from app.modules.report import web_service
from app.modules.report import service as report_service


# Public example from the unofficial Mini Apps docs; used only as a known HMAC vector.
_DOCS_TOKEN = '5768337691:AAH5YkoiEuPk8-FZa32hStHTqXiLPtAEhx8'
_DOCS_AUTH = 1662771648
_DOCS_USER = (
    '{"id":279058397,"first_name":"Vladislav","last_name":"Kibenko",'
    '"username":"vdkfrost","language_code":"ru","is_premium":true}'
)
_DOCS_HASH = 'c501b71e775f74ce10e377dea85a7ea24ecd640b223ea86dfe453e0eaed2e2b2'


def _init_data(bot_token, fields: dict) -> str:
    pairs = list(fields.items())
    data_check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs) if k != 'hash')
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, 'hash': digest})


class TestInitDataHmac:
    def test_official_example_vector(self):
        raw = urlencode({
            'auth_date': str(_DOCS_AUTH),
            'query_id': 'AAHdF6IQAAAAAN0XohDhrOrc',
            'user': _DOCS_USER,
            'hash': _DOCS_HASH,
        })
        parsed = validate_init_data(raw, _DOCS_TOKEN, now=_DOCS_AUTH, max_age=60)
        assert parsed['user']['id'] == 279058397

    def test_rejects_wrong_bot_token(self):
        raw = _init_data('token-a', {
            'auth_date': '1000',
            'user': json.dumps({'id': 1, 'first_name': 'A'}),
        })
        with pytest.raises(SecurityError) as exc:
            validate_init_data(raw, 'token-b', now=1000, max_age=60)
        assert exc.value.code == 'bad_init'

    def test_rejects_expired_and_future(self):
        now = 2_000_000
        raw = _init_data('tok', {
            'auth_date': str(now - 1000),
            'user': json.dumps({'id': 7}),
        })
        with pytest.raises(SecurityError) as exc:
            validate_init_data(raw, 'tok', now=now, max_age=60)
        assert exc.value.code == 'init_expired'
        future = _init_data('tok', {
            'auth_date': str(now + 3600),
            'user': json.dumps({'id': 7}),
        })
        with pytest.raises(SecurityError):
            validate_init_data(future, 'tok', now=now, max_age=86400)

    def test_rejects_missing_user_and_empty(self):
        raw = _init_data('tok', {'auth_date': '1000'})
        with pytest.raises(SecurityError) as exc:
            validate_init_data(raw, 'tok', now=1000, max_age=60)
        assert exc.value.code == 'no_user'
        with pytest.raises(SecurityError) as exc2:
            validate_init_data('', 'tok', now=1000)
        assert exc2.value.code == 'no_init'


class TestStartTokenAndOrigin:
    def test_roundtrip_and_tamper(self, monkeypatch):
        monkeypatch.setenv('SECRET_KEY', 'unit-secret')
        token = mint_start_token(mode='write', target_user_id=42, clone_id=6, now=1000, max_age=100)
        parsed = parse_start_token(token, now=1050)
        assert parsed['mode'] == 'write'
        assert parsed['target_user_id'] == 42
        assert parsed['clone_id'] == 6
        with pytest.raises(SecurityError):
            parse_start_token(token[:-2] + 'ab', now=1050)
        with pytest.raises(SecurityError) as exc:
            parse_start_token(token, now=2000)
        assert exc.value.code == 'start_expired'

    def test_https_origins_only(self):
        assert https_origin_from_url('https://sanxiu.one/core/clone_webhook/6') == 'https://sanxiu.one'
        assert https_origin_from_url('http://sanxiu.one/core/webhook') is None
        assert https_origin_from_url('https://localhost/core/webhook') is None
        assert public_https_origin_for_bot(
            6, webhook_url='https://sanxiu.one/core/clone_webhook/6', railway_domain='ignored.example'
        ) == 'https://sanxiu.one'
        assert public_https_origin_for_bot(
            None, webhook_url=None, railway_domain='daka-production.up.railway.app'
        ) == 'https://daka-production.up.railway.app'
        assert build_webapp_url('https://sanxiu.one', 'abc.def') == 'https://sanxiu.one/report/app?s=abc.def'
        assert build_webapp_url('http://sanxiu.one', 'abc.def') is None


class TestImageSniff:
    def test_accepts_real_headers_rejects_fake(self):
        import io
        from PIL import Image
        for fmt in ('JPEG', 'PNG', 'GIF', 'WEBP'):
            buf = io.BytesIO()
            Image.new('RGB', (24, 24), 'red').save(buf, format=fmt)
            assert sniff_image(buf.getvalue())[0] in ('image/jpeg', 'image/png')
        for fake in (b'\xff\xd8\xff' + b'x' * 20,
                     b'\x89PNG\r\n\x1a\n' + b'x' * 20,
                     b'GIF89a' + b'x' * 20, b'not-an-image'):
            assert sniff_image(fake) is None



@pytest.fixture
def web_app(flask_app, monkeypatch):
    from app import db
    from app.models import (
        UserReport, ReportWebSession, ReportWebUpload, BotGroup, SystemConfig,
    )
    monkeypatch.setenv('SECRET_KEY', 'test-secret-key')
    monkeypatch.setenv('TG_BOT_TOKEN', 'main-bot-token')
    existing_reports = {r.id for r in UserReport.query.all()}
    existing_sessions = {r.id for r in ReportWebSession.query.all()}
    existing_uploads = {r.id for r in ReportWebUpload.query.all()}
    yield flask_app, db
    for model, existing in (
        (ReportWebUpload, existing_uploads),
        (ReportWebSession, existing_sessions),
        (UserReport, existing_reports),
    ):
        for row in model.query.all():
            if row.id not in existing:
                db.session.delete(row)
    db.session.commit()


def _session_row(db, **kw):
    from app.models import ReportWebSession
    defaults = dict(
        telegram_user_id=9, clone_id=None, target_user_id=42, mode='write',
    )
    defaults.update(kw)
    created = web_service.create_web_session(db, ReportWebSession, **defaults)
    return created


class TestWebSessionsAndSubmit:
    def test_csrf_and_expired_session(self, web_app):
        app, db = web_app
        from app.models import ReportWebSession
        created = _session_row(db)
        row = created['session']
        web_service.require_csrf(row, created['csrf'])
        with pytest.raises(SecurityError) as exc:
            web_service.require_csrf(row, 'wrong')
        assert exc.value.code == 'bad_csrf'
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        with pytest.raises(SecurityError) as exc2:
            web_service.load_valid_session(ReportWebSession, created['token'])
        assert exc2.value.code == 'no_session'

    def test_submit_idempotent_and_view_hides_private(self, web_app):
        app, db = web_app
        from app.models import UserReport, SystemConfig, GroupMember, GroupUser, BotGroup, ReportWebSession
        created = _session_row(db, mode='write', target_user_id=42, telegram_user_id=9)
        from app.models import ReportWebUpload
        kwargs = dict(session_id=created['session'].id, submit_key=created['submit_key'],
                      answers=[{'question': 'q', 'answer': 't'}], raw_answers=['t', 'd', 'r'])
        state, r, _ = web_service.commit_web_submission(
            db, ReportWebSession, UserReport, ReportWebUpload, **kwargs)
        assert state == 'ok'
        again, repeat, _ = web_service.commit_web_submission(
            db, ReportWebSession, UserReport, ReportWebUpload, **kwargs)
        assert again == 'duplicate' and repeat.id == r.id
        with pytest.raises(SecurityError):
            web_service.commit_web_submission(
                db, ReportWebSession, UserReport, ReportWebUpload, **{**kwargs, 'submit_key': 'other-key'})

        # View session should only see approved owned reports; not this pending one,
        # not a rejected one, not a legacy row, and never a submitter field.
        pending = r
        approved = UserReport(**report_service.new_report_values(
            user_id=42, submitter_id=9, raw_answers=['ok'],
            answers=[{'question': 'q', 'answer': 'ok'}],
            photo_file_id=None, bot_clone_id=None,
        ))
        approved.status = 'approved'
        rejected = UserReport(**report_service.new_report_values(
            user_id=42, submitter_id=9, raw_answers=['no'],
            answers=[{'question': 'q', 'answer': 'no'}],
            photo_file_id=None, bot_clone_id=None,
        ))
        rejected.status = 'rejected'
        legacy = UserReport(
            user_id=42, submitter_id=9, fault_time='old', status='approved',
            owner_known=False, clone_id=None,
        )
        clone_owned = UserReport(**report_service.new_report_values(
            user_id=42, submitter_id=9, raw_answers=['c'],
            answers=[{'question': 'q', 'answer': 'c'}],
            photo_file_id=None, bot_clone_id=4,
        ))
        clone_owned.status = 'approved'
        db.session.add_all([approved, rejected, legacy, clone_owned])
        db.session.commit()

        view = web_service.create_web_session(
            db, ReportWebSession,
            telegram_user_id=9, clone_id=None, target_user_id=42, mode='view',
        )
        payload = web_service.view_payload_for_session(
            UserReport, SystemConfig, GroupMember, GroupUser, BotGroup, view['session'],
        )
        ids = {item['id'] for item in payload['reports']}
        assert approved.id in ids
        assert pending.id not in ids
        assert rejected.id not in ids
        assert legacy.id not in ids
        assert clone_owned.id not in ids
        assert all('submitter' not in item and 'photo' not in item for item in payload['reports'])


class TestAdminListIsolation:
    def test_hides_legacy_and_other_clones(self, web_app):
        app, db = web_app
        from app.models import UserReport
        main = UserReport(**report_service.new_report_values(
            user_id=1, submitter_id=2, raw_answers=['a'],
            answers=[{'question': 'q', 'answer': 'a'}],
            photo_file_id=None, bot_clone_id=None,
        ))
        main.status = 'pending'
        clone = UserReport(**report_service.new_report_values(
            user_id=1, submitter_id=2, raw_answers=['b'],
            answers=[{'question': 'q', 'answer': 'b'}],
            photo_file_id=None, bot_clone_id=5,
        ))
        legacy = UserReport(
            user_id=1, submitter_id=2, fault_time='old', status='approved',
            owner_known=False, clone_id=None,
        )
        db.session.add_all([main, clone, legacy])
        db.session.commit()
        main_ids = {r.id for r in web_service.admin_reports_query(UserReport, clone_id=None).all()}
        clone_ids = {r.id for r in web_service.admin_reports_query(UserReport, clone_id=5).all()}
        assert main.id in main_ids and clone.id not in main_ids and legacy.id not in main_ids
        assert clone.id in clone_ids and main.id not in clone_ids and legacy.id not in clone_ids
        assert web_service.admin_may_view_report(main, None)
        assert not web_service.admin_may_view_report(legacy, None)
        assert not web_service.admin_may_view_report(clone, None)


class TestSchemaAndHttpGate:
    def test_web_schema_idempotent(self):
        from sqlalchemy import create_engine, text
        engine = create_engine('sqlite:///:memory:')
        class _DB:
            def __init__(self, engine):
                self.engine = engine
        applied = web_service.ensure_report_web_schema(_DB(engine))
        assert applied
        web_service.ensure_report_web_schema(_DB(engine))
        with engine.connect() as conn:
            tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert 'report_web_sessions' in tables
            assert 'report_web_uploads' in tables

    def test_direct_page_has_no_secrets_and_api_requires_session(self, web_app):
        app, db = web_app
        client = app.test_client()
        page = client.get('/report/app')
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        assert '请从机器人打开' in html
        assert 'main-bot-token' not in html
        assert 'TG_BOT_TOKEN' not in html
        assert 'initDataUnsafe' not in html
        denied = client.get('/report/api/bootstrap')
        assert denied.status_code == 403
        created = client.post('/report/api/session', json={'initData': '', 'startToken': 'x'})
        body = created.get_json()
        assert created.status_code == 403
        assert body['ok'] is False


def test_submit_transaction_interrupted_then_retry(web_app, monkeypatch):
    app, db = web_app
    from app.models import ReportWebSession, ReportWebUpload, UserReport
    created = _session_row(db, target_user_id=982772)
    sid = created['session'].id
    count = UserReport.query.count()
    actual_commit = db.session.commit
    def fail_commit():
        raise RuntimeError('simulated crash before commit')
    monkeypatch.setattr(db.session, 'commit', fail_commit)
    kwargs = dict(session_id=sid, submit_key=created['submit_key'],
                  answers=[{'question': 'q', 'answer': 'a'}], raw_answers=['a'])
    with pytest.raises(RuntimeError):
        web_service.commit_web_submission(db, ReportWebSession, UserReport, ReportWebUpload, **kwargs)
    monkeypatch.setattr(db.session, 'commit', actual_commit)
    assert UserReport.query.count() == count
    assert db.session.get(ReportWebSession, sid).submitted_report_id is None
    status, report, _ = web_service.commit_web_submission(
        db, ReportWebSession, UserReport, ReportWebUpload, **kwargs)
    assert status == 'ok'
    rid = report.id
    status, report, _ = web_service.commit_web_submission(
        db, ReportWebSession, UserReport, ReportWebUpload, **kwargs)
    assert status == 'duplicate' and report.id == rid
    assert UserReport.query.count() == count + 1


def test_init_duplicate_and_invalid_input_rejected():
    raw = _init_data('tok', {'auth_date': str(int(time.time())), 'user': json.dumps({'id': 1})})
    for data in (raw + '&user=%7B%22id%22%3A2%7D', 'x' * 8193, [], {}):
        with pytest.raises(SecurityError):
            validate_init_data(data, 'tok')


def test_shanghai_day_bounds():
    start, end = web_service.shanghai_day_range_to_utc('2026-11-16', '2026-11-16')
    assert start == datetime(2026, 11, 15, 16)
    assert end == datetime(2026, 11, 16, 15, 59, 59, 999999)
    assert web_service.format_shanghai(start) == '2026-11-16 00:00（北京时间）'
    assert web_service.format_shanghai(start, style='short', with_label=False) == '11-16 00:00'


def test_real_concurrent_sessions_one_report(tmp_path):
    """Two real SQLAlchemy connections submit the same session simultaneously."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import ReportWebSession, ReportWebUpload, UserReport
    from app.modules.report.web_security import sha256_hex
    engine = create_engine(f'sqlite:///{tmp_path / "concurrent.db"}',
                           connect_args={'timeout': 10})
    for table in (ReportWebSession.__table__, ReportWebUpload.__table__, UserReport.__table__):
        table.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as sess:
        row = ReportWebSession(token_hash=sha256_hex('tok'), telegram_user_id=5,
                               target_user_id=8, clone_id=None, mode='write',
                               csrf_hash=sha256_hex('csrf'), submit_key_hash=sha256_hex('submit'),
                               expires_at=datetime.utcnow() + timedelta(hours=1))
        sess.add(row)
        sess.commit()
        sid = row.id
    barrier = threading.Barrier(2)
    def worker():
        with factory() as sess:
            # Deliberately populate a stale identity map before taking the lock.
            stale = sess.get(ReportWebSession, sid)
            assert stale.submitted_report_id is None
            barrier.wait(timeout=5)
            status, report, _ = web_service.commit_web_submission(
                SimpleNamespace(engine=engine, session=sess), ReportWebSession,
                UserReport, ReportWebUpload, session_id=sid, submit_key='submit',
                answers=[{'question': 'q', 'answer': 'a'}], raw_answers=['a'])
            return status, report.id
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        assert sorted(item[0] for item in results) == ['duplicate', 'ok']
        assert len({item[1] for item in results}) == 1
        with factory() as sess:
            assert sess.query(UserReport).count() == 1
            assert sess.get(ReportWebSession, sid).submitted_report_id == results[0][1]
    finally:
        engine.dispose()


@pytest.mark.parametrize('clone_mode', [False, True])
def test_http_session_upload_submit_review_binding_and_view(web_app, monkeypatch, clone_mode):
    """Real Flask endpoints, ORM, Pillow and running asyncio loop; only TG I/O fake."""
    import asyncio
    import io
    import threading
    from types import SimpleNamespace
    from PIL import Image
    from app import bot_clone_manager, limiter
    from app.models import BotClone, UserReport, ReportWebUpload, SystemConfig
    from app.modules.core import routes as core_routes
    monkeypatch.setattr(limiter, 'enabled', False)
    monkeypatch.setenv('RAILWAY_PUBLIC_DOMAIN', 'sanxiu.one')
    app, db = web_app
    clone = None
    clone_id = None
    token = 'main-bot-token'
    if clone_mode:
        clone = BotClone(clone_name='web-test', bot_token='clone-test-token',
                         webhook_url='https://sanxiu.one/core/clone_webhook/1')
        db.session.add(clone)
        db.session.commit()
        clone_id = clone.id
        token = 'clone-test-token'
    config_key = report_service.scoped_key('admin_group_id', clone_id)
    previous = SystemConfig.get_value(config_key, '')
    SystemConfig.set_value(config_key, '-100123456')
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    def run_loop():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
    thread = threading.Thread(target=run_loop)
    thread.start()
    assert ready.wait(3)
    calls = []
    entries = []
    class FakeTelegram:
        async def delete_message(self, **kwargs):
            return True
        async def send_message(self, **kwargs):
            entries.append(kwargs)
            return SimpleNamespace(message_id=501)
        async def send_photo(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=881, photo=[SimpleNamespace(file_id='same-bot-file-id')])
    runtime_app = SimpleNamespace(bot=FakeTelegram(), running=True)
    monkeypatch.setattr(core_routes, 'global_bot_loop', loop)
    if clone_mode:
        monkeypatch.setitem(bot_clone_manager.active_clones, clone_id, {'app': runtime_app})
    else:
        monkeypatch.setattr(core_routes, 'global_ptb_app', runtime_app)
    client = app.test_client()
    def make_session(mode):
        raw = _init_data(token, {'auth_date': str(int(time.time())), 'user': json.dumps({'id': 19})})
        from app.modules.report import bot as report_bot
        from urllib.parse import parse_qs, urlparse
        async def reply_text(text, **kwargs):
            entries.append({'text': text, **kwargs})
        message = SimpleNamespace(text=f'/start@ReportBot {"report" if mode == "write" else "view"}_887713',
                                  message_id=500, reply_text=reply_text)
        update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=19))
        context = SimpleNamespace(bot=runtime_app.bot, user_data={},
                                  application=SimpleNamespace(bot_data={'flask_app': app, 'clone_id': clone_id}))
        handler = report_bot.report_start if mode == 'write' else report_bot.view_reports
        from concurrent.futures import ThreadPoolExecutor
        # Real bot callbacks have no inherited Flask request/app context.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: asyncio.run(handler(update, context))).result(timeout=5)
        markup = entries[-1]['reply_markup']
        start = parse_qs(urlparse(markup.inline_keyboard[0][0].web_app.url).query)['s'][0]
        assert parse_start_token(start)['clone_id'] == clone_id
        response = client.post('/report/api/session', json={'initData': raw, 'startToken': start})
        assert response.status_code == 200, response.get_json()
        session = response.get_json()
        headers = {'Authorization': 'Bearer ' + session['token'], 'X-CSRF-Token': session['csrf']}
        return session, headers
    try:
        session, headers = make_session('write')
        bootstrap = client.get('/report/api/bootstrap', headers=headers).get_json()
        assert bootstrap['target_user_id'] == 887713
        image = io.BytesIO()
        Image.new('RGB', (32, 32), 'blue').save(image, 'PNG')
        image.seek(0)
        upload = client.post('/report/api/upload', headers=headers,
                             data={'photo': (image, 'test.png')}).get_json()
        assert upload['ok']
        payload = {'submit_key': session['submit_key'], 'answers': ['正常'] * len(bootstrap['questions']),
                   'upload_token': upload['upload_token']}
        result = client.post('/report/api/submit', headers=headers, json=payload)
        assert result.status_code == 200, result.get_json()
        data = result.get_json()
        assert data['notified'], data
        report = db.session.get(UserReport, data['report_id'])
        assert report.clone_id == clone_id and report.owner_known
        assert report.admin_message_id == 881 and report.admin_chat_id == '-100123456'
        assert report.photo_file_id == 'same-bot-file-id'
        assert report.notify_status == 'notified'
        assert not ReportWebUpload.query.filter_by(report_id=report.id).count()
        again = client.post('/report/api/submit', headers=headers, json=payload).get_json()
        assert again['duplicate'] and again['report_id'] == report.id
        assert len(calls) == 1
        _, view_headers = make_session('view')
        assert not client.get('/report/api/bootstrap', headers=view_headers).get_json()['reports']
        report.status = 'approved'
        report.channel_chat_id = '@reports_test'
        report.channel_msg_id = 42
        db.session.commit()
        view = client.get('/report/api/bootstrap', headers=view_headers).get_json()
        item = next(item for item in view['reports'] if item['id'] == report.id)
        assert 'answers' not in item and 'channel_link' not in item
        detail = client.get(f'/report/api/reports/{report.id}', headers=view_headers).get_json()
        assert detail['report']['channel_link'] == 'https://t.me/reports_test/42'
        assert item['date'] == web_service.format_shanghai(
            report.created_at, style='short', with_label=False)
        assert view['timezone'] == '北京时间'
        assert view['sort'] == 'newest'
        assert 'submitter' not in view and '投稿者' not in item.get('title', '')
        assert 'submitter' not in item and 'photo_file_id' not in item
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
        loop.close()
        SystemConfig.set_value(config_key, previous)
        if clone is not None:
            db.session.delete(clone)
            db.session.commit()


def test_upload_request_limit_real_multipart_and_normalization(web_app, monkeypatch):
    import io
    from PIL import Image
    from app import limiter
    from app.models import ReportWebUpload
    from app.modules.report.web_security import MAX_UPLOAD_BYTES, MAX_UPLOAD_REQUEST_BYTES
    app, db = web_app
    monkeypatch.setattr(limiter, 'enabled', False)
    monkeypatch.setitem(app.config, 'MAX_CONTENT_LENGTH', 1024)
    created = _session_row(db)
    headers = {'Authorization': 'Bearer ' + created['token'], 'X-CSRF-Token': created['csrf']}
    image = io.BytesIO()
    Image.new('RGB', (32, 32), 'red').save(image, format='PNG')
    data = image.getvalue()
    client = app.test_client()
    exact = data + b'\0' * (MAX_UPLOAD_BYTES - len(data))
    response = client.post('/report/api/upload', headers=headers,
                           data={'photo': (io.BytesIO(exact), 'image.png')})
    assert response.status_code == 200, response.get_json()
    blob = ReportWebUpload.query.filter_by(session_id=created['session'].id).one()
    assert blob.content_type == 'image/jpeg' and len(blob.data) < len(exact)
    Image.open(io.BytesIO(blob.data)).verify()
    response = client.post('/report/api/upload', headers=headers,
                           data={'photo': (io.BytesIO(exact + b'x'), 'large.png')})
    assert response.status_code == 400
    response = client.post('/report/api/upload', headers=headers,
                           data={'photo': (io.BytesIO(b'x' * MAX_UPLOAD_REQUEST_BYTES), 'large.png')})
    assert response.status_code == 413 and response.get_json()['code'] == 'too_large'


@pytest.mark.parametrize('failure', ['not_ready', 'rejected', 'unknown'])
def test_delivery_failure_keeps_attachment_and_safe_retry(web_app, monkeypatch, failure):
    import asyncio
    import threading
    from types import SimpleNamespace
    from telegram.error import BadRequest
    from app.models import UserReport, ReportWebUpload, ReportWebSession, SystemConfig
    from app.modules.report.web_routes import retry_report_notification
    from app.modules.core import routes as core_routes
    app, db = web_app
    created = _session_row(db)
    # Store a real normalized image and bind it in the same report transaction.
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (20, 20), 'red').save(buf, 'PNG')
    upload = web_service.store_upload(db, ReportWebUpload, created['session'], buf.getvalue())
    blob = web_service.load_upload(ReportWebUpload, created['session'], upload['upload_token'])
    _, report, _ = web_service.commit_web_submission(
        db, ReportWebSession, UserReport, ReportWebUpload,
        session_id=created['session'].id, submit_key=created['submit_key'],
        answers=[{'question': 'q', 'answer': 'a'}], raw_answers=['a'], upload=blob)
    old_admin = SystemConfig.get_value('admin_group_id', '')
    SystemConfig.set_value('admin_group_id', '-100123456')
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(3)
    calls = []
    class TelegramStub:
        fail = failure
        async def send_photo(self, **kwargs):
            calls.append(kwargs)
            if self.fail == 'rejected':
                raise BadRequest('test rejection')
            if self.fail == 'unknown':
                raise TimeoutError('test ambiguous delivery')
            return SimpleNamespace(message_id=771, photo=[SimpleNamespace(file_id='persisted-photo')])
    bot = TelegramStub()
    runtime = SimpleNamespace(bot=bot, running=(failure != 'not_ready'))
    monkeypatch.setattr(core_routes, 'global_ptb_app', runtime)
    monkeypatch.setattr(core_routes, 'global_bot_loop', loop)
    try:
        ok, reason = retry_report_notification(report)
        assert not ok
        db.session.refresh(report)
        assert ReportWebUpload.query.filter_by(report_id=report.id).count() == 1
        created['session'].expires_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        web_service.purge_expired_web_state(db, ReportWebSession, ReportWebUpload)
        assert ReportWebUpload.query.filter_by(report_id=report.id).count() == 1
        runtime.running = True
        bot.fail = None
        ok, _ = retry_report_notification(report)
        if failure == 'unknown':
            assert not ok and len(calls) == 1
            assert report.notify_status == 'unknown'
            assert ReportWebUpload.query.filter_by(report_id=report.id).count() == 1
        else:
            assert ok
            db.session.refresh(report)
            assert report.photo_file_id == 'persisted-photo' and report.admin_message_id == 771
            assert ReportWebUpload.query.filter_by(report_id=report.id).count() == 0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
        loop.close()
        SystemConfig.set_value('admin_group_id', old_admin)


def test_admin_retry_authorization_csrf_and_clone_scope(web_app, monkeypatch):
    from app.models import UserReport
    from app.modules.core import routes as core_routes
    app, db = web_app
    monkeypatch.setattr(core_routes, 'global_ptb_app', None)
    monkeypatch.setattr(core_routes, 'global_bot_loop', None)
    report = UserReport(**report_service.new_report_values(
        user_id=55, submitter_id=9, raw_answers=['a'], answers=[{'question': 'q', 'answer': 'a'}],
        photo_file_id=None, bot_clone_id=None))
    report.notify_status = 'pending'
    db.session.add(report)
    db.session.commit()
    client = app.test_client()
    url = f'/admin/reports/{report.id}/retry-notify'
    assert client.post(url).status_code == 302
    with client.session_transaction() as session:
        session['logged_in'] = True
        session['clone_id'] = None
        session['report_notify_csrf'] = 'test-csrf'
    assert client.post(url).status_code == 403
    with client.session_transaction() as session:
        session['clone_id'] = 999
    assert client.post(url, data={'csrf': 'test-csrf'}).status_code == 404
    with client.session_transaction() as session:
        session['clone_id'] = None
    detail = client.get(f'/admin/reports/{report.id}')
    assert detail.status_code == 200
    assert '重试通知审核群'.encode() in detail.data
    assert client.post(url, data={'csrf': 'test-csrf'}).status_code == 302
    db.session.refresh(report)
    assert report.admin_message_id is None


def test_oversized_caption_rejected_before_report_is_saved(web_app, monkeypatch):
    from app import limiter
    from app.models import UserReport, SystemConfig
    app, db = web_app
    monkeypatch.setattr(limiter, 'enabled', False)
    previous = SystemConfig.get_value('report_require_photo', 'true')
    SystemConfig.set_value('report_require_photo', 'false')
    try:
        created = _session_row(db)
        headers = {'Authorization': 'Bearer ' + created['token'], 'X-CSRF-Token': created['csrf']}
        client = app.test_client()
        bootstrap = client.get('/report/api/bootstrap', headers=headers).get_json()
        count = UserReport.query.count()
        response = client.post('/report/api/submit', headers=headers,
                               json={'submit_key': created['submit_key'],
                                     'answers': ['x' * 4000] * len(bootstrap['questions'])})
        assert response.status_code == 400 and response.get_json()['code'] == 'content_too_long'
        assert UserReport.query.count() == count
        assert client.post('/report/api/submit', headers=headers, json=[]).status_code == 400
    finally:
        SystemConfig.set_value('report_require_photo', previous)


def test_public_report_pages_are_bounded(web_app):
    from app.models import UserReport, SystemConfig, GroupMember, GroupUser, BotGroup
    app, db = web_app
    created = _session_row(db, mode='view', target_user_id=771661)
    for _ in range(25):
        report = UserReport(**report_service.new_report_values(
            user_id=771661, submitter_id=9, raw_answers=['a'], answers=[],
            photo_file_id=None, bot_clone_id=None))
        report.status = 'approved'
        db.session.add(report)
    db.session.commit()
    def page(number):
        return web_service.view_payload_for_session(
            UserReport, SystemConfig, GroupMember, GroupUser, BotGroup, created['session'], page=number)
    first, second = page(1), page(2)
    assert len(first['reports']) == 20 and first['total'] == 25 and first['has_more']
    assert len(second['reports']) == 5 and not second['has_more']
    assert {r['id'] for r in first['reports']}.isdisjoint(r['id'] for r in second['reports'])


@pytest.mark.asyncio
async def test_view_deep_link_ends_active_write_conversation(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from telegram.ext import ConversationHandler
    from app.modules.report import bot
    view = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, 'view_reports', view)
    context = SimpleNamespace(user_data={'questions': ['q'], 'answers': ['a']})
    update = SimpleNamespace(message=SimpleNamespace(text='/start@ReportBot view_123'))
    assert await bot.report_cancel(update, context) == ConversationHandler.END
    assert not context.user_data
    view.assert_awaited_once_with(update, context)


def test_empty_secret_fails_closed(monkeypatch):
    monkeypatch.delenv('SECRET_KEY', raising=False)
    # No Flask context: an absent signing key must not be replaced with b''.
    from flask import Flask
    app = Flask('empty-secret-test')
    with app.app_context():
        with pytest.raises(SecurityError) as exc:
            mint_start_token(mode='write', target_user_id=1, clone_id=None)
        assert exc.value.code == 'no_signing_key'
        app.config['SECRET_KEY'] = 'default_secret_key'
        with pytest.raises(SecurityError):
            mint_start_token(mode='write', target_user_id=1, clone_id=None)


def test_admin_filter_shanghai_midnight_boundaries(web_app):
    from app.models import UserReport
    from app.modules.report.routes import _parse_admin_filters
    app, db = web_app
    rows = []
    for timestamp in [datetime(2026, 11, 15, 15, 59, 59), datetime(2026, 11, 15, 16),
                      datetime(2026, 11, 16, 15, 59, 59, 999999), datetime(2026, 11, 16, 16)]:
        report = UserReport(**report_service.new_report_values(
            user_id=887116, submitter_id=9, raw_answers=['a'], answers=[],
            photo_file_id=None, bot_clone_id=None))
        report.created_at = timestamp
        db.session.add(report)
        rows.append(report)
    db.session.commit()
    with app.test_request_context('/admin/reports?target=887116&date_from=2026-11-16&date_to=2026-11-16'):
        ids = {row.id for row in _parse_admin_filters(None)['query'].all()}
        assert ids == {rows[1].id, rows[2].id}


def test_web_schema_upgrades_previous_uploads_without_data_loss():
    from types import SimpleNamespace
    from sqlalchemy import create_engine, text, inspect
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE report_web_uploads (id INTEGER PRIMARY KEY, session_id INTEGER, '
                          'upload_token_hash TEXT, data BLOB, content_type TEXT, size INTEGER, created_at TIMESTAMP)'))
        conn.execute(text("INSERT INTO report_web_uploads(id, data, size) VALUES(1, X'010203', 3)"))
    web_service.ensure_report_web_schema(SimpleNamespace(engine=engine))
    web_service.ensure_report_web_schema(SimpleNamespace(engine=engine))
    assert 'report_id' in {col['name'] for col in inspect(engine).get_columns('report_web_uploads')}
    with engine.connect() as conn:
        assert conn.execute(text('SELECT data, report_id FROM report_web_uploads WHERE id=1')).one() == (b'\x01\x02\x03', None)
    engine.dispose()


@pytest.mark.parametrize('denial', ['pending', 'rejected', 'legacy', 'target', 'clone', 'write', 'anonymous'])
def test_single_report_detail_scope(web_app, monkeypatch, denial):
    from app import limiter
    from app.models import UserReport
    app, db = web_app
    monkeypatch.setattr(limiter, 'enabled', False)
    report = UserReport(**report_service.new_report_values(
        user_id=66421, submitter_id=1234, raw_answers=['summary'],
        answers=[{'question': 'q', 'answer': 'private-full-answer'}],
        photo_file_id='must-not-leak', bot_clone_id=None))
    report.status = 'approved'
    if denial in ('pending', 'rejected'): report.status = denial
    if denial == 'legacy': report.owner_known = False
    if denial == 'target': report.user_id = 66422
    if denial == 'clone': report.clone_id = 999
    db.session.add(report)
    db.session.commit()
    session = _session_row(db, target_user_id=66421, mode='write' if denial == 'write' else 'view')
    headers = {} if denial == 'anonymous' else {'Authorization': 'Bearer ' + session['token']}
    response = app.test_client().get(f'/report/api/reports/{report.id}', headers=headers)
    assert response.status_code in (403, 404)
    assert b'private-full-answer' not in response.data and b'must-not-leak' not in response.data


def test_detail_list_separation_and_return_page_contract(web_app, monkeypatch):
    from app import limiter
    from app.models import UserReport
    app, db = web_app
    monkeypatch.setattr(limiter, 'enabled', False)
    report = UserReport(**report_service.new_report_values(
        user_id=77123, submitter_id=1234, raw_answers=['short summary'],
        answers=[{'question': 'q', 'answer': 'full-detail-not-in-list'}],
        photo_file_id='hidden-photo', bot_clone_id=None))
    report.status = 'approved'
    db.session.add(report)
    db.session.commit()
    session = _session_row(db, target_user_id=77123, mode='view')
    headers = {'Authorization': 'Bearer ' + session['token']}
    client = app.test_client()
    listing = client.get('/report/api/bootstrap?page=1&per_page=1', headers=headers).get_json()
    assert listing['page'] == 1 and listing['reports'][0]['id'] == report.id
    assert set(listing['reports'][0]) == {'id', 'date', 'title', 'summary'}
    assert listing['sort'] == 'newest' and listing['timezone'] == '北京时间'
    assert listing['target_contact']['user_id'] == 77123
    assert 'submitter' not in listing and 'contributor' not in listing
    detail = client.get(f'/report/api/reports/{report.id}', headers=headers).get_json()['report']
    assert detail['answers'][0]['answer'] == 'full-detail-not-in-list'
    assert set(detail) == {'id', 'date', 'title', 'answers', 'channel_link'}
    assert client.get('/report/api/bootstrap?page=1&per_page=1', headers=headers).get_json() == listing
    html = client.get('/report/app').get_data(as_text=True)
    assert 'backToReports' in html
    assert 'ReportWebAppView.renderView' in html and 'viewState.sort' in html
    assert 'textContent' in html and 'dataset.reportId' in html


@pytest.mark.parametrize('name,username,expected,link', [
    ('真实昵称', 'valid_user', '真实昵称', 'https://t.me/valid_user'),
    ('', 'valid_user', 'valid_user', 'https://t.me/valid_user'),
    ('', '', '用户987654321', 'tg://user?id=987654321'),
    ('<script>&"', '', '&lt;script&gt;&amp;&quot;', 'tg://user?id=987654321'),
    ('长' * 40, '', '长' * 23 + '…', 'tg://user?id=987654321'),
    ('名字\n另一行', 'bad"<username', '名字 另一行', 'tg://user?id=987654321'),
])
def test_compact_contact_identity(name, username, expected, link):
    from types import SimpleNamespace
    identity = report_service.compact_contact_identity(
        987654321, member=SimpleNamespace(first_name=name, last_name='', username=username))
    assert identity == f'<a href="{link}">{expected}</a> · <code>987654321</code>'
    assert '\n' not in identity and '<script>' not in identity


def test_contact_caption_preserves_template_and_puts_identities_last():
    body = '<b>管理员自定义正文</b>\n原有内容\n'
    contact = report_service.compact_contact_identity(1)
    contributor = report_service.compact_contact_identity(2)
    published = report_service.build_contact_caption(body, contact, contributor)
    review = report_service.build_contact_caption(body, contact, contributor, review_report_id=33)
    assert published == body + '\n\n联系人：' + contact + '\n投稿者：' + contributor
    assert published.count('联系人：') == 1 and published.count('投稿者：') == 1
    assert '被提交人' not in published and '提交人' not in published
    assert review.endswith(published)
    assert len(review.encode('utf-16-le')) >= len(published.encode('utf-16-le'))


def test_web_review_footer_is_scoped_and_matches_channel_builder(web_app):
    from app.models import BotGroup, GroupMember, UserReport
    from app.modules.report.web_routes import review_caption
    app, db = web_app
    groups, members = [], []
    try:
        for clone_id, prefix in ((None, 'Main'), (881, 'Clone')):
            group = BotGroup(chat_id='-19887227', clone_id=clone_id, title=prefix)
            db.session.add(group)
            db.session.flush()
            groups.append(group)
            for uid, suffix in ((71881, 'Contact'), (71882, 'Contributor')):
                member = GroupMember(group_id=group.id, user_id=uid, first_name=prefix + suffix,
                                     username=prefix.lower() + '_' + suffix.lower())
                db.session.add(member)
                members.append(member)
        db.session.commit()
        for clone_id, prefix, excluded in ((None, 'Main', 'Clone'), (881, 'Clone', 'Main')):
            report = UserReport(id=33, user_id=71881, submitter_id=71882, clone_id=clone_id,
                                answers='[{"question":"Q","answer":"A"}]')
            caption = review_caption(report)
            assert prefix + 'Contact' in caption and prefix + 'Contributor' in caption
            assert excluded + 'Contact' not in caption
            assert caption.index('💬 A') < caption.index('联系人：') < caption.index('投稿者：')
            assert '<code>71881</code>' in caption and '<code>71882</code>' in caption
            assert caption.count('联系人：') == 1
    finally:
        for member in members: db.session.delete(member)
        for group in groups: db.session.delete(group)
        db.session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('clone_id', [None, 882])
async def test_chat_submission_uses_same_contact_footer(web_app, clone_id):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.models import SystemConfig, UserReport
    from app.modules.report import bot
    app, db = web_app
    db.session.rollback()
    key = report_service.scoped_key('admin_group_id', clone_id)
    previous = SystemConfig.get_value(key, '')
    SystemConfig.set_value(key, '-100123456')
    db.session.commit()
    sent = AsyncMock(return_value=SimpleNamespace(message_id=77))
    telegram = SimpleNamespace(send_message=sent, send_photo=AsyncMock())
    target_id, submitter_id = 91891 + (clone_id or 0), 91892 + (clone_id or 0)
    query = SimpleNamespace(from_user=SimpleNamespace(id=submitter_id, first_name='投稿名', last_name='', username='author_name'),
                            get_bot=lambda: telegram, edit_message_text=AsyncMock())
    context = SimpleNamespace(application=SimpleNamespace(bot_data={'flask_app': app, 'clone_id': clone_id}),
                              user_data={bot._TARGET_KEY: target_id, 'questions': [{'text': 'Q'}], 'answers': ['A']})
    try:
        await bot._save_and_notify_from_callback(query, context, None)
        if sent.await_count != 1:
            edited = query.edit_message_text.await_count and query.edit_message_text.call_args
            raise AssertionError('send_message not called; edit=' + repr(edited) + ' admin=' + repr(SystemConfig.get_value(key, '')))
        caption = sent.call_args.kwargs['text']
        assert caption.index('💬 A') < caption.index('联系人：') < caption.index('投稿者：')
        assert f'<a href="https://t.me/author_name">投稿名</a> · <code>{submitter_id}</code>' in caption
        assert '被提交人：' not in caption and '提交人：' not in caption
    finally:
        UserReport.query.filter(UserReport.user_id == target_id).delete(synchronize_session=False)
        db.session.commit()
        SystemConfig.set_value(key, previous)
        db.session.commit()


@pytest.mark.parametrize('raw,expected', [
    ('【老师艺名】：', '老师艺名'),
    ('【所在地区】', '所在地区'),
    ('故障现象：', '故障现象'),
    ('处理结果', '处理结果'),
    ('【必填】老师艺名：', '【必填】老师艺名'),
])
def test_display_question_text_strips_outer_markup_only(raw, expected):
    assert web_service.display_question_text(raw) == expected


def test_list_title_uses_name_field_not_first_region():
    answers = [
        {'question': '【所在地区】：', 'answer': '上海'},
        {'question': '【老师艺名】：', 'answer': '小猫老师'},
        {'question': '今日情况：', 'answer': '顺利完成'},
        {'question': '备注', 'answer': ''},
    ]
    title, summary = web_service.list_title_and_summary(answers, 17)
    assert title == '小猫老师'
    assert summary == '上海\n顺利完成'
    fallback_title, fallback_summary = web_service.list_title_and_summary(
        [{'question': '【所在地区】：', 'answer': '测'},
         {'question': '情况说明', 'answer': '<b>第二行</b>'}],
        17,
    )
    assert fallback_title == '报告 #17'
    assert fallback_summary == '测\n<b>第二行</b>'
    assert web_service.list_title_and_summary([], 9) == ('报告 #9', '')


def test_view_sort_is_stable_and_toggle_keeps_return_state(web_app, monkeypatch):
    from datetime import datetime
    from app import limiter
    from app.models import UserReport
    app, db = web_app
    monkeypatch.setattr(limiter, 'enabled', False)
    stamp = datetime(2026, 11, 16, 6, 48)
    rows = []
    for answer in ('甲', '乙', '丙', '丁', '戊'):
        report = UserReport(**report_service.new_report_values(
            user_id=64517, submitter_id=11, raw_answers=[answer],
            answers=[{'question': '【所在地区】：', 'answer': '杭州'},
                     {'question': '老师艺名', 'answer': answer}],
            photo_file_id=None, bot_clone_id=None))
        report.status = 'approved'
        report.created_at = stamp
        db.session.add(report)
        rows.append(report)
    db.session.commit()
    ids = [row.id for row in rows]
    session = _session_row(db, target_user_id=64517, mode='view')
    headers = {'Authorization': 'Bearer ' + session['token']}
    client = app.test_client()
    newest = client.get('/report/api/bootstrap?page=1&per_page=2&sort=newest', headers=headers).get_json()
    newest_p2 = client.get('/report/api/bootstrap?page=2&per_page=2&sort=newest', headers=headers).get_json()
    assert [item['id'] for item in newest['reports']] == ids[-1:-3:-1]
    assert [item['id'] for item in newest_p2['reports']] == ids[-3:-5:-1]
    assert newest['reports'][0]['title'] == '戊'
    assert newest['reports'][0]['summary'] == '杭州'
    assert newest['reports'][0]['date'] == '11-16 14:48'
    assert 'submitter' not in newest and newest['target_contact']['user_id'] == 64517
    oldest = client.get('/report/api/bootstrap?page=2&per_page=2&sort=oldest', headers=headers).get_json()
    assert oldest['sort'] == 'oldest' and oldest['page'] == 2
    assert [item['id'] for item in oldest['reports']] == ids[2:4]
    same = client.get('/report/api/bootstrap?page=2&per_page=2&sort=oldest', headers=headers).get_json()
    assert same == oldest
    ignored = client.get('/report/api/bootstrap?page=1&per_page=2&sort=unknown', headers=headers).get_json()
    assert ignored['sort'] == 'newest'
    assert [item['id'] for item in ignored['reports']] == ids[-1:-3:-1]


def test_detail_normalizes_questions_and_keeps_raw_answers(web_app, monkeypatch):
    from app import limiter
    from app.models import UserReport
    app, db = web_app
    monkeypatch.setattr(limiter, 'enabled', False)
    payload = '<img src=x onerror=alert(1)>\n第二行'
    report = UserReport(**report_service.new_report_values(
        user_id=64518, submitter_id=11, raw_answers=['测', payload],
        answers=[{'question': '【所在地区】：', 'answer': '测'},
                 {'question': '【情况说明】：', 'answer': payload}],
        photo_file_id=None, bot_clone_id=None))
    report.status = 'approved'
    db.session.add(report)
    db.session.commit()
    session = _session_row(db, target_user_id=64518, mode='view')
    headers = {'Authorization': 'Bearer ' + session['token']}
    detail = app.test_client().get(f'/report/api/reports/{report.id}', headers=headers).get_json()['report']
    assert [item['question'] for item in detail['answers']] == ['所在地区', '情况说明']
    assert detail['answers'][1]['answer'] == payload
    assert detail['title'] == '报告 #{}'.format(report.id)
    stored = db.session.get(UserReport, report.id)
    assert '【所在地区】：' in stored.answers


def test_report_view_js_helpers():
    import subprocess
    from pathlib import Path
    script = Path(__file__).with_name('test_report_webapp_view.js')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
