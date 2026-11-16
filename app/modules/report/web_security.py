"""Security helpers for the report Mini App.

initData is validated with the official Telegram algorithm and the token of
the *current* bot (main or the clone named in the signed start token).
Client-supplied clone/user ids and initDataUnsafe are never trusted.
Bot tokens are never written to logs or included in responses.
"""

from __future__ import annotations

import hashlib
import io
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Optional
from urllib.parse import parse_qsl, urlparse

logger = logging.getLogger(__name__)

# Mini App identity is short-lived; users can reopen the page from the bot.
INITDATA_MAX_AGE_SECONDS = 60 * 60
INITDATA_MAX_BYTES = 8192
# Allow a small clock skew so a just-issued auth_date is not rejected.
INITDATA_FUTURE_SKEW_SECONDS = 60
# Start tokens are minted when the bot sends the WebApp button.
START_TOKEN_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
START_TOKEN_MAX_BYTES = 512
SESSION_TTL_SECONDS = 2 * 60 * 60
MAX_ANSWER_CHARS = 4000

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # Telegram Bot API photo limit
# Multipart form overhead on top of the file, used as a request-size ceiling.
MAX_UPLOAD_REQUEST_BYTES = MAX_UPLOAD_BYTES + 256 * 1024
# Reject images whose declared or decoded size would explode memory.
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_SIDE = 8096

# Never sign Mini App tokens with an empty or known insecure default key.
_INSECURE_SECRET_KEYS = frozenset({'', 'default_secret_key'})


class SecurityError(Exception):
    """Raised when a security check fails.  ``code`` is a stable machine tag."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def new_urlsafe_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def signing_secret_key() -> bytes:
    """Return the HMAC key used for start tokens, or fail closed.

    An empty or known-insecure default key must never be used to sign or
    verify Mini App entry tokens.  Callers should hide the WebApp button
    and keep the in-chat fallback available.
    """
    key = (os.getenv('SECRET_KEY') or '').strip()
    if not key:
        try:
            from flask import current_app, has_app_context
            if has_app_context():
                key = str(current_app.config.get('SECRET_KEY') or '').strip()
        except Exception:
            key = key or ''
    if not key or key in _INSECURE_SECRET_KEYS:
        raise SecurityError('no_signing_key', '网页入口暂不可用，请在对话中填写或查看。')
    return key.encode('utf-8')


def _secret_key() -> bytes:
    return signing_secret_key()


def https_origin_from_url(url) -> Optional[str]:
    """Return ``https://host[:port]`` or None if the URL is not https."""
    if not url:
        return None
    raw = str(url).strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme.lower() != 'https':
        return None
    if not parsed.netloc or '@' in parsed.netloc:
        return None
    host = parsed.hostname
    if not host:
        return None
    # Reject obviously unsafe hosts.
    if host in ('localhost', '127.0.0.1', '0.0.0.0', '::1'):
        return None
    if parsed.port and parsed.port not in (443, None):
        return f'https://{host}:{parsed.port}'
    return f'https://{host}'


def public_https_origin_for_bot(clone_id, *, webhook_url=None, railway_domain=None) -> Optional[str]:
    """Resolve the public https origin used by this bot's webhook, if any.

    Prefers the clone's stored webhook URL (e.g. https://sanxiu.one/...), then
    falls back to ``RAILWAY_PUBLIC_DOMAIN``.  Returns None when no https origin
    can be verified — callers must not invent a WebApp URL in that case.
    """
    origin = https_origin_from_url(webhook_url)
    if origin:
        return origin
    domain = (railway_domain if railway_domain is not None
              else os.getenv('RAILWAY_PUBLIC_DOMAIN', ''))
    domain = (domain or '').strip().rstrip('/')
    if not domain:
        return None
    if domain.startswith('http://') or domain.startswith('https://'):
        return https_origin_from_url(domain)
    return https_origin_from_url(f'https://{domain}')


def build_webapp_url(origin: str, start_token: str) -> Optional[str]:
    origin = https_origin_from_url(origin)
    if not origin or not start_token:
        return None
    # start_token is already url-safe
    return f'{origin}/report/app?s={start_token}'


def mint_start_token(*, mode: str, target_user_id: int, clone_id, now: int = None,
                     max_age: int = START_TOKEN_MAX_AGE_SECONDS) -> str:
    if mode not in ('write', 'view'):
        raise SecurityError('bad_mode', '无效的页面类型。')
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError) as exc:
        raise SecurityError('bad_target', '无效的目标用户。') from exc
    if target_user_id <= 0:
        raise SecurityError('bad_target', '无效的目标用户。')
    now = int(now if now is not None else time.time())
    payload = {
        'v': 1,
        'm': mode,
        't': target_user_id,
        'c': 0 if not clone_id else int(clone_id),
        'e': now + int(max_age),
        'n': new_urlsafe_token(16),
    }
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
    sig = hmac.new(_secret_key(), body, hashlib.sha256).digest()
    from base64 import urlsafe_b64encode
    return urlsafe_b64encode(body).rstrip(b'=').decode('ascii') + '.' + urlsafe_b64encode(sig).rstrip(b'=').decode('ascii')


