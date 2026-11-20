"""Only mocked transports: never contact Telegram or production services."""
import asyncio
import ast
import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from telegram import Bot, InputMediaPhoto, User
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.request import BaseRequest

from app import send_audit as audit

TOKEN = '123456789:AA' + 'x' * 33
SECRET = 'PRIVATE_BODY_DO_NOT_LOG'
URL = 'https://api.telegram.org/bot' + TOKEN + '/sendMessage'


class Transport(BaseRequest):
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        data = request_data.parameters if request_data else {}
        endpoint = url.rsplit('/', 1)[-1]
        self.calls.append((endpoint, data))
        if self.error and endpoint != 'getMe':
            raise self.error
        if endpoint == 'getMe':
            result = {'id': 123456789, 'is_bot': True, 'first_name': 'test'}
        elif self.response is not None:
            result = self.response
        else:
            result = {'message_id': 99, 'date': 1, 'chat': {'id': -42, 'type': 'group'}}
        return 200, json.dumps({'ok': True, 'result': result}).encode()


@pytest.fixture(autouse=True)
def state(monkeypatch, caplog):
    # Restore any process-global hook after each test, even if conftest installed it.
    original = Bot._do_post
    monkeypatch.setattr(Bot, '_do_post', original)
    monkeypatch.setattr(audit, '_CLONES', {})
    monkeypatch.setattr(audit, '_VERIFIED', set())
    caplog.set_level(logging.INFO, logger='app.send_audit')


def events(caplog, phase=None):
    result = [json.loads(r.getMessage().split('BOT_SEND_AUDIT ', 1)[1])
              for r in caplog.records if r.name == 'app.send_audit'
              and r.getMessage().startswith('BOT_SEND_AUDIT ')]
    return [r for r in result if phase is None or r.get('phase') == phase]


def make_bot(response=None, error=None, clone_id=None):
    transport = Transport(response, error)
    bot = Bot(TOKEN, request=transport, get_updates_request=transport)
    audit.register_bot(bot, clone_id)
    object.__setattr__(bot, '_bot_user', User(123456789, 'test', True))
    audit._VERIFIED.add(123456789)
    return bot, transport


@pytest.mark.asyncio
@pytest.mark.parametrize('method,response,data,ids', [
    ('sendMessage', {'message_id': 1, 'chat': {'id': -42}}, {}, [1]),
    ('sendMediaGroup', [{'message_id': 1}, {'message_id': 2}], {}, [1, 2]),
    ('copyMessage', {'message_id': 3}, {'from_chat_id': -10, 'message_id': 8}, [3]),
    ('copyMessages', [{'message_id': 4}, {'message_id': 5}],
     {'from_chat_id': -10, 'message_ids': [8, 9]}, [4, 5]),
    ('forwardMessage', {'message_id': 6}, {'from_chat_id': -10, 'message_id': 8}, [6]),
    ('forwardMessages', [{'message_id': 7}], {'from_chat_id': -10, 'message_ids': [8]}, [7]),
    ('editMessageText', {'message_id': 8}, {'message_id': 8}, [8]),
    ('editMessageCaption', True, {}, []),
    ('editMessageMedia', {'message_id': 8}, {'message_id': 8}, [8]),
    ('editMessageReplyMarkup', True, {}, []),
    ('sendPhoto', {'message_id': 9}, {}, [9]),
    ('sendPaidMedia', {'message_id': 10}, {}, [10]),
])
async def test_request_layer_results(caplog, method, response, data, ids):
    bot, transport = make_bot(response)
    payload = {'chat_id': -42, 'text': SECRET, 'reply_markup': {'url': URL}, **data}
    before = dict(payload)
    result = await bot._do_post(method, payload)
    assert result == response
    assert payload == before
    assert len(transport.calls) == 1
    attempt, finished = events(caplog, 'attempt')[0], events(caplog, 'result')[0]
    assert attempt['attempt_id'] == finished['attempt_id']
    assert finished['status'] == 'success'
    assert [i['message_id'] for i in finished['messages']] == ids
    assert all(i['chat_id'] == -42 for i in finished['messages'])
    assert finished['bot_id'] == 123456789 and finished['clone_id'] is None
    assert finished['time'].endswith('+08:00')
    if 'from_chat_id' in data:
        assert finished['from_chat_id'] == -10
    if 'message_id' in data:
        assert finished['source_message_ids'] == [data['message_id']]
    assert SECRET not in caplog.text and TOKEN not in caplog.text and URL not in caplog.text


