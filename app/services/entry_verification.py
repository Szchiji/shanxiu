"""
Entry-verification (进群验证) challenge builders and answer checks.

Supports four types already stored in GroupEntryExitSettings:
- question: custom question + text answer (unchanged behaviour)
- captcha: randomly generated numeric code, answered by typing
- emoji: random emoji buttons, answered by tapping the correct one
- multiple_choice: configured options as buttons, answered by tapping
"""

from __future__ import annotations

import json
import random
from typing import Any, Dict, List, Optional, Sequence

VERIFICATION_TIMEOUT_MIN = 30
VERIFICATION_TIMEOUT_MAX = 300
VERIFICATION_TIMEOUT_DEFAULT = 60

CAPTCHA_MIN = 1000
CAPTCHA_MAX = 9999

EMOJI_POOL = [
    '😀', '🎉', '🔥', '👍', '🌟', '🍀', '🎈', '🍎',
    '🌈', '🎯', '🐶', '🐱', '🌸', '⚽', '🎁', '🎵',
]
EMOJI_CHOICE_COUNT = 4

CALLBACK_PREFIX = 'ev:'


def clamp_verification_timeout(value, default: int = VERIFICATION_TIMEOUT_DEFAULT) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return default
    if timeout < VERIFICATION_TIMEOUT_MIN:
        return VERIFICATION_TIMEOUT_MIN
    if timeout > VERIFICATION_TIMEOUT_MAX:
        return VERIFICATION_TIMEOUT_MAX
    return timeout


