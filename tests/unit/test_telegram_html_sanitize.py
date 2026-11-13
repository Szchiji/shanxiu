"""sanitize_html_for_telegram keeps Telegram-supported tags and drops junk."""
from app.services import sanitize_html_for_telegram


def test_allows_inline_and_block_telegram_formats():
    html = (
        '<b>bold</b> <i>i</i> <u>u</u> <s>s</s> '
        '<code>copy me</code> '
        '<pre>block\ncode</pre> '
        '<tg-spoiler>hidden</tg-spoiler> '
        '<span class="tg-spoiler">also hidden</span> '
        '<blockquote>quoted</blockquote> '
        '<a href="https://t.me/example">link</a>'
    )
    cleaned = sanitize_html_for_telegram(html)
    assert '<code>copy me</code>' in cleaned
    assert '<pre>block\ncode</pre>' in cleaned
    assert '<tg-spoiler>hidden</tg-spoiler>' in cleaned
    assert '<span class="tg-spoiler">also hidden</span>' in cleaned
    assert '<blockquote>quoted</blockquote>' in cleaned
    assert '<a href="https://t.me/example">link</a>' in cleaned
    assert '<b>bold</b>' in cleaned


def test_strips_disallowed_tags_and_attributes_but_keeps_text():
    html = (
        '<script src="https://evil.example/x.js"></script><div>hello</div>'
        '<img src=x onerror=alert(1)>'
        '<blockquote onclick="evil()">quote</blockquote>'
        '<code class="x" onclick="evil()">tick</code>'
    )
    cleaned = sanitize_html_for_telegram(html)
    assert '<script' not in cleaned.lower()
    assert 'onclick' not in cleaned
    assert 'onerror' not in cleaned
    assert '<img' not in cleaned
    assert 'hello' in cleaned
    assert '<blockquote>quote</blockquote>' in cleaned
    assert '<code>tick</code>' in cleaned


def test_flattens_editor_divs_inside_pre_and_blockquote():
    pre = sanitize_html_for_telegram('<pre><div>line1</div><div>line2</div></pre>')
    assert '<div>' not in pre
    assert '<pre>' in pre and '</pre>' in pre
    assert 'line1' in pre and 'line2' in pre
    assert 'line1\nline2' in pre or 'line1\n\nline2' in pre

    quote = sanitize_html_for_telegram('<blockquote><div>a</div><br><div>b</div></blockquote>')
    assert '<div>' not in quote
    assert '<br' not in quote
    assert '<blockquote>' in quote
    assert 'a' in quote and 'b' in quote


def test_nbsp_and_empty():
    assert sanitize_html_for_telegram('') == ''
    assert sanitize_html_for_telegram(None) is None
    assert sanitize_html_for_telegram('a&nbsp;b') == 'a b'


def test_strips_bare_spans_but_keeps_spoiler_class():
    cleaned = sanitize_html_for_telegram('<span>plain</span> <span class="tg-spoiler">hid</span>')
    assert cleaned == 'plain <span class="tg-spoiler">hid</span>'
