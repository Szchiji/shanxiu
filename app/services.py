from . import db
import json
import bleach

# Telegram-supported HTML tags
_TELEGRAM_ALLOWED_TAGS = [
    'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
    'a', 'code', 'pre', 'tg-spoiler', 'tg-emoji', 'span'
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

    All other tags are stripped while their inner text content is preserved.
    """
    if not text:
        return text

    return bleach.clean(
        text,
        tags=_TELEGRAM_ALLOWED_TAGS,
        attributes=_telegram_attributes,
        strip=True
    )

