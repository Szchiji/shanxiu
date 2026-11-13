"""Admin rich-text converters, toolbar markup, and shared JS syntax."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS_PATH = ROOT / 'app' / 'modules' / 'core' / 'templates' / 'partials' / 'rich_text_formats.js'
TEMPLATES = ROOT / 'app' / 'modules' / 'core' / 'templates'

EDITOR_PAGES = [
    'scheduled_messages.html',
    'auto_replies.html',
    'start_messages.html',
    'settings.html',
    'chat_settings.html',
    'group_setting.html',
    'system.html',
]

NEW_TITLES = ['点击复制', '代码块', '剧透', '引用']


def node_executable():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for rich-text JavaScript tests')
    return node


def test_shared_js_syntax():
    result = subprocess.run([node_executable(), '--check', str(JS_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_html_to_template_and_back_preserves_telegram_tags():
    script = r'''
const {htmlToTemplate, templateToHtml} = require(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
for (const c of cases) {
  const out = c.fn === 'htmlToTemplate' ? htmlToTemplate(c.input) : templateToHtml(c.input);
  if (out !== c.expected) {
    console.error(JSON.stringify({name: c.name, got: out, expected: c.expected}));
    process.exit(1);
  }
  if (c.roundtrip) {
    const back = htmlToTemplate(templateToHtml(c.input));
    if (back !== c.input) {
      console.error(JSON.stringify({name: c.name + ' roundtrip', got: back, expected: c.input}));
      process.exit(1);
    }
  }
}
console.log('ok');
'''
    cases = [
        dict(name='plain lines', fn='templateToHtml', input='hello\nworld', expected='hello<div>world</div>'),
        dict(name='divs to nl', fn='htmlToTemplate', input='hello<div>world</div>', expected='hello\nworld'),
        dict(name='keep code', fn='htmlToTemplate', input='say <code>copy me</code>', expected='say <code>copy me</code>'),
        dict(name='keep spoiler', fn='htmlToTemplate',
             input='hide <tg-spoiler>secret</tg-spoiler>!', expected='hide <tg-spoiler>secret</tg-spoiler>!'),
        dict(name='pre inner divs', fn='htmlToTemplate',
             input='<pre><div>a</div><div>b</div></pre>', expected='<pre>\na\nb</pre>'),
        dict(name='pre roundtrip', fn='templateToHtml', input='hello\n<pre>line1\nline2</pre>\nworld',
             expected='hello<pre>line1\nline2</pre><div>world</div>', roundtrip=True),
        dict(name='quote roundtrip', fn='templateToHtml',
             input='intro\n<blockquote>quoted\ntext</blockquote>\noutro',
             expected='intro<blockquote>quoted\ntext</blockquote><div>outro</div>', roundtrip=True),
        dict(name='zwsp', fn='htmlToTemplate', input='<code>\u200Bcopy</code>', expected='<code>copy</code>'),
    ]
    result = subprocess.run(
        [node_executable(), '-e', script, str(JS_PATH), json.dumps(cases, ensure_ascii=False)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == 'ok'


@pytest.mark.parametrize('template', EDITOR_PAGES)
def test_editor_toolbar_has_new_format_buttons(template):
    source = (TEMPLATES / template).read_text()
    assert "partials/rich_text_formats.html" in source
    for title in NEW_TITLES:
        assert f'title="{title}"' in source
    assert "wrapTelegramFormat('code')" in source
    assert "wrapTelegramFormat('pre')" in source
    assert "wrapTelegramFormat('tg-spoiler')" in source
    assert "wrapTelegramFormat('blockquote')" in source
