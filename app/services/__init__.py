from app import db
import json
import re
import bleach

# Telegram-supported HTML tags
_TELEGRAM_ALLOWED_TAGS = [
    'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
    'a', 'code', 'pre', 'tg-spoiler', 'tg-emoji', 'span', 'blockquote'
]


def _telegram_attributes(tag, name, value):
    """Determine whether an attribute is allowed for a given Telegram HTML tag."""
    if tag == 'a' and name == 'href':
        return True
    if tag == 'span' and name == 'class':
        # Only allow the tg-spoiler class on span elements
        return value.strip() == 'tg-spoiler'
    if tag == 'tg-emoji' and name == 'emoji-id':
        return True
    return False


def _strip_bare_spans(text):
    """
    Remove bare <span> tags (those without class="tg-spoiler") while keeping
    their text content.  <span class="tg-spoiler"> … </span> pairs are left
    intact.  Uses a simple stack so nested spans are handled correctly.
    """
    _SPAN_RE = re.compile(r'(<span\s+class="tg-spoiler"[^>]*>|<span\b[^>]*>|</span>)')
    parts = _SPAN_RE.split(text)
    result = []
    stack = []  # tracks 'spoiler' or 'bare' for each open <span>

    for part in parts:
        if part == '<span class="tg-spoiler">':
            stack.append('spoiler')
            result.append(part)
        elif part.startswith('<span'):
            # Any other <span …> variant (bare or with a non-tg-spoiler class)
            stack.append('bare')
            # intentionally not appended — tag is stripped
        elif part == '</span>':
            if stack:
                kind = stack.pop()
                if kind == 'spoiler':
                    result.append('</span>')
                # else bare — closing tag is also stripped
        else:
            result.append(part)

    return ''.join(result)



def _flatten_blocks_inside_pre_and_blockquote(text):
    """Turn leftover editor <div>/<p>/<br> inside pre/blockquote into newlines."""
    def _flatten(match):
        tag, attrs, inner = match.group(1), match.group(2), match.group(3)
        inner = re.sub(r'<div><br\s*/?></div>', '\n', inner, flags=re.I)
        inner = re.sub(r'<p><br\s*/?></p>', '\n', inner, flags=re.I)
        inner = re.sub(r'<br\s*/?>', '\n', inner, flags=re.I)
        inner = re.sub(r'</(?:div|p)>', '', inner, flags=re.I)
        inner = re.sub(r'<(?:div|p)\b[^>]*>', '\n', inner, flags=re.I)
        return f'<{tag}{attrs}>{inner}</{tag}>'

    return re.sub(
        r'<(pre|blockquote)(\b[^>]*)>([\s\S]*?)</\1>',
        _flatten,
        text,
        flags=re.I,
    )


def sanitize_html_for_telegram(text):
    """
    Sanitize HTML to only include Telegram-supported tags.

    Telegram supports these HTML tags:
    - <b>, <strong> - bold
    - <i>, <em> - italic
    - <u>, <ins> - underline
    - <s>, <strike>, <del> - strikethrough
    - <code> - inline code
    - <pre> - code block
    - <a href=""> - link
    - <tg-spoiler> or <span class="tg-spoiler"> - spoiler
    - <tg-emoji> - custom emoji
    - <blockquote> - quoted block

    All other tags are stripped while their inner text content is preserved.
    <div>, <p> and <br> nested inside <pre> or <blockquote> are converted to
    newlines first so Telegram HTML is not broken by leftover editor markup.
    """
    if not text:
        return text

    # Replace common HTML entities that Telegram's HTML parser does not support.
    # &nbsp; in particular appears as literal "&nbsp;" in Telegram messages if
    # not decoded first.
    text = text.replace('&nbsp;', ' ')

    # Contenteditable editors may leave <div>/<p>/<br> inside <pre> or
    # <blockquote>. Telegram's HTML mode does not accept those tags, and
    # stripping them would glue lines together, so turn them into newlines.
    text = _flatten_blocks_inside_pre_and_blockquote(text)

    cleaned = bleach.clean(
        text,
        tags=_TELEGRAM_ALLOWED_TAGS,
        attributes=_telegram_attributes,
        strip=True
    )
    # bleach keeps <span> tags whose attributes were stripped (e.g. a span with
    # a non-tg-spoiler class becomes a bare <span>).  Telegram rejects any
    # <span> that does not carry class="tg-spoiler", so remove them here.
    return _strip_bare_spans(cleaned)

