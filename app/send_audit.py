"""Fail-open, metadata-only Bot API audit for PTB 21.11.1 and explicit HTTP calls.

One attempt/result pair per actual PTB request-layer invocation, not per business
helper. No request mutation, extra request, DB access, retry, or response wrapping.
External token users, Telethon/MTProto and other processes are outside this hook.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import logging
import re
import sys
from threading import Lock
from uuid import uuid4

_LOG = logging.getLogger('app.send_audit')
_TZ = timezone(timedelta(hours=8))
_INSTALL_LOCK = Lock()
# Numeric identities only. Never retain tokens, bot objects, payloads or URLs.
_CLONES: dict[int, int | None] = {}
_VERIFIED: set[int] = set()
_ID = re.compile(r'-?\d{1,20}\Z')
_USERNAME = re.compile(r'@[A-Za-z0-9_]{1,64}\Z')
_SYMBOL = re.compile(r'[A-Za-z_][A-Za-z0-9_.]{0,180}\Z')
_METHOD = re.compile(r'(?:send|copy|forward|edit)[A-Z][A-Za-z]{0,70}\Z')
_EXTRA = frozenset({'setMessageReaction', 'deleteMessage', 'deleteMessages',
                    'stopMessageLiveLocation', 'stopPoll', 'setGameScore'})


def _number(value):
    if type(value) is int and -(10**20) < value < 10**20:
        return value
    if type(value) is str and _ID.fullmatch(value):
        return int(value)
    return None


def _chat(value):
    number = _number(value)
    if number is not None:
        return number
    if type(value) is str and _USERNAME.fullmatch(value):
        return value
    return None


def _token_id(token):
    # Only consume the numeric prefix; never store/emit the credential.
    if type(token) is str:
        return _number(token.partition(':')[0])
    return None


def _tracked(method):
    return type(method) is str and (bool(_METHOD.fullmatch(method)) or method in _EXTRA)


def _source():
    # Bounded frame walk, no inspect.stack(), locals, filenames or arguments.
    frame = sys._getframe(1)
    try:
        for _ in range(32):
            if frame is None:
                break
            module = frame.f_globals.get('__name__', '')
            function = frame.f_code.co_name
            if (type(module) is str and module.startswith('app.')
                    and module != __name__ and _SYMBOL.fullmatch(module)
                    and _SYMBOL.fullmatch(function)):
                return module + '.' + function
            frame = frame.f_back
        return 'unattributed'
    finally:
        del frame


def _emit(fields):
    try:
        _LOG.info('BOT_SEND_AUDIT %s', json.dumps({
            'v': 1, 'time': datetime.now(_TZ).isoformat(timespec='milliseconds'),
            **fields,
        }, ensure_ascii=True, separators=(',', ':')))
    except Exception:
        # Even a broken logging handler must not affect the Telegram call.
        pass


def _begin(method, data, bot_id, clone_id, transport):
    try:
        data = data if isinstance(data, dict) else {}
        fields = {
            'attempt_id': uuid4().hex, 'method': method, 'bot_id': bot_id,
            'clone_id': clone_id, 'transport': transport, 'source': _source(),
        }
        for key in ('chat_id', 'from_chat_id'):
            value = _chat(data.get(key))
            if value is not None:
                fields[key] = value
        for key in ('message_id', 'message_ids'):
            value = data.get(key)
            if key == 'message_id':
                value = [value]
            if type(value) in (list, tuple):
                ids = [_number(i) for i in value[:1000]]
                fields['source_message_ids'] = [i for i in ids if i is not None]
                if len(value) > 1000:
                    fields['source_ids_truncated'] = True
        _emit({**fields, 'phase': 'attempt', 'status': 'pending'})
        return fields
    except Exception:
        return None


def _result_messages(result, fallback_chat):
    output = []
    results = result if isinstance(result, list) else [result]
    for item in results[:1000]:
        if not isinstance(item, dict):
            continue
        message_id = _number(item.get('message_id'))
        if message_id is None:
            continue
        chat = item.get('chat')
        chat_id = _chat(chat.get('id')) if isinstance(chat, dict) else None
        output.append({'chat_id': chat_id if chat_id is not None else fallback_chat,
                       'message_id': message_id})
    return output


def _finish(fields, status, result=None, error=None):
    try:
        if fields is None:
            return
        event = {**fields, 'phase': 'result', 'status': status}
        if status == 'success':
            event['messages'] = _result_messages(result, fields.get('chat_id'))
            if type(result) is bool:
                event['boolean_result'] = result
        if error is not None:
            event['error_type'] = error  # fixed enum, never exception text/type names
        _emit(event)
    except Exception:
        pass


def _error(exc):
    from telegram.error import (BadRequest, ChatMigrated, Conflict, Forbidden,
                                InvalidToken, NetworkError, RetryAfter, TelegramError)
    for cls, name in ((BadRequest, 'bad_request'), (Forbidden, 'forbidden'),
                      (InvalidToken, 'invalid_token'), (RetryAfter, 'rate_limited'),
                      (ChatMigrated, 'chat_migrated'), (Conflict, 'conflict')):
        if isinstance(exc, cls):
            return 'failed', name
    if isinstance(exc, asyncio.CancelledError):
        return 'unknown', 'cancelled'
    if isinstance(exc, NetworkError):
        return 'unknown', 'network'
    if isinstance(exc, TelegramError):
        return 'unknown', 'telegram_error'
    return 'unknown', 'unexpected'


def _failed(fields, exc):
    try:
        status, error = _error(exc)
        _finish(fields, status, error=error)
    except Exception:
        pass


def _identity(bot):
    user = getattr(bot, '_bot_user', None)
    bot_id = _number(getattr(user, 'id', None))
    key = _token_id(bot.token)
    if bot_id is None and key in _VERIFIED:
        bot_id = key  # a temporary Bot can share an already verified identity
    return bot_id, _CLONES.get(key)


def install_send_audit():
    """Install exactly once at PTB's shared request layer; log safe status."""
    try:
        from telegram import Bot, __version__
        with _INSTALL_LOCK:
            if getattr(Bot._do_post, '_send_audit_installed', False):
                return True
            original = Bot._do_post

            @wraps(original)
            async def audited(self, endpoint, data, **kwargs):
                fields = None
                try:
                    if _tracked(endpoint):
                        bot_id, clone_id = _identity(self)
                        fields = _begin(endpoint, data, bot_id, clone_id, 'ptb')
                except Exception:
                    pass
                try:
                    result = await original(self, endpoint=endpoint, data=data, **kwargs)
                except BaseException as exc:
                    _failed(fields, exc)
                    raise
                if endpoint == 'getMe':
                    try:
                        bot_id = _number(result.get('id')) if isinstance(result, dict) else None
                        if bot_id is not None:
                            _VERIFIED.add(bot_id)
                            _emit({'event': 'identity_ready', 'bot_id': bot_id,
                                   'clone_id': _CLONES.get(_token_id(self.token)),
                                   'transport': 'ptb'})
                    except Exception:
                        pass
                _finish(fields, 'success', result=result)
                return result

            audited._send_audit_installed = True
            Bot._do_post = audited
        _emit({'event': 'installed', 'installed': True,
               'ptb_version': __version__ if re.fullmatch(r'[0-9.]+', __version__) else 'unknown',
               'coverage': 'Bot._do_post;explicit_bot_api_post',
               'external_calls_covered': False})
        return True
    except Exception:
        _emit({'event': 'installed', 'installed': False})
        return False


