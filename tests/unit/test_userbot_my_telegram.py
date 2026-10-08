"""my.telegram.org api_id/api_hash flow: HTML parsing (fixtures) + error handling."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.userbot import my_telegram as mt

FIX = Path(__file__).parent / 'fixtures'


def _fx(name):
    return (FIX / name).read_text(encoding='utf-8')


def test_parse_existing_app():
    page = mt.parse_apps_page(_fx('my_telegram_apps_existing.html'))
    assert page.api_id == 1234567
    assert page.api_hash == '0123456789abcdef0123456789abcdef'


def test_parse_create_form():
    page = mt.parse_apps_page(_fx('my_telegram_apps_create.html'))
    assert page.api_id is None and page.api_hash is None
    assert page.create_hash == 'a1b2c3d4e5f60718'


def test_parse_send_password():
    assert mt.parse_send_password_response('{"random_hash":"abc123"}') == 'abc123'
    with pytest.raises(mt.MyTelegramError, match='尝试次数过多'):
        mt.parse_send_password_response('Sorry, too many tries. Please try again later.')
    with pytest.raises(mt.MyTelegramError, match='手动'):
        mt.parse_send_password_response('<html>changed</html>')


def test_parse_login():
    mt.parse_login_response('true')
    with pytest.raises(mt.MyTelegramError, match='验证码错误'):
        mt.parse_login_response('Invalid confirmation code!')


def test_generate_app_fields_valid():
    f = mt.generate_app_fields()
    assert f['app_shortname'].isalnum() and 5 <= len(f['app_shortname']) <= 32
    assert f['app_platform'] == 'desktop'


class _FakeHttp:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def _next(self, method, url, data=None, timeout=None):
        self.calls.append((method, url.replace(mt.BASE_URL, ''), dict(data or {})))
        return SimpleNamespace(text=self.script.pop(0))

    def post(self, url, data=None, timeout=None):
        return self._next('POST', url, data, timeout)

    def get(self, url, timeout=None):
        return self._next('GET', url, None, timeout)


def test_full_flow_creates_app_when_missing():
    http = _FakeHttp([
        '{"random_hash":"rh1"}', 'true',
        _fx('my_telegram_apps_create.html'), '',
        _fx('my_telegram_apps_existing.html'),
    ])
    flow = mt.MyTelegramFlow('+8613800000000', http=http)
    flow.send_password()
    flow.login('ABCdef')
    assert flow.fetch_or_create_app() == (1234567, '0123456789abcdef0123456789abcdef')
    create = [c for c in http.calls if c[1] == '/apps/create'][0]
    assert create[2]['hash'] == 'a1b2c3d4e5f60718'
    assert http.calls[1][2] == {'phone': '+8613800000000', 'random_hash': 'rh1', 'password': 'ABCdef'}


def test_existing_app_skips_create():
    http = _FakeHttp([_fx('my_telegram_apps_existing.html')])
    flow = mt.MyTelegramFlow('+1', http=http)
    assert flow.fetch_or_create_app()[0] == 1234567
    assert all(c[1] != '/apps/create' for c in http.calls)


def test_create_error_gives_manual_hint():
    http = _FakeHttp([_fx('my_telegram_apps_create.html'), 'ERROR'])
    flow = mt.MyTelegramFlow('+1', http=http)
    with pytest.raises(mt.MyTelegramError) as ei:
        flow.fetch_or_create_app()
    assert 'ERROR' in str(ei.value) and '手动' in str(ei.value)


def test_network_error_is_friendly():
    import requests

    class Boom(_FakeHttp):
        def post(self, *a, **k):
            raise requests.ConnectionError('x')

    flow = mt.MyTelegramFlow('+1', http=Boom([]))
    with pytest.raises(mt.MyTelegramError, match='无法连接'):
        flow.send_password()
