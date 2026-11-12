"""Tests for entry verification challenge building and answer checking."""

import json
import random
from types import SimpleNamespace

from app.services.entry_verification import (
    CALLBACK_PREFIX,
    VERIFICATION_TIMEOUT_DEFAULT,
    VERIFICATION_TIMEOUT_MAX,
    VERIFICATION_TIMEOUT_MIN,
    build_entry_challenge,
    can_start_verification,
    clamp_verification_timeout,
    evaluate_verification_submission,
    parse_verification_callback_data,
    pending_state_from_challenge,
)


def _settings(**kwargs):
    base = dict(
        entry_verification_enabled=True,
        verification_type='question',
        verification_question='群口令是什么？',
        verification_answer='欢迎',
        verification_options=None,
        verification_timeout=60,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestTimeoutClamp:
    def test_default_and_bounds(self):
        assert clamp_verification_timeout(None) == VERIFICATION_TIMEOUT_DEFAULT
        assert clamp_verification_timeout('abc') == VERIFICATION_TIMEOUT_DEFAULT
        assert clamp_verification_timeout(10) == VERIFICATION_TIMEOUT_MIN
        assert clamp_verification_timeout(999) == VERIFICATION_TIMEOUT_MAX
        assert clamp_verification_timeout(90) == 90


class TestCanStart:
    def test_disabled(self):
        assert can_start_verification(_settings(entry_verification_enabled=False)) is False
        assert can_start_verification(None) is False

    def test_question_requires_question_and_answer(self):
        assert can_start_verification(_settings(verification_answer='')) is False
        assert can_start_verification(_settings()) is True

    def test_captcha_and_emoji_need_no_preset(self):
        assert can_start_verification(_settings(
            verification_type='captcha', verification_question='', verification_answer=''
        )) is True
        assert can_start_verification(_settings(
            verification_type='emoji', verification_question='', verification_answer=''
        )) is True

    def test_multiple_choice_requires_options_and_index(self):
        assert can_start_verification(_settings(
            verification_type='multiple_choice',
            verification_question='哪个是对的？',
            verification_answer='2',
            verification_options=json.dumps(['红', '绿', '蓝']),
        )) is True
        assert can_start_verification(_settings(
            verification_type='multiple_choice',
            verification_question='哪个是对的？',
            verification_answer='2',
            verification_options=json.dumps(['只有一项']),
        )) is False
        assert can_start_verification(_settings(
            verification_type='multiple_choice',
            verification_question='哪个是对的？',
            verification_answer='9',
            verification_options=json.dumps(['红', '绿']),
        )) is False


class TestBuildAndEvaluate:
    def test_question_text_answer_case_insensitive(self):
        challenge = build_entry_challenge(
            _settings(), chat_id=1, user_id=42, user_first_name='张三'
        )
        assert challenge['type'] == 'question'
        assert challenge['mode'] == 'text'
        assert challenge['buttons'] is None
        assert '请回答验证问题' in challenge['text']
        assert '群口令是什么？' in challenge['text']
        state = pending_state_from_challenge(challenge, 1, 0)
        assert evaluate_verification_submission(state, text='欢迎') == 'correct'
        assert evaluate_verification_submission(state, text=' 欢迎 ') == 'correct'
        assert evaluate_verification_submission(state, text='欢迎啊') == 'wrong'
        assert evaluate_verification_submission(state, callback_index=0) == 'ignored'

    def test_captcha_generates_code_and_requires_exact_digits(self):
        rng = random.Random(0)
        challenge = build_entry_challenge(
            _settings(verification_type='captcha', verification_question='', verification_answer=''),
            chat_id=1, user_id=42, user_first_name='李四', rng=rng,
        )
        assert challenge['type'] == 'captcha'
        assert challenge['mode'] == 'text'
        code = challenge['answer']
        assert code.isdigit() and len(code) == 4
        assert code in challenge['text']
        state = pending_state_from_challenge(challenge, 1, 0)
        assert evaluate_verification_submission(state, text=code) == 'correct'
        assert evaluate_verification_submission(state, text=code.lower()) == 'correct'
        assert evaluate_verification_submission(state, text='0000') == 'wrong'

    def test_emoji_emits_buttons_correct_and_wrong_and_timeout_payload(self):
        rng = random.Random(1)
        challenge = build_entry_challenge(
            _settings(verification_type='emoji', verification_timeout=45),
            chat_id=99, user_id=7, user_first_name='王五', rng=rng,
        )
        assert challenge['type'] == 'emoji'
        assert challenge['mode'] == 'callback'
        assert challenge['timeout'] == 45
        assert challenge['buttons']
        labels = [row[0][0] for row in challenge['buttons']]
        data = [row[0][1] for row in challenge['buttons']]
        assert len(labels) == 4
        assert all(d.startswith(f'{CALLBACK_PREFIX}7:') for d in data)
        assert challenge['answer'] in labels
        state = pending_state_from_challenge(challenge, 1, 0)
        correct = challenge['correct_index']
        assert evaluate_verification_submission(
            state, callback_index=correct, clicker_id=7, expected_user_id=7
        ) == 'correct'
        wrong = 0 if correct != 0 else 1
        assert evaluate_verification_submission(
            state, callback_index=wrong, clicker_id=7, expected_user_id=7
        ) == 'wrong'
        assert evaluate_verification_submission(
            state, callback_index=correct, clicker_id=8, expected_user_id=7
        ) == 'not_yours'
        # Typing must not pass an emoji challenge
        assert evaluate_verification_submission(state, text=challenge['answer']) == 'ignored'

    def test_multiple_choice_buttons_only(self):
        settings = _settings(
            verification_type='multiple_choice',
            verification_question='首都是？',
            verification_answer='2',
            verification_options=json.dumps(['上海', '北京', '广州']),
        )
        challenge = build_entry_challenge(settings, chat_id=1, user_id=10, user_first_name='用户')
        assert challenge['mode'] == 'callback'
        assert [row[0][0] for row in challenge['buttons']] == ['上海', '北京', '广州']
        assert challenge['correct_index'] == 1
        state = pending_state_from_challenge(challenge, 1, 0)
        assert evaluate_verification_submission(state, callback_index=1, clicker_id=10, expected_user_id=10) == 'correct'
        assert evaluate_verification_submission(state, callback_index=0, clicker_id=10, expected_user_id=10) == 'wrong'
        assert evaluate_verification_submission(state, text='北京') == 'ignored'
        assert evaluate_verification_submission(state, text='2') == 'ignored'

    def test_timeout_default_and_kick_state_preserved(self):
        challenge = build_entry_challenge(
            _settings(verification_timeout=12),
            chat_id=1, user_id=1, user_first_name='A',
        )
        assert challenge['timeout'] == VERIFICATION_TIMEOUT_MIN
        state = pending_state_from_challenge(challenge, 9, 123.0)
        assert state['timeout'] == VERIFICATION_TIMEOUT_MIN
        assert state['group_id'] == 9
        # Missing / already-finished state
        assert evaluate_verification_submission(None, text='x') == 'missing'

    def test_parse_callback_data(self):
        assert parse_verification_callback_data('ev:42:1') == (42, 1)
        assert parse_verification_callback_data('vote_1_2') is None
        assert parse_verification_callback_data('ev:x:1') is None
