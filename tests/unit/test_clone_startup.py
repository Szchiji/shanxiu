"""Clone lifecycle retries and saved-but-not-running API regressions (no network)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from flask import Flask, session
from telegram.error import TimedOut

from app import bot_clone_manager as manager


def fake_app(failure=None, stage='initialize'):
    app = SimpleNamespace(
        running=False, initialize=AsyncMock(), shutdown=AsyncMock(),
        bot=SimpleNamespace(set_webhook=AsyncMock()),
        updater=SimpleNamespace(running=False, stop=AsyncMock(), start_polling=AsyncMock()),
    )

    async def start():
        app.running = True

    async def stop():
        app.running = False

    app.start = AsyncMock(side_effect=start)
    app.stop = AsyncMock(side_effect=stop)
    if failure:
        target = app.bot.set_webhook if stage == 'webhook' else getattr(app, stage)
        target.side_effect = failure
    return app


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setattr(manager, 'active_clones', {})
    monkeypatch.setattr(manager, 'CLONE_RETRY_DELAY', 0)
    transports = []

    def request(**kwargs):
        transport = SimpleNamespace(shutdown=AsyncMock(), kwargs=kwargs)
        transports.append(transport)
        return transport

    monkeypatch.setattr(manager, 'HTTPXRequest', request)
    builder = MagicMock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.get_updates_request.return_value = builder
    monkeypatch.setattr(manager.Application, 'builder', lambda: builder)
    return builder, transports


def start():
    return asyncio.run(manager.start_clone_bot(101, 'dummy', 'https://example.invalid/clone'))


@pytest.mark.parametrize('stage', ['initialize', 'start', 'webhook'])
def test_timeout_then_success(factory, stage):
    builder, transports = factory
    failed, good = fake_app(TimedOut(), stage), fake_app()
    builder.build.side_effect = [failed, good]
    assert start() is True
    assert builder.build.call_count == 2
    failed.shutdown.assert_awaited_once()
    if stage == 'webhook':
        failed.stop.assert_awaited_once()
    assert all(t.shutdown.await_count == 1 for t in transports[:2])
    assert all(t.shutdown.await_count == 0 for t in transports[2:])
    assert all(t.kwargs['read_timeout'] == 20 for t in transports)
    assert manager.active_clones[101]['app'] is good


@pytest.mark.parametrize('stage', ['initialize', 'start', 'webhook'])
def test_timeout_exhausted_no_leak(factory, stage):
    builder, transports = factory
    apps = [fake_app(TimedOut(), stage) for _ in range(manager.CLONE_START_ATTEMPTS)]
    builder.build.side_effect = apps
    assert start() is False
    assert builder.build.call_count == manager.CLONE_START_ATTEMPTS
    assert not manager.active_clones
    for app in apps:
        app.shutdown.assert_awaited_once()
        assert not app.running
    assert all(t.shutdown.await_count == 1 for t in transports)


def test_unexpected_error_not_retried(factory):
    builder, transports = factory
    builder.build.return_value = fake_app(ValueError('invalid configuration'))
    assert start() is False
    assert builder.build.call_count == 1
    assert not manager.active_clones
    assert all(t.shutdown.await_count == 1 for t in transports)


def test_cleanup_failure_does_not_retry(factory):
    builder, transports = factory
    app = fake_app(TimedOut(), 'webhook')
    app.stop.side_effect = RuntimeError('stop failed')
    builder.build.return_value = app
    assert start() is False
    assert builder.build.call_count == 1
    assert all(t.shutdown.await_count == 1 for t in transports)


def test_cancellation_cleans_partial_initialization(factory):
    builder, transports = factory
    app = fake_app(asyncio.CancelledError())
    builder.build.return_value = app
    with pytest.raises(asyncio.CancelledError):
        start()
    app.shutdown.assert_awaited_once()
    assert all(t.shutdown.await_count == 1 for t in transports)
    assert not manager.active_clones


def test_attempt_deadline_retries(factory, monkeypatch):
    builder, transports = factory
    monkeypatch.setattr(manager, 'CLONE_ATTEMPT_TIMEOUT', 0.001)
    app = fake_app()

    async def hang():
        await asyncio.Event().wait()

    app.initialize.side_effect = hang
    builder.build.return_value = app
    assert start() is False
    assert builder.build.call_count == manager.CLONE_START_ATTEMPTS
    assert all(t.shutdown.await_count == 1 for t in transports)


def test_polling_behavior_unchanged(factory):
    builder, _ = factory
    app = fake_app()
    builder.build.return_value = app
    assert asyncio.run(manager.start_clone_bot(101, 'dummy')) is True
    assert app.updater.start_polling.await_args.kwargs['drop_pending_updates'] is False
    assert manager.active_clones[101]['mode'] == 'polling'
    app.bot.set_webhook.assert_not_awaited()


def test_failed_stop_prevents_restart(monkeypatch):
    monkeypatch.setattr(manager, 'stop_clone_bot', AsyncMock(return_value=False))
    start_mock = AsyncMock()
    monkeypatch.setattr(manager, 'start_clone_bot', start_mock)
    assert asyncio.run(manager.restart_clone_bot(101, 'dummy')) is False
    start_mock.assert_not_awaited()


@pytest.mark.parametrize('path', ['new', 'enable', 'restart'])
@pytest.mark.parametrize('outcome', [True, False, TimeoutError(), RuntimeError('failed')])
def test_save_api_reports_start_result(monkeypatch, path, outcome):
    from app.modules.core import routes
    flask_app = Flask(__name__)
    flask_app.secret_key = 'test-only'
    clone = SimpleNamespace(id=101, expiration_date=None, get_bot_token=lambda: 'dummy')
    monkeypatch.setattr(routes, 'BotClone', MagicMock(return_value=clone))
    routes.BotClone.query.get.return_value = clone
    db = MagicMock()
    monkeypatch.setattr(routes, 'db', db)
    monkeypatch.setattr(routes, 'encrypt_token', lambda _: 'encrypted-test')
    monkeypatch.setattr(routes, 'global_bot_loop', object())
    monkeypatch.setattr(routes, 'global_flask_app', flask_app)
    monkeypatch.setattr(routes, 'is_clone_instance', lambda: False)
    monkeypatch.setattr(manager, 'is_clone_running', lambda _: path == 'restart')
    future = MagicMock()
    if isinstance(outcome, Exception):
        future.result.side_effect = outcome
    else:
        future.result.return_value = outcome

    def schedule(coro, loop):
        coro.close()  # Fake scheduler must not leave an unawaited coroutine.
        return future

    monkeypatch.setattr(routes.asyncio, 'run_coroutine_threadsafe', schedule)
    data = {'clone_name': '测试', 'bot_token': 'dummy', 'is_active': True}
    if path != 'new':
        data['id'] = 101
    with flask_app.test_request_context('/core/api/save_bot_clone', method='POST', json=data):
        session['logged_in'] = True
        result = routes.api_save_bot_clone().get_json()
    db.session.commit.assert_called_once()
    if outcome is True:
        assert result['status'] == 'ok'
    else:
        assert result['status'] == 'error'
        assert '配置已保存' in result['msg']
        assert ('重启失败' if path == 'restart' else '启动失败') in result['msg']
        assert 'dummy' not in result['msg']
    if isinstance(outcome, Exception):
        future.cancel.assert_called_once()
    expected = routes.CLONE_RESTART_TIMEOUT if path == 'restart' else routes.CLONE_START_TIMEOUT
    future.result.assert_called_once_with(timeout=expected)


def test_api_timeouts_cover_retry_and_cleanup_budget():
    from app.modules.core import routes
    budget = (manager.CLONE_START_ATTEMPTS *
              (manager.CLONE_ATTEMPT_TIMEOUT + manager.CLONE_CLEANUP_TIMEOUT) +
              (manager.CLONE_START_ATTEMPTS - 1) * manager.CLONE_RETRY_DELAY)
    assert routes.CLONE_START_TIMEOUT > budget
    assert routes.CLONE_STOP_TIMEOUT > manager.CLONE_STOP_BUDGET
    assert routes.CLONE_RESTART_TIMEOUT > (budget + manager.CLONE_STOP_BUDGET +
                                           manager.RESTART_DELAY_SECONDS)