def _b64url_decode(data: str) -> bytes:
    from base64 import urlsafe_b64decode
    pad = '=' * (-len(data) % 4)
    return urlsafe_b64decode(data + pad)


def parse_start_token(token: str, *, now: int = None) -> dict:
    if not token or not isinstance(token, str) or '.' not in token:
        raise SecurityError('bad_start', '入口无效，请从机器人重新打开。')
    if len(token) > START_TOKEN_MAX_BYTES:
        raise SecurityError('bad_start', '入口无效，请从机器人重新打开。')
    body_b64, _, sig_b64 = token.partition('.')
    try:
        body = _b64url_decode(body_b64)
        sig = _b64url_decode(sig_b64)
    except Exception as exc:
        raise SecurityError('bad_start', '入口无效，请从机器人重新打开。') from exc
    expected = hmac.new(_secret_key(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise SecurityError('bad_start', '入口无效，请从机器人重新打开。')
    try:
        payload = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SecurityError('bad_start', '入口无效，请从机器人重新打开。') from exc
    if not isinstance(payload, dict) or payload.get('v') != 1 or payload.get('m') not in ('write', 'view'):
        raise SecurityError('bad_start', '入口无效，请从机器人重新打开。')
    now = int(now if now is not None else time.time())
    try:
        exp = int(payload['e'])
        target = int(payload['t'])
        clone_raw = payload.get('c') or 0
        clone_id = None if not int(clone_raw) else int(clone_raw)
    except (TypeError, ValueError, KeyError) as exc:
        raise SecurityError('bad_start', '入口无效，请从机器人重新打开。') from exc
    if exp < now:
        raise SecurityError('start_expired', '入口已过期，请从机器人重新打开。')
    if target <= 0:
        raise SecurityError('bad_target', '无效的目标用户。')
    return {
        'mode': payload['m'],
        'target_user_id': target,
        'clone_id': clone_id,
        'expires_at': exp,
    }


def _data_check_string(pairs: list) -> str:
    # Official: all fields except hash, sorted alphabetically, key=value joined by \n.
    filtered = [(k, v) for k, v in pairs if k != 'hash']
    filtered.sort(key=lambda item: item[0])
    return '\n'.join(f'{k}={v}' for k, v in filtered)


def validate_init_data(init_data: str, bot_token: str, *, now: int = None,
                       max_age: int = INITDATA_MAX_AGE_SECONDS) -> dict:
    """Validate Mini App initData with the official HMAC-SHA256 algorithm.

    ``bot_token`` must be the token of the bot that opened the Mini App.
    Returns the parsed fields including a required ``user`` dict with ``id``.
    """
    if not isinstance(init_data, str) or not init_data:
        raise SecurityError('no_init', '请在 Telegram 机器人里打开此页面。')
    if len(init_data) > INITDATA_MAX_BYTES:
        raise SecurityError('bad_init', '身份校验失败，请从机器人重新打开。')
    if not bot_token or not isinstance(bot_token, str):
        raise SecurityError('no_bot', '当前机器人不可用。')

    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise SecurityError('no_init', '请在 Telegram 机器人里打开此页面。')
    keys = [k for k, _v in pairs]
    if len(keys) != len(set(keys)):
        raise SecurityError('bad_init', '身份校验失败，请从机器人重新打开。')
    incoming = {k: v for k, v in pairs}
    received_hash = incoming.get('hash')
    if not received_hash or not re.fullmatch(r'[0-9a-f]{64}', received_hash.lower()):
        raise SecurityError('bad_init', '身份校验失败，请从机器人重新打开。')

    data_check_string = _data_check_string(pairs)
    secret = hmac.new(b'WebAppData', bot_token.encode('utf-8'), hashlib.sha256).digest()
    computed = hmac.new(secret, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed.lower(), received_hash.lower()):
        # Do not log initData or the token.
        logger.info('report webapp initData HMAC mismatch')
        raise SecurityError('bad_init', '身份校验失败，请从机器人重新打开。')

    now = int(now if now is not None else time.time())
    try:
        auth_date = int(incoming.get('auth_date') or '')
    except (TypeError, ValueError) as exc:
        raise SecurityError('bad_init', '身份校验失败，请从机器人重新打开。') from exc
    if auth_date > now + INITDATA_FUTURE_SKEW_SECONDS:
        raise SecurityError('bad_init', '身份校验失败，请从机器人重新打开。')
    if now - auth_date > int(max_age):
        raise SecurityError('init_expired', '登录已过期，请从机器人重新打开。')

    user_raw = incoming.get('user')
    if not user_raw:
        raise SecurityError('no_user', '无法确认 Telegram 用户，请从机器人重新打开。')
    try:
        user = json.loads(user_raw)
    except (ValueError, TypeError) as exc:
        raise SecurityError('no_user', '无法确认 Telegram 用户，请从机器人重新打开。') from exc
    if not isinstance(user, dict):
        raise SecurityError('no_user', '无法确认 Telegram 用户，请从机器人重新打开。')
    try:
        user_id = int(user.get('id'))
    except (TypeError, ValueError) as exc:
        raise SecurityError('no_user', '无法确认 Telegram 用户，请从机器人重新打开。') from exc
    if isinstance(user.get('id'), bool) or not isinstance(user.get('id'), int) or user_id <= 0 or user_id > 2**63 - 1:
        raise SecurityError('no_user', '无法确认 Telegram 用户，请从机器人重新打开。')
    user['id'] = user_id
    return {
        'user': user,
        'auth_date': auth_date,
        'query_id': incoming.get('query_id'),
        'fields': incoming,
    }


def normalize_image(data: bytes):
    """Decode an upload with Pillow, enforce size/pixel limits, and normalize.

    Returns ``(bytes, mime)`` where *mime* is ``image/jpeg`` or ``image/png``
    so the bytes can be sent with ``send_photo``.  GIF/WebP/animated sources
    are converted (first frame only) and never forwarded under a fake PNG
    type.  Magic-byte-only or truncated files are rejected.  Raises
    ``SecurityError`` with a user-facing message, or returns None when the
    caller prefers a generic rejection — this function raises.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise SecurityError('bad_image', '请上传 JPG、PNG、WebP 或 GIF 图片，且不超过 10 MB。')
    data = bytes(data)
    if not data or len(data) < 12 or len(data) > MAX_UPLOAD_BYTES:
        raise SecurityError('bad_image', '请上传 JPG、PNG、WebP 或 GIF 图片，且不超过 10 MB。')

    from PIL import Image, ImageOps, UnidentifiedImageError
    from PIL.Image import DecompressionBombError

    try:
        with Image.open(io.BytesIO(data)) as header:
            width, height = header.size
            if (width <= 0 or height <= 0 or width > MAX_IMAGE_SIDE
                    or height > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS):
                raise SecurityError('bad_image', '图片尺寸过大，请压缩后再上传。')
            header.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            fmt = (im.format or '').upper()
            if fmt in ('JPG',):
                fmt = 'JPEG'
            if fmt not in ('JPEG', 'PNG', 'GIF', 'WEBP'):
                raise SecurityError('bad_image', '请上传 JPG、PNG、WebP 或 GIF 图片，且不超过 10 MB。')
            width, height = im.size
            if width <= 0 or height <= 0:
                raise SecurityError('bad_image', '图片无法识别，请更换后重试。')
            if (width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE or width + height > 10000
                    or max(width, height) > 20 * min(width, height)
                    or width * height > MAX_IMAGE_PIXELS):
                raise SecurityError('bad_image', '图片尺寸过大，请压缩后再上传。')
            if getattr(im, 'is_animated', False):
                im.seek(0)
            im = ImageOps.exif_transpose(im)
            has_alpha = (
                im.mode in ('RGBA', 'LA')
                or (im.mode == 'P' and 'transparency' in im.info)
            )
            out = io.BytesIO()
            if has_alpha:
                converted = im.convert('RGBA')
                converted.save(out, format='PNG', optimize=True)
                mime = 'image/png'
            else:
                converted = im.convert('RGB')
                converted.save(out, format='JPEG', quality=85, optimize=True)
                mime = 'image/jpeg'
            result = out.getvalue()
            if not result or len(result) > MAX_UPLOAD_BYTES:
                raise SecurityError('bad_image', '图片处理后仍过大，请压缩后再上传。')
            return result, mime
    except SecurityError:
        raise
    except (UnidentifiedImageError, DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise SecurityError('bad_image', '图片无法识别或尺寸不安全，请更换后重试。') from exc


def sniff_image(data: bytes):
    """Return ``(mime, suffix)`` after a full decode, else None.

    Kept as a convenience wrapper; uploads must go through ``normalize_image``.
    """
    try:
        _normalized, mime = normalize_image(data)
    except SecurityError:
        return None
    suffix = '.jpg' if mime == 'image/jpeg' else '.png'
    return mime, suffix