@pytest.mark.asyncio
async def test_public_ptb_and_extbot_methods(caplog):
    from telegram.ext import ExtBot
    t = Transport()
    bot = ExtBot(TOKEN, request=t, get_updates_request=t)
    audit.register_bot(bot, clone_id=13)
    await bot.initialize()  # mocked getMe also populates the identity cache
    sent = await bot.send_message(-42, SECRET)
    assert sent.message_id == 99
    assert events(caplog, 'result')[-1]['clone_id'] == 13
    assert events(caplog, 'result')[-1]['bot_id'] == 123456789
    assert all(call[0] in ('getMe', 'sendMessage') for call in t.calls)
    assert any(e.get('event') == 'identity_ready' for e in events(caplog))
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('error,status,kind', [
    (BadRequest(SECRET + TOKEN), 'failed', 'bad_request'),
    (Forbidden(SECRET + TOKEN), 'failed', 'forbidden'),
    (RetryAfter(1), 'failed', 'rate_limited'),
    (TimedOut(SECRET + TOKEN), 'unknown', 'network'),
    (NetworkError(SECRET + TOKEN), 'unknown', 'network'),
    (RuntimeError(SECRET + TOKEN), 'unknown', 'unexpected'),
    (asyncio.CancelledError(SECRET + TOKEN), 'unknown', 'cancelled'),
])
async def test_errors_do_not_change_semantics(monkeypatch, caplog, error, status, kind):
    calls = []

    async def original(self, endpoint, data, **kwargs):
        calls.append(data)
        raise error

    monkeypatch.setattr(Bot, '_do_post', original)
    bot, _ = make_bot()
    with pytest.raises(type(error)) as caught:
        await bot._do_post('sendMessage', {'chat_id': -42, 'text': SECRET})
    assert caught.value is error and len(calls) == 1
    event = events(caplog, 'result')[0]
    assert (event['status'], event['error_type']) == (status, kind)
    assert TOKEN not in caplog.text and SECRET not in caplog.text


@pytest.mark.asyncio
async def test_install_idempotent_and_concurrent_isolation(monkeypatch, caplog):
    async def original(self, endpoint, data, **kwargs):
        await asyncio.sleep(0)
        return {'message_id': data['chat_id'] + 100, 'chat': {'id': data['chat_id']}}

    monkeypatch.setattr(Bot, '_do_post', original)
    main, _ = make_bot()
    other = Bot('987654321:AA' + 'y' * 33)
    audit.register_bot(other, 13)
    object.__setattr__(other, '_bot_user', User(987654321, 'clone', True))
    installed = Bot._do_post
    assert audit.install_send_audit() and Bot._do_post is installed
    await asyncio.gather(*[(main if i % 2 else other)._do_post('sendMessage',
                           {'chat_id': i, 'text': SECRET}) for i in range(30)])
    starts, ends = events(caplog, 'attempt'), events(caplog, 'result')
    assert len(starts) == len(ends) == 30
    assert len({e['attempt_id'] for e in starts}) == 30
    for e in ends:
        assert e['clone_id'] == (None if e['chat_id'] % 2 else 13)
        assert e['bot_id'] == (123456789 if e['chat_id'] % 2 else 987654321)
        assert e['messages'][0]['message_id'] == e['chat_id'] + 100
        assert next(s for s in starts if s['attempt_id'] == e['attempt_id'])['chat_id'] == e['chat_id']


@pytest.mark.asyncio
async def test_broken_logger_cannot_affect_original_result_or_error(monkeypatch):
    result = {'message_id': 123}

    async def original(self, endpoint, data, **kwargs):
        return result

    monkeypatch.setattr(Bot, '_do_post', original)
    monkeypatch.setattr(audit._LOG, 'info', Mock(side_effect=RuntimeError(SECRET + TOKEN)))
    bot, _ = make_bot()
    assert await bot._do_post('sendMessage', {'chat_id': 1}) is result
    error = TimedOut(TOKEN)

    async def failing(self, endpoint, data, **kwargs):
        raise error

    monkeypatch.setattr(Bot, '_do_post', failing)
    audit.install_send_audit()
    with pytest.raises(TimedOut) as caught:
        await bot._do_post('sendMessage', {'chat_id': 1})
    assert caught.value is error


@pytest.mark.parametrize('result,expected', [
    ({'ok': True, 'result': {'message_id': 5}}, 'success'),
    ({'ok': False, 'error_code': 400, 'description': SECRET + TOKEN}, 'failed'),
    ({'ok': False, 'error_code': 500, 'description': SECRET + TOKEN}, 'unknown'),
    ({'ok': False, 'description': SECRET + TOKEN}, 'unknown'),
    ({}, 'unknown'),
])
def test_direct_http_unchanged_response(caplog, result, expected):
    audit._CLONES[123456789] = 13
    audit._VERIFIED.add(123456789)
    response = Mock()
    response.json.return_value = result
    post = Mock(return_value=response)
    kwargs = {'json': {'chat_id': -42, 'text': SECRET, 'session': TOKEN, 'links': URL}, 'timeout': 5}
    assert audit.bot_api_post(post, URL, **kwargs) is response
    post.assert_called_once_with(URL, **kwargs)
    e = events(caplog, 'result')[0]
    assert e['status'] == expected and e['bot_id'] == 123456789 and e['clone_id'] == 13
    assert TOKEN not in caplog.text and SECRET not in caplog.text and URL not in caplog.text