def register_bot(bot, clone_id=None):
    """Bind before initialize, without any new getMe request or token retention."""
    try:
        installed = install_send_audit()
        key = _token_id(bot.token)
        clone = _number(clone_id)
        if key is not None:
            _CLONES[key] = clone
        bot_id, _ = _identity(bot)
        if bot_id is not None:
            _VERIFIED.add(bot_id)
        _emit({'event': 'binding', 'installed': installed, 'bot_id': bot_id,
               'clone_id': clone, 'transport': 'ptb'})
    except Exception:
        pass


def bot_api_post(post, url, **kwargs):
    """Audit an explicit requests.post Bot API call; return its *same* Response.

    No global requests patch. Current direct writes are reactions/member tags;
    direct reads are intentionally not audited. Future direct writes must use
    this function (the inventory test guards production call sites).
    """
    fields = None
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        # Never log or retain URL, token, query string, JSON body or error text.
        match = re.fullmatch(r'/bot([0-9]{1,20}):[^/]+/([A-Za-z]{1,80})', parts.path)
        if parts.scheme == 'https' and parts.netloc == 'api.telegram.org' and match:
            key = int(match[1])
            method = match[2]
            # Include member tag writes too, without user/tag content.
            if _tracked(method) or method == 'setChatMemberTag':
                fields = _begin(method, kwargs.get('json') or kwargs.get('data'),
                                key if key in _VERIFIED else None,
                                _CLONES.get(key), 'requests')
    except Exception:
        pass
    try:
        response = post(url, **kwargs)
    except BaseException as exc:
        # requests network/transport errors have ambiguous delivery status too.
        _finish(fields, 'unknown', error='http_transport')
        raise
    if fields is not None:
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get('ok') is True:
                _finish(fields, 'success', result=payload.get('result'))
            elif isinstance(payload, dict) and payload.get('ok') is False:
                # Only a definite 4xx Bot API rejection is a definite failure.
                code = _number(payload.get('error_code'))
                if code is not None and 400 <= code < 500:
                    _finish(fields, 'failed', error='bot_api_rejected')
                else:
                    _finish(fields, 'unknown', error='bot_api_error')
            else:
                _finish(fields, 'unknown', error='invalid_response')
        except Exception:
            _finish(fields, 'unknown', error='invalid_response')
    return response