def parse_verification_options(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return [line.strip() for line in text.splitlines() if line.strip()]
    return []


def parse_correct_option_index(answer, options: Sequence[str]) -> Optional[int]:
    """Parse a 1-based index from the settings UI into a 0-based index."""
    if not options:
        return None
    try:
        one_based = int(str(answer).strip())
    except (TypeError, ValueError):
        return None
    index = one_based - 1
    if 0 <= index < len(options):
        return index
    return None


def can_start_verification(settings) -> bool:
    if settings is None or not getattr(settings, 'entry_verification_enabled', False):
        return False
    vtype = getattr(settings, 'verification_type', None) or 'question'
    if vtype == 'question':
        return bool(
            (getattr(settings, 'verification_question', None) or '').strip()
            and (getattr(settings, 'verification_answer', None) or '').strip()
        )
    if vtype == 'captcha':
        return True
    if vtype == 'emoji':
        return True
    if vtype == 'multiple_choice':
        options = parse_verification_options(getattr(settings, 'verification_options', None))
        question = (getattr(settings, 'verification_question', None) or '').strip()
        return bool(question and len(options) >= 2 and parse_correct_option_index(
            getattr(settings, 'verification_answer', None), options
        ) is not None)
    return False


def build_entry_challenge(
    settings,
    *,
    chat_id: int,
    user_id: int,
    user_first_name: str,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """Build a verification challenge payload for a new member.

    Returns None if this group cannot start verification.
    """
    if not can_start_verification(settings):
        return None

    rng = rng or random
    vtype = getattr(settings, 'verification_type', None) or 'question'
    timeout = clamp_verification_timeout(getattr(settings, 'verification_timeout', None))
    mention = f"<a href='tg://user?id={user_id}'>{user_first_name or '新成员'}</a>"
    footer = f"⏱ 请在 {timeout} 秒内完成验证，否则将被移出群组。"
    question_footer = f"⏱ 请在 {timeout} 秒内回答，否则将被移出群组。"

    if vtype == 'question':
        question = (settings.verification_question or '').strip()
        answer = (settings.verification_answer or '').strip().lower()
        text = (
            f"👋 欢迎 {mention}!\n\n"
            f"🔐 请回答验证问题:\n{question}\n\n"
            f"{question_footer}"
        )
        return {
            'type': 'question',
            'mode': 'text',
            'text': text,
            'answer': answer,
            'timeout': timeout,
            'buttons': None,
            'correct_index': None,
        }

    if vtype == 'captcha':
        code = str(rng.randint(CAPTCHA_MIN, CAPTCHA_MAX))
        text = (
            f"👋 欢迎 {mention}!\n\n"
            f"🔐 请输入以下数字验证码完成验证：\n"
            f"<code>{code}</code>\n\n"
            f"{footer}"
        )
        return {
            'type': 'captcha',
            'mode': 'text',
            'text': text,
            'answer': code,
            'timeout': timeout,
            'buttons': None,
            'correct_index': None,
        }

    if vtype == 'emoji':
        pool = list(EMOJI_POOL)
        rng.shuffle(pool)
        choices = pool[:EMOJI_CHOICE_COUNT]
        if len(choices) < 2:
            choices = ['😀', '🎉', '🔥', '👍']
        correct_index = rng.randrange(len(choices))
        buttons = [
            [(label, f'{CALLBACK_PREFIX}{user_id}:{idx}')]
            for idx, label in enumerate(choices)
        ]
        text = (
            f"👋 欢迎 {mention}!\n\n"
            f"🔐 请点击正确的表情完成验证：{choices[correct_index]}\n\n"
            f"{footer}"
        )
        return {
            'type': 'emoji',
            'mode': 'callback',
            'text': text,
            'answer': choices[correct_index],
            'timeout': timeout,
            'buttons': buttons,
            'correct_index': correct_index,
        }

    if vtype == 'multiple_choice':
        options = parse_verification_options(settings.verification_options)
        correct_index = parse_correct_option_index(settings.verification_answer, options)
        if correct_index is None:
            return None
        question = (settings.verification_question or '').strip()
        buttons = [
            [(label, f'{CALLBACK_PREFIX}{user_id}:{idx}')]
            for idx, label in enumerate(options)
        ]
        text = (
            f"👋 欢迎 {mention}!\n\n"
            f"🔐 请选择正确答案:\n{question}\n\n"
            f"{footer}"
        )
        return {
            'type': 'multiple_choice',
            'mode': 'callback',
            'text': text,
            'answer': options[correct_index],
            'timeout': timeout,
            'buttons': buttons,
            'correct_index': correct_index,
        }

    return None


def pending_state_from_challenge(challenge: Dict[str, Any], group_id: int, started_at: float) -> Dict[str, Any]:
    return {
        'type': challenge['type'],
        'mode': challenge['mode'],
        'answer': challenge.get('answer') or '',
        'correct_index': challenge.get('correct_index'),
        'group_id': group_id,
        'timeout': challenge['timeout'],
        'started_at': started_at,
    }


def evaluate_verification_submission(
    state: Optional[Dict[str, Any]],
    *,
    clicker_id: Optional[int] = None,
    expected_user_id: Optional[int] = None,
    text: Optional[str] = None,
    callback_index: Optional[int] = None,
) -> str:
    """Return ``correct``, ``wrong``, ``ignored``, ``not_yours``, or ``missing``."""
    if not state:
        return 'missing'
    if expected_user_id is not None and clicker_id is not None and clicker_id != expected_user_id:
        return 'not_yours'

    mode = state.get('mode') or 'text'
    if callback_index is not None:
        if mode != 'callback':
            return 'ignored'
        try:
            submitted = int(callback_index)
        except (TypeError, ValueError):
            return 'wrong'
        correct = state.get('correct_index')
        return 'correct' if correct is not None and submitted == correct else 'wrong'

    if text is not None:
        if mode != 'text':
            # Button-based challenges should not be solved by typing.
            return 'ignored'
        submitted = (text or '').strip()
        if (state.get('type') or '') == 'captcha':
            return 'correct' if submitted == str(state.get('answer') or '') else 'wrong'
        return 'correct' if submitted.lower() == str(state.get('answer') or '').strip().lower() else 'wrong'

    return 'ignored'


def parse_verification_callback_data(data: Optional[str]):
    """Parse ``ev:{user_id}:{index}`` into ``(user_id, index)`` or None."""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    payload = data[len(CALLBACK_PREFIX):]
    parts = payload.split(':')
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