def test_direct_http_network_and_invalid_json(caplog):
    import requests
    post = Mock(side_effect=requests.Timeout(SECRET + TOKEN + URL))
    with pytest.raises(requests.Timeout):
        audit.bot_api_post(post, URL, json={'chat_id': 1})
    assert post.call_count == 1
    assert events(caplog, 'result')[-1]['status'] == 'unknown'
    response = Mock()
    response.json.side_effect = ValueError(SECRET + TOKEN)
    assert audit.bot_api_post(Mock(return_value=response), URL, json={}) is response
    assert events(caplog, 'result')[-1]['error_type'] == 'invalid_response'
    assert TOKEN not in caplog.text and SECRET not in caplog.text and URL not in caplog.text


def test_direct_http_logger_failure_and_unrelated_request(monkeypatch):
    monkeypatch.setattr(audit._LOG, 'info', Mock(side_effect=ValueError('broken')))
    response = Mock()
    response.json.return_value = {'ok': True, 'result': True}
    assert audit.bot_api_post(Mock(return_value=response), URL, json={}) is response
    response.json.assert_called_once()
    response.json.reset_mock()
    assert audit.bot_api_post(Mock(return_value=response), 'https://example.org/path', json={}) is response
    response.json.assert_not_called()


def test_metadata_rejects_content_in_identity_fields(caplog):
    fields = audit._begin('sendMessage', {'chat_id': URL, 'from_chat_id': SECRET,
                          'message_id': TOKEN, 'text': SECRET}, 1, None, 'ptb')
    audit._finish(fields, 'success', {'message_id': TOKEN, 'chat': {'id': URL}})
    assert TOKEN not in caplog.text and SECRET not in caplog.text and URL not in caplog.text


def test_direct_write_inventory_and_webhook_contract():
    # Prevent unnoticed additions of unaudited production requests.post calls.
    # HTTP reads and public t.me media scraping intentionally remain unchanged.
    root = Path(__file__).resolve().parents[2] / 'app'
    unexpected = []
    for file in root.rglob('*.py'):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ('requests', '_requests')
                    and node.func.attr in ('post', 'put', 'patch', 'request')):
                unexpected.append((str(file.relative_to(root)), node.lineno))
    assert unexpected == []
    # Existing webhook handlers acknowledge only; they don't return Bot API methods.
    from app.modules.core import routes
    import inspect
    for handler in (routes.webhook, routes.clone_webhook):
        tree = ast.parse(inspect.getsource(handler))
        returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
        assert returns
        for node in returns:
            value = ast.literal_eval(node.value)
            assert isinstance(value, tuple) and isinstance(value[0], str)
            assert value[0] in ('OK', 'Error', 'Bot Not Ready', 'Clone Not Found')


@pytest.mark.asyncio
async def test_public_album_copy_forward_edit(caplog):
    message = {'message_id': 50, 'date': 1, 'chat': {'id': -42, 'type': 'group'}}
    bot, transport = make_bot([message, {**message, 'message_id': 51}])
    album = await bot.send_media_group(-42, [InputMediaPhoto('file_1'), InputMediaPhoto('file_2')])
    assert [m.message_id for m in album] == [50, 51]
    assert [m['message_id'] for m in events(caplog, 'result')[-1]['messages']] == [50, 51]
    transport.response = {'message_id': 52}
    assert (await bot.copy_message(-42, -12, 3)).message_id == 52
    transport.response = message
    assert (await bot.forward_message(-42, -12, 3)).message_id == 50
    transport.response = True
    assert await bot.edit_message_text(SECRET, chat_id=-42, message_id=50) is True
    assert len(events(caplog, 'result')) == 4
    assert len(transport.calls) == 4
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_unbound_bot_reuses_verified_identity_and_safe_caller(caplog):
    make_bot(clone_id=13)
    t = Transport()
    temporary = Bot(TOKEN, request=t, get_updates_request=t)
    scope = {'__name__': 'app.safe_business', 'SECRET': SECRET}
    exec('async def deliver(bot):\n    return await bot.send_message(-42, SECRET)', scope)
    await scope['deliver'](temporary)
    event = events(caplog, 'result')[0]
    assert (event['bot_id'], event['clone_id']) == (123456789, 13)
    assert event['source'] == 'app.safe_business.deliver'
    assert SECRET not in caplog.text


@pytest.mark.parametrize('method', ['setMessageReaction', 'setChatMemberTag'])
def test_existing_direct_write_methods(caplog, method):
    response = Mock()
    response.json.return_value = {'ok': True, 'result': True}
    url = URL.rsplit('/', 1)[0] + '/' + method
    post = Mock(return_value=response)
    assert audit.bot_api_post(post, url, json={'chat_id': -42, 'message_id': 5,
                               'reaction': SECRET, 'tag': SECRET}) is response
    event = events(caplog, 'result')[0]
    assert event['method'] == method and event['status'] == 'success'
    assert event['source_message_ids'] == [5]
    assert SECRET not in caplog.text and TOKEN not in caplog.text
