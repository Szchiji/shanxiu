"""Compile browser-parsed admin scripts/handlers; never contact live services."""
import json
import re
import shutil
import subprocess
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask
from jinja2 import FileSystemLoader

ROOT = Path(__file__).resolve().parents[2] / 'app' / 'modules'
TEMPLATES = sorted(ROOT.glob('**/templates/*.html'))
PAYLOAD = "O'Reilly \"双引号\" ` ${auditInjected()} \\n & </script><script>auditInjected()</script>"


class BrowserHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.handlers, self.rows = [], [], []
        self.inside = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.inside = True
            self.scripts.append('')
        if tag == 'tr' and 'data-id' in attrs:
            self.rows.append(attrs)
        self.handlers.extend(v for k, v in attrs.items() if k.startswith('on') and v)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.scripts[-1] += data


def node_executable():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for browser JavaScript syntax regressions')
    return node


def compile_html(html, tmp_path):
    node = node_executable()
    parsed = BrowserHTML()
    parsed.feed(html)
    for i, script in enumerate(parsed.scripts):
        path = tmp_path / f'script-{i}.js'
        path.write_text(script)
        result = subprocess.run([node, '--check', str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    path = tmp_path / 'handlers.js'
    path.write_text('\n'.join('function handler%d(event) {%s}' % (i, h)
                              for i, h in enumerate(parsed.handlers)))
    result = subprocess.run([node, '--check', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return parsed


@pytest.fixture()
def render_env():
    app = Flask(__name__)
    app.jinja_loader = FileSystemLoader([str(ROOT / 'core/templates'), str(ROOT / 'report/templates')])
    app.secret_key = 'test-only'
    app.add_url_rule('/core/group/<int:gid>/scheduled_messages',
                     endpoint='core.page_scheduled_messages', view_func=lambda gid: '')
    app.jinja_env.filters['from_json'] = json.loads
    return app


def script_context(group_type):
    # Every script interpolation in every admin template is exercised below.
    text = '\n'.join(p.read_text() for p in TEMPLATES)
    conf = {k: PAYLOAD for k in re.findall(r'\b(?:conf|s|sys)\.(\w+)', text)}
    conf.update(msg_signin_success=PAYLOAD, msg_repeat_signin=PAYLOAD)
    group = SimpleNamespace(id=9, type=group_type)
    context = dict(group=group, chat=group, conf=conf, s=conf, sys=conf,
                   session_token='valid-token', page_name='page', railway_domain=PAYLOAD,
                   rules=[], main_groups=[], status={}, votes_data=[],
                   stats=[dict(date=date(2026, 11, 13), total_messages=2, active_users=1)],
                   message=PAYLOAD)
    for name in re.findall(r'\b(\w+_json)\b', text):
        context[name] = json.dumps([dict(id=1, text=PAYLOAD, url=PAYLOAD)])
    return context


@pytest.mark.parametrize('template', TEMPLATES, ids=lambda p: p.name)
@pytest.mark.parametrize('group_type', ['supergroup', 'channel'])
def test_every_admin_inline_script_compiles(render_env, tmp_path, template, group_type):
    source = template.read_text()
    scripts = re.findall(r'<script\b[^>]*>(.*?)</script\s*>', source, re.S | re.I)
    with render_env.test_request_context('/'):
        rendered = [render_env.jinja_env.from_string('<script>' + s + '</script>').render(
            **script_context(group_type)) for s in scripts]
    html = '\n'.join(rendered)
    parsed = compile_html(html, tmp_path)
    assert len(parsed.scripts) == len(scripts), 'Stored content terminated a script element'


@pytest.mark.parametrize('template,variables', [
    ('settings.html', dict(conf=script_context('supergroup')['conf'])),
    ('chat_settings.html', dict(s=script_context('supergroup')['s'])),
])
def test_template_text_is_data_not_template_literal(render_env, tmp_path, template, variables):
    source = (ROOT / 'core/templates' / template).read_text()
    # Evaluate only the data initializer, without a DOM or third-party libraries.
    match = re.search(r'(?:const|let) (?:msgs|msgData) = (\{.*?\n    \});', source, re.S)
    assert match
    with render_env.test_request_context('/'):
        initializer = render_env.jinja_env.from_string(match[1]).render(**variables)
    path = tmp_path / 'values.js'
    path.write_text('const templates = ' + initializer + ';\nconsole.log(JSON.stringify(templates));')
    result = subprocess.run([node_executable(), str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout).values()) == {PAYLOAD}


FULL_PAGES = [
    'auto_replies.html', 'scheduled_messages.html', 'settings.html', 'chat_settings.html',
    'system.html', 'group_setting.html', 'fields.html', 'bot_clones.html',
    'group_bottom_button.html', 'group_lottery.html', 'keyword_filter.html',
    'quiz_games.html', 'start_messages.html', 'channel_post_buttons.html',
    'group_members.html', 'users.html', 'points_rules.html', 'channel_templates.html',
    'channel_coupons.html', 'channel_forward_rules.html',
]


def full_context(group_type, with_rows):
    ctx = script_context(group_type)
    group = SimpleNamespace(id=9, type=group_type, title=PAYLOAD, chat_id='-100123')
    ctx.update(group=group, current_group=group, chat=group, page='audit',
               is_main_instance=True, is_channel=group_type == 'channel',
               current_page=1, total_pages=1, per_page=20, total_items=int(with_rows),
               pagination=SimpleNamespace(pages=1), member_info={}, g_conf=ctx['conf'],
               stats=dict(total=1, verified=1, last_sync=None), search=PAYLOAD)
    row = dict(id=1, group_id=9, media_type='image', media_url=PAYLOAD,
               media_urls=json.dumps([dict(type='image', url=PAYLOAD)]),
               content=PAYLOAD, links=json.dumps([dict(text=PAYLOAD, url=PAYLOAD)]),
               remark=PAYLOAD, trigger_keyword=PAYLOAD, delete_after=10,
               is_active=True, updated_at=None, start_time=None, stop_time=None,
               target_type='group', target_channel_id='', message_thread_id=2,
               repeat_interval=10, delete_previous=True, auto_pin=False,
               user_id=123, first_name=PAYLOAD, last_name=PAYLOAD, username=PAYLOAD,
               is_bot=False, status='member', joined_at=None,
               tg_id='123', tg_display_name=PAYLOAD, tg_username=PAYLOAD,
               profile_data=json.dumps(dict(name=PAYLOAD)), profile_dict=dict(name=PAYLOAD),
               member_tag_list=[PAYLOAD], expiration_date=None, is_banned=False,
               rule_name=PAYLOAD, rule_type='message', points_amount=2, max_daily_points=10,
               field_name=PAYLOAD, name=PAYLOAD, label=PAYLOAD, key='name', type='text',
               options=[], template_name=PAYLOAD, trigger_keywords=PAYLOAD, send_as_reply=False,
               coupon_name=PAYLOAD, coupon_code=PAYLOAD, button_text=PAYLOAD, button_url=PAYLOAD,
               sort_order=0, source_keywords=PAYLOAD, target_chat_id='-100123',
               target_thread_id=None, forward_mode='copy', lottery_name=PAYLOAD,
               lottery_type='random', pool_source='verified', top_n_winners=1,
               title=PAYLOAD, question=PAYLOAD, keyword=PAYLOAD, filter_type='exact',
               action='delete', clone_name=PAYLOAD, bot_username=PAYLOAD)
    row['get_media_url_list'] = lambda: [dict(type='image', url=PAYLOAD)]
    # Use actual dicts (rather than ORM stand-ins) for any tojson event arguments.
    for name in ['auto_replies', 'scheduled_messages', 'members', 'users', 'fields',
                 'rules', 'templates', 'coupons', 'lotteries', 'filters', 'quizzes',
                 'start_messages', 'clones', 'buttons']:
        ctx[name] = [row] if with_rows else []
    return ctx


@pytest.mark.parametrize('template', FULL_PAGES)
@pytest.mark.parametrize('group_type', ['supergroup', 'channel'])
@pytest.mark.parametrize('with_rows', [False, True])
def test_full_html_scripts_handlers_and_row_values(render_env, tmp_path, template, group_type, with_rows):
    with render_env.test_request_context('/'):
        from flask import session
        session['logged_in'] = True
        html = render_env.jinja_env.get_template(template).render(**full_context(group_type, with_rows))
    parsed = compile_html(html, tmp_path)
    assert not any(s.strip() == 'auditInjected()' for s in parsed.scripts)
    if with_rows and template in ('auto_replies.html', 'scheduled_messages.html'):
        assert len(parsed.rows) == 1
        row = parsed.rows[0]
        assert row['data-media-url'] == PAYLOAD
        assert json.loads(row['data-media-urls']) == [dict(type='image', url=PAYLOAD)]
        assert row['data-content'] == PAYLOAD
        assert row['data-remark'] == PAYLOAD
        assert json.loads(row['data-links']) == [dict(text=PAYLOAD, url=PAYLOAD)]
    if with_rows and template == 'users.html':
        class Profiles(HTMLParser):
            values = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'data-profile' in attrs:
                    self.values.append(json.loads(attrs['data-profile']))
        profiles = Profiles()
        profiles.feed(html)
        assert profiles.values == [dict(name=PAYLOAD)]


@pytest.mark.parametrize('template,variable', [
    ('bot_clones.html', 'clonesData'), ('channel_post_buttons.html', 'currentLinks'),
    ('fields.html', 'fields'), ('group_bottom_button.html', 'buttonsData'),
    ('group_lottery.html', 'lotteriesData'), ('keyword_filter.html', 'filtersData'),
    ('quiz_games.html', 'quizzesData'), ('start_messages.html', 'allItems'),
])
def test_serialized_json_preserves_values(render_env, tmp_path, template, variable):
    source = (ROOT / 'core/templates' / template).read_text()
    match = re.search(r'(?:const|let) ' + variable + r' = (JSON.parse\(.*?\));', source)
    assert match
    with render_env.test_request_context('/'):
        initializer = render_env.jinja_env.from_string(match[1]).render(**script_context('supergroup'))
    path = tmp_path / 'json-values.js'
    path.write_text('console.log(JSON.stringify(' + initializer + '));')
    result = subprocess.run([node_executable(), str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [dict(id=1, text=PAYLOAD, url=PAYLOAD)]


@pytest.mark.parametrize('group_type,page', [
    ('supergroup', 'settings'), ('supergroup', 'auto_replies'), ('channel', 'auto_replies'),
])
def test_real_flask_routes_with_stored_special_characters(flask_app, client, tmp_path, group_type, page):
    from uuid import uuid4
    from app import db
    from app.models import AutoReply, BotGroup
    with flask_app.app_context():
        group = BotGroup(chat_id='-100' + uuid4().hex, title=PAYLOAD, type=group_type,
                         config=json.dumps(dict(template=PAYLOAD, msg_checkin_success=PAYLOAD)),
                         fields_config=json.dumps([dict(key='name', label=PAYLOAD, type='text')]))
        db.session.add(group)
        db.session.flush()
        gid = group.id
        db.session.add(AutoReply(group_id=gid, trigger_keyword=PAYLOAD, media_type='image',
                                 media_url=PAYLOAD, media_urls=json.dumps([PAYLOAD]),
                                 content=PAYLOAD, links='[]', remark=PAYLOAD, is_active=True))
        db.session.commit()
    with client.session_transaction() as session:
        session['logged_in'] = True
        session.pop('clone_id', None)
    response = client.get(f'/core/group/{gid}/{page}')
    assert response.status_code == 200
    parsed = compile_html(response.get_data(as_text=True), tmp_path)
    assert not any(s.strip() == 'auditInjected()' for s in parsed.scripts)
    if page == 'auto_replies':
        assert parsed.rows[0]['data-content'] == PAYLOAD
        assert json.loads(parsed.rows[0]['data-media-urls']) == [PAYLOAD]
