"""
Report module – Telegram bot handlers.

Conversation flow (triggered by /start report_<user_id>):
  Questions are loaded dynamically from SystemConfig key ``report_questions``
  (JSON array of ``{"text": str, "required": bool}``).
  After all questions the bot optionally asks for a photo depending on
  the ``report_require_photo`` config flag.

  States:
    STEP_QUESTION – collecting text answers one by one
    STEP_PHOTO    – waiting for the user to send a photo (or skip)
    STEP_CONFIRM  – showing a summary and waiting for confirm/cancel

View flow (triggered by /start view_<user_id>):
  Lists approved reports that belong to this bot (main or the current clone)
  for the target user, and links to the channel message recorded at publish time.

Audit callbacks (inline keyboard in admin group):
  Require the click to come from the configured admin group, the clicker to be
  an administrator of that group, and the report to belong to this bot.
  audit_approve_<id>  → publish to channel, notify submitter
  audit_reject_<id>   → update status, notify submitter
  audit_delete_channel_<id> → delete the published channel message
"""

import json
import logging
import asyncio
import re as _re
import warnings

from app.modules.report import service as report_service

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logger = logging.getLogger(__name__)

# Conversation states
STEP_QUESTION, STEP_PHOTO, STEP_CONFIRM, STEP_TARGET = range(4)

# Key used to store the target user_id inside conversation user_data
_TARGET_KEY = '_report_target_user_id'

# Matches https://t.me/username, http://t.me/username, t.me/username
# Telegram usernames are 5–32 characters: letters, digits, underscores.
_TG_URL_RE = _re.compile(r'^(?:https?://)?t\.me/([A-Za-z0-9_]{5,32})\s*$', _re.IGNORECASE)

_DEFAULT_QUESTIONS = [
    {"text": "请问故障发生的时间是？", "required": True,
     "hint": "例如：2024-01-15 14:30"},
    {"text": "请描述具体的故障现象？", "required": True,
     "hint": "例如：设备无法启动，屏幕显示错误代码 E01"},
    {"text": "最终的处理结果是什么？", "required": True,
     "hint": "例如：已更换电源模块，设备恢复正常"},
]

_DEFAULT_PHOTO_PROMPT = '请发送预约聊天截图或付款截图 📷（必填）'

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_flask_app(context: ContextTypes.DEFAULT_TYPE):
    """Return the Flask app stored in bot_data by run_bot()."""
    return context.application.bot_data.get('flask_app')


def _get_clone_id(context: ContextTypes.DEFAULT_TYPE):
    """Return the clone_id for the running bot (None for the main bot)."""
    return context.application.bot_data.get('clone_id')


def _scoped_key(key: str, clone_id) -> str:
    """Return a clone-scoped config key, or the bare key for the main bot."""
    return report_service.scoped_key(key, clone_id)


def _db_get_config(flask_app, key: str, default: str = '', clone_id=None) -> str:
    with flask_app.app_context():
        from app.models import SystemConfig
        return SystemConfig.get_value(_scoped_key(key, clone_id), default)


def _group_user_to_namespace(gu):
    """Convert a GroupUser ORM row to a SimpleNamespace compatible with GroupMember."""
    return report_service.group_user_to_namespace(gu)


def _load_questions(flask_app, clone_id=None) -> list:
    """Return the configured question list, falling back to defaults."""
    raw = _db_get_config(flask_app, 'report_questions', '', clone_id=clone_id)
    try:
        qs = json.loads(raw) if raw else _DEFAULT_QUESTIONS
        return qs if qs else _DEFAULT_QUESTIONS
    except (ValueError, TypeError):
        return _DEFAULT_QUESTIONS


def _push_media_enabled(flask_app, clone_id=None) -> bool:
    """Return True if channel push should include the photo."""
    val = _db_get_config(flask_app, 'report_push_media', 'true', clone_id=clone_id)
    return val.lower() != 'false'


def _require_photo_enabled(flask_app, clone_id=None) -> bool:
    """Return True if the report flow should ask the user to submit a photo."""
    val = _db_get_config(flask_app, 'report_require_photo', 'true', clone_id=clone_id)
    return val.lower() != 'false'


def _build_answers_block(answers: list) -> str:
    """Format a list of {question, answer} dicts as a readable text block."""
    return report_service.build_answers_block(answers)


def _build_push_caption(flask_app, report_id, answers: list, clone_id=None) -> str:
    """Build the channel-push caption from the configured template.

    The template itself may contain trusted Telegram HTML.  User answers and
    {qN} values are escaped so they cannot break or inject markup.
    """
    template = _db_get_config(
        flask_app,
        'report_push_template',
        '📋 <b>认证用户报告 #{report_id}</b>\n\n{answers}',
        clone_id=clone_id,
    )
    return report_service.build_push_caption(template, report_id, answers)


def _prompt_for_question(questions: list, idx: int, total: int) -> str:
    q = questions[idx]
    optional_hint = '' if q.get('required', True) else '（选填，输入 - 可跳过）'
    text = f'第{idx + 1}/{total}步：{q["text"]}{optional_hint}'
    hint = q.get('hint', '').strip()
    if hint:
        text += f'\n\n💡 填写提示：{hint}'
    return text


def _get_photo_prompt(flask_app, clone_id=None) -> str:
    """Return the configured photo-step prompt text."""
    return _db_get_config(flask_app, 'report_photo_prompt', _DEFAULT_PHOTO_PROMPT, clone_id=clone_id)


def _format_user_identity(user_id, member=None, from_user=None) -> str:
    """Return a rich identity string: 昵称 @username ｜ ID: xxx (names escaped)."""
    return report_service.format_user_identity(user_id, member=member, from_user=from_user)


async def _try_delete_message(bot, chat_id: int, message_id: int) -> None:
    """Silently delete a message, ignoring errors (too old, already deleted, etc.)."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _edit_or_reply(context, message, chat_id: int, text: str, **kwargs):
    """Edit the stored q_msg_id if available, otherwise send a new message.

    Updates ``context.user_data['q_msg_id']`` so subsequent steps always point
    at the single persistent question/status card.
    """
    q_msg_id = context.user_data.get('q_msg_id')
    if q_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=q_msg_id, text=text, **kwargs
            )
            return
        except Exception:
            pass
    sent = await message.reply_text(text, **kwargs)
    context.user_data['q_msg_id'] = sent.message_id


def _build_channel_link(channel: str, msg_id: int) -> str:
    """Build a t.me link for a channel message."""
    return report_service.build_channel_link(channel, msg_id)


def _webapp_button(clone_id, mode: str, target_user_id, label: str, flask_app=None):
    """Return a WebAppInfo button when an https origin can be verified; else None."""
    try:
        from app.modules.report.web_routes import resolve_https_origin
        from app.modules.report.web_security import build_webapp_url, mint_start_token
        from contextlib import nullcontext
        # PTB runs outside Flask's request thread. Clone origin lookup touches
        # the DB, so create its context explicitly instead of silently losing
        # every clone's WebApp button in production.
        with flask_app.app_context() if flask_app is not None else nullcontext():
            origin = resolve_https_origin(clone_id)
            if not origin:
                return None
            token = mint_start_token(mode=mode, target_user_id=int(target_user_id), clone_id=clone_id)
        url = build_webapp_url(origin, token)
        if not url:
            return None
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    except Exception as exc:
        logger.warning('webapp button unavailable: %s', exc)
        return None


def _write_entry_markup(clone_id, target_user_id, flask_app=None):
    rows = []
    web = _webapp_button(clone_id, 'write', target_user_id, '在网页填写', flask_app)
    if web:
        rows.append([web])
        rows.append([InlineKeyboardButton(
            '在对话中填写（备用）',
            callback_data=f'report_chat_fallback_{int(target_user_id)}',
        )])
        return InlineKeyboardMarkup(rows), True
    return None, False


def _view_entry_markup(clone_id, target_user_id, flask_app=None):
    web = _webapp_button(clone_id, 'view', target_user_id, '在网页查看', flask_app)
    if web:
        return InlineKeyboardMarkup([[web]])
    return None


# ──────────────────────────────────────────────────────────────────────────────
# ConversationHandler – write a report
# ──────────────────────────────────────────────────────────────────────────────

async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /start report_<user_id> (also /start@BotName)."""
    text = update.message.text or ''
    target_user_id = report_service.parse_start_target_user_id(text, 'report')
    if not target_user_id:
        await update.message.reply_text('❌ 链接无效，请重新点击报告链接。')
        return ConversationHandler.END
    context.user_data[_TARGET_KEY] = target_user_id

    flask_app = _get_flask_app(context)
    if flask_app is None:
        await update.message.reply_text('❌ 服务暂时不可用，请稍后再试。')
        return ConversationHandler.END

    clone_id = _get_clone_id(context)
    questions = _load_questions(flask_app, clone_id=clone_id)
    context.user_data['questions'] = questions
    context.user_data['answers'] = []
    context.user_data['current_q_idx'] = 0

    total = len(questions)
    # Delete the /start command message to keep the chat clean.
    await _try_delete_message(context.bot, update.effective_chat.id, update.message.message_id)
    markup, has_web = _write_entry_markup(clone_id, target_user_id, flask_app)
    if has_web:
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                '📝 请选择填写方式。\n\n'
                '可在 Telegram 内打开网页表单，或继续在对话中逐题填写（备用入口）。'
            ),
            reply_markup=markup,
        )
        context.user_data['q_msg_id'] = msg.message_id
        return ConversationHandler.END
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f'📝 开始填写报告（共{total}步）\n\n' + _prompt_for_question(questions, 0, total),
    )
    context.user_data['q_msg_id'] = msg.message_id
    return STEP_QUESTION


async def report_step_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flask_app = _get_flask_app(context)
    questions = context.user_data.get('questions', _DEFAULT_QUESTIONS)
    idx = context.user_data.get('current_q_idx', 0)
    answer = update.message.text.strip()
    chat_id = update.effective_chat.id

    # Always delete the user's answer to keep the chat clean.
    await _try_delete_message(context.bot, chat_id, update.message.message_id)

    # Validate required questions: reject empty/whitespace-only answers.
    if questions[idx].get('required', True) and answer == '':
        await _edit_or_reply(
            context, update.message, chat_id,
            '⚠️ 此项为必填，请输入有效内容。\n\n' + _prompt_for_question(questions, idx, len(questions)),
        )
        return STEP_QUESTION

    # Handle optional-skip marker
    if answer == '-' and not questions[idx].get('required', True):
        answer = ''

    context.user_data['answers'].append(answer)
    idx += 1
    context.user_data['current_q_idx'] = idx

    total = len(questions)
    if idx < total:
        await _edit_or_reply(
            context, update.message, chat_id,
            _prompt_for_question(questions, idx, total),
        )
        return STEP_QUESTION

    # All questions answered – check if photo is needed
    clone_id = _get_clone_id(context)
    if flask_app and _require_photo_enabled(flask_app, clone_id=clone_id):
        photo_prompt = _get_photo_prompt(flask_app, clone_id=clone_id)
        await _edit_or_reply(
            context, update.message, chat_id,
            f'第{total + 1}步：{photo_prompt}',
        )
        return STEP_PHOTO

    # No photo required – go to confirmation summary
    return await _show_confirm_edit(update, context)


async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a summary of all answers and ask the user to confirm or cancel."""
    flask_app = _get_flask_app(context)
    clone_id = _get_clone_id(context)
    questions = context.user_data.get('questions', _DEFAULT_QUESTIONS)
    raw_answers = context.user_data.get('answers', [])
    photo_file_id = context.user_data.get('photo_file_id')

    answers = [
        {"question": questions[i]["text"], "answer": raw_answers[i] if i < len(raw_answers) else ''}
        for i in range(len(questions))
    ]

    if flask_app:
        preview_text = _build_push_caption(flask_app, '预览', answers, clone_id=clone_id)
    else:
        preview_text = _build_answers_block(answers)

    lines = ['📝 <b>请确认以下报告内容（预览）：</b>\n', preview_text]
    if photo_file_id:
        lines.append('\n📷 已附带截图')

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ 确认提交', callback_data='report_confirm_submit'),
        InlineKeyboardButton('✏️ 重新填写', callback_data='report_restart'),
    ]])
    msg = '\n\n'.join(lines)
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
    return STEP_CONFIRM


async def _show_confirm_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show confirmation summary by editing the persistent question card (q_msg_id)."""
    flask_app = _get_flask_app(context)
    clone_id = _get_clone_id(context)
    questions = context.user_data.get('questions', _DEFAULT_QUESTIONS)
    raw_answers = context.user_data.get('answers', [])
    photo_file_id = context.user_data.get('photo_file_id')

    answers = [
        {"question": questions[i]["text"], "answer": raw_answers[i] if i < len(raw_answers) else ''}
        for i in range(len(questions))
    ]

    if flask_app:
        preview_text = _build_push_caption(flask_app, '预览', answers, clone_id=clone_id)
    else:
        preview_text = _build_answers_block(answers)

    lines = ['📝 <b>请确认以下报告内容（预览）：</b>\n', preview_text]
    if photo_file_id:
        lines.append('\n📷 已附带截图')

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ 确认提交', callback_data='report_confirm_submit'),
        InlineKeyboardButton('✏️ 重新填写', callback_data='report_restart'),
    ]])
    msg_text = '\n\n'.join(lines)
    chat_id = update.effective_chat.id
    q_msg_id = context.user_data.get('q_msg_id')
    if q_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=q_msg_id,
                text=msg_text, parse_mode='HTML', reply_markup=keyboard,
            )
            return STEP_CONFIRM
        except Exception:
            pass
    sent = await update.message.reply_text(msg_text, parse_mode='HTML', reply_markup=keyboard)
    context.user_data['q_msg_id'] = sent.message_id
    return STEP_CONFIRM


async def report_step_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flask_app = _get_flask_app(context)
    chat_id = update.effective_chat.id
    if flask_app is None:
        await update.message.reply_text('❌ 服务暂时不可用，请稍后再试。')
        return ConversationHandler.END

    # Accept photo or document-as-image
    photo_file_id = None
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type and \
            update.message.document.mime_type.startswith('image/'):
        photo_file_id = update.message.document.file_id
    else:
        # Delete the invalid message and show an error on the question card.
        await _try_delete_message(context.bot, chat_id, update.message.message_id)
        total = len(context.user_data.get('questions', _DEFAULT_QUESTIONS))
        photo_prompt = _get_photo_prompt(flask_app, clone_id=_get_clone_id(context))
        await _edit_or_reply(
            context, update.message, chat_id,
            f'❌ 请发送一张图片。\n\n第{total + 1}步：{photo_prompt}',
        )
        return STEP_PHOTO

    # Delete the photo message to keep the chat clean.
    await _try_delete_message(context.bot, chat_id, update.message.message_id)
    context.user_data['photo_file_id'] = photo_file_id
    return await _show_confirm_edit(update, context)


async def report_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline-keyboard actions from the confirmation/skip/restart screen."""
    query = update.callback_query
    await query.answer()
    data = query.data or ''

    if data == 'report_skip_photo':
        # The skip button is no longer shown, but handle any stale callbacks
        # gracefully by re-displaying the confirmation screen.
        return await _show_confirm_from_callback(query, context)

    if data == 'report_restart':
        # Restart from question 0 keeping the same target user and questions
        context.user_data['answers'] = []
        context.user_data['current_q_idx'] = 0
        context.user_data.pop('photo_file_id', None)
        questions = context.user_data.get('questions', _DEFAULT_QUESTIONS)
        total = len(questions)
        await query.edit_message_text(
            f'📝 重新填写报告（共{total}步）\n\n' + _prompt_for_question(questions, 0, total)
        )
        # The confirm message is now the new question card.
        context.user_data['q_msg_id'] = query.message.message_id
        return STEP_QUESTION

    if data == 'report_confirm_submit':
        photo_file_id = context.user_data.get('photo_file_id')
        # Replace the confirm message with a "submitting…" notice
        await query.edit_message_text('⏳ 正在提交报告，请稍候…')
        return await _save_and_notify_from_callback(query, context, photo_file_id)

    return STEP_CONFIRM


async def _show_confirm_from_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Same as _show_confirm but edits an existing message instead of sending a new one."""
    flask_app = _get_flask_app(context)
    clone_id = _get_clone_id(context)
    questions = context.user_data.get('questions', _DEFAULT_QUESTIONS)
    raw_answers = context.user_data.get('answers', [])
    photo_file_id = context.user_data.get('photo_file_id')

    answers = [
        {"question": questions[i]["text"], "answer": raw_answers[i] if i < len(raw_answers) else ''}
        for i in range(len(questions))
    ]

    if flask_app:
        preview_text = _build_push_caption(flask_app, '预览', answers, clone_id=clone_id)
    else:
        preview_text = _build_answers_block(answers)

    lines = ['📝 <b>请确认以下报告内容（预览）：</b>\n', preview_text]
    if photo_file_id:
        lines.append('\n📷 已附带截图')
    else:
        lines.append('\n📷 无截图')

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ 确认提交', callback_data='report_confirm_submit'),
        InlineKeyboardButton('✏️ 重新填写', callback_data='report_restart'),
    ]])
    await query.edit_message_text('\n\n'.join(lines), parse_mode='HTML', reply_markup=keyboard)
    return STEP_CONFIRM


async def _save_and_notify_from_callback(query, context: ContextTypes.DEFAULT_TYPE, photo_file_id):
    """Like _save_and_notify but called from a CallbackQuery (no update.message)."""
    flask_app = _get_flask_app(context)
    clone_id = _get_clone_id(context)
    questions = context.user_data.get('questions', _DEFAULT_QUESTIONS)
    raw_answers = context.user_data.get('answers', [])

    answers = [
        {"question": questions[i]["text"], "answer": raw_answers[i]}
        for i in range(min(len(questions), len(raw_answers)))
    ]

    submitter_id = query.from_user.id if query.from_user else None
    target_user_id = context.user_data.get(_TARGET_KEY)

    def _save():
        with flask_app.app_context():
            from app.models import UserReport, GroupMember, GroupUser, BotGroup
            from app import db
            r = UserReport(**report_service.new_report_values(
                user_id=target_user_id,
                submitter_id=submitter_id,
                raw_answers=raw_answers,
                answers=answers,
                photo_file_id=photo_file_id,
                bot_clone_id=clone_id,
            ))
            db.session.add(r)
            db.session.commit()
            target_member = report_service.find_member_for_bot(
                GroupMember=GroupMember,
                GroupUser=GroupUser,
                BotGroup=BotGroup,
                bot_clone_id=clone_id,
                user_id=target_user_id,
            )
            return r.id, target_member

    loop = asyncio.get_running_loop()
    try:
        report_id, target_member = await loop.run_in_executor(None, _save)
    except Exception as e:
        logger.error('Failed to save report: %s', e)
        try:
            await query.edit_message_text('❌ 报告保存失败，尚未提交，请稍后再试。')
        except Exception:
            pass
        return ConversationHandler.END

    # Notify admin group
    admin_notify_failed = False
    admin_group_id_str = _db_get_config(flask_app, 'admin_group_id', '', clone_id=clone_id)
    if admin_group_id_str:
        try:
            try:
                admin_group_id = int(str(admin_group_id_str).strip())
            except (TypeError, ValueError):
                admin_group_id = admin_group_id_str.strip()
            template_text = _build_push_caption(flask_app, report_id, answers, clone_id=clone_id)
            target_identity = _format_user_identity(target_user_id, member=target_member)
            submitter_identity = _format_user_identity(submitter_id, from_user=query.from_user)
            caption = (
                f"📋 <b>新报告待审核 #{report_id}</b>\n\n"
                f"👤 被提交人：{target_identity}\n"
                f"📤 提交人：{submitter_identity}\n\n"
                f"{template_text}"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton('✅ 通过', callback_data=f'audit_approve_{report_id}'),
                    InlineKeyboardButton('❌ 驳回', callback_data=f'audit_reject_{report_id}'),
                ]
            ])
            if photo_file_id:
                sent_admin = await query.get_bot().send_photo(
                    chat_id=admin_group_id,
                    photo=photo_file_id,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            else:
                sent_admin = await query.get_bot().send_message(
                    chat_id=admin_group_id,
                    text=caption,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )

            def _store_admin_msg():
                with flask_app.app_context():
                    from app.models import UserReport
                    from app import db
                    report_service.store_admin_review_message(
                        db, UserReport, report_id,
                        admin_chat_id=admin_group_id,
                        admin_message_id=sent_admin.message_id,
                    )

            await asyncio.get_running_loop().run_in_executor(None, _store_admin_msg)
        except Exception as e:
            logger.error('Failed to notify admin group: %s', e)
            admin_notify_failed = True

    if admin_notify_failed:
        await query.edit_message_text(
            '🎉 报告已提交，但通知审核群失败。请联系管理员查看，勿重复提交。'
        )
    else:
        await query.edit_message_text('🎉 报告已提交！\n\n管理员审核通过后，将发布到报告频道，并通知您。')
    context.user_data.clear()
    return ConversationHandler.END


async def report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Always clear report state first, regardless of what happens next.
    context.user_data.clear()
    text = update.message.text or ''
    intent = report_service.start_command_intent(text)
    # When /start report_NNN is sent mid-conversation (e.g. user clicked the
    # deep-link again), restart the report flow instead of just canceling so the
    # user doesn't have to click twice.
    if intent == 'report':
        return await report_start(update, context)
    # Mid-report "查看报告" deep links should open the list, not the welcome
    # /start message (and not require a second click after cancel).
    if intent == 'view':
        await view_reports(update, context)
        return ConversationHandler.END
    # When a plain /start (without report_/view_ param) is sent mid-conversation,
    # forward directly to cmd_start so the user reaches admin welcome in one step
    # instead of having to send /start twice.  cmd_start does not use user_data,
    # so clearing it above is safe before forwarding.
    if intent == 'start':
        cmd_start_fn = context.application.bot_data.get('cmd_start')
        if cmd_start_fn:
            await cmd_start_fn(update, context)
            return ConversationHandler.END
        logger.warning('report_cancel: cmd_start not found in bot_data; falling back to cancel message')
    await update.message.reply_text('❌ 已取消报告填写。')
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────────
# View reports handler
# ──────────────────────────────────────────────────────────────────────────────

async def view_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /start view_<user_id> (also /start@BotName)."""
    text = update.message.text or ''
    target_user_id = report_service.parse_start_target_user_id(text, 'view')
    if not target_user_id:
        await update.message.reply_text('❌ 链接无效，请重新点击查看链接。')
        return
    flask_app = _get_flask_app(context)
    if flask_app is None:
        await update.message.reply_text('❌ 服务暂时不可用，请稍后再试。')
        return

    clone_id = _get_clone_id(context)

    # Prefer the authenticated, paginated Mini App over building an unbounded
    # chat message. Keep the original scoped DB list only as a fallback.
    markup = _view_entry_markup(clone_id, target_user_id, flask_app)
    if markup:
        await update.message.reply_text('📋 点击下方按钮查看该人的已通过报告。', reply_markup=markup)
        return

    def _get_reports():
        with flask_app.app_context():
            from app.models import UserReport, SystemConfig, GroupMember, GroupUser, BotGroup
            reports = report_service.visible_reports_query(
                UserReport, target_user_id, clone_id
            ).limit(20).all()
            fallback_channel = SystemConfig.get_value(_scoped_key('report_channel', clone_id), '')
            member = report_service.find_member_for_bot(
                GroupMember=GroupMember,
                GroupUser=GroupUser,
                BotGroup=BotGroup,
                bot_clone_id=clone_id,
                user_id=target_user_id,
            )
            return reports, fallback_channel, member

    loop = asyncio.get_running_loop()
    reports, fallback_channel, member = await loop.run_in_executor(None, _get_reports)

    identity = report_service.format_user_identity(target_user_id, member=member)

    if not reports:
        await update.message.reply_text(
            f'📭 该认证用户（{identity}）目前还没有任何历史报告。',
            parse_mode='HTML',
            reply_markup=_view_entry_markup(clone_id, target_user_id, flask_app),
        )
        return

    rows = []
    from .web_service import format_shanghai
    for r in reports:
        label = f'报告 #{r.id} · {format_shanghai(r.created_at, with_time=False)}'
        rows.append([InlineKeyboardButton(label, callback_data=f'report_view_{target_user_id}_{r.id}')])
    await update.message.reply_text(
        f'📋 {identity} 的最近报告（最多20份），请选择一篇查看：',
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(rows))


async def report_view_callback(update, context):
    query = update.callback_query
    await query.answer()
    try:
        _, _, target_id, report_id = query.data.split('_')
        target_id, report_id = int(target_id), int(report_id)
    except (ValueError, AttributeError):
        return
    flask_app = _get_flask_app(context)
    if flask_app is None:
        await query.message.reply_text('服务暂不可用，请重新打开查报告入口。')
        return
    from types import SimpleNamespace
    from .web_service import view_report_detail
    from .web_security import SecurityError
    with flask_app.app_context():
        from app.models import UserReport, SystemConfig
        try:
            report = view_report_detail(UserReport, SystemConfig,
                SimpleNamespace(mode='view', target_user_id=target_id, clone_id=_get_clone_id(context)), report_id)
        except SecurityError:
            await query.message.reply_text('报告不存在或不可查看，请重新打开查报告入口。')
            return
    text = f'报告 #{report["id"]} · {report["date"]}\n\n'
    text += '\n\n'.join(f'{a["question"]}\n{a["answer"]}' for a in report['answers'])
    if report['channel_link']:
        text += '\n\n频道原文：' + report['channel_link']
    # No HTML/entities to split; do not truncate a long legacy report.
    for offset in range(0, len(text), 1800):
        await query.message.reply_text(text[offset:offset + 1800])


async def report_audit_or_view_callback(update, context):
    if (update.callback_query.data or '').startswith('report_view_'):
        return await report_view_callback(update, context)
    return await audit_callback(update, context)


# ──────────────────────────────────────────────────────────────────────────────
# Audit callback handler
# ──────────────────────────────────────────────────────────────────────────────

async def audit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ''
    flask_app = _get_flask_app(context)
    clone_id = _get_clone_id(context)

    async def _post_audit_result(text: str, update_keyboard=None):
        """Send a new reply to the original admin message with the audit result.

        ``update_keyboard`` replaces the original message's keyboard (pass None
        to strip the buttons entirely).  A new reply message carries the result text.
        """
        try:
            await query.edit_message_reply_markup(reply_markup=update_keyboard)
        except Exception as e:
            logger.debug('Could not update original message keyboard: %s', e)
        if query.message:
            try:
                await query.get_bot().send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    parse_mode='HTML',
                    reply_to_message_id=query.message.message_id,
                )
            except Exception as e:
                logger.warning('Could not send audit result reply: %s', e)

    if flask_app is None:
        await query.answer('❌ 服务暂时不可用。', show_alert=True)
        return

    if data.startswith('audit_approve_'):
        report_id = int(data.split('audit_approve_')[1])
        action = 'approve'
    elif data.startswith('audit_reject_'):
        report_id = int(data.split('audit_reject_')[1])
        action = 'reject'
    elif data.startswith('audit_delete_channel_'):
        report_id = int(data.split('audit_delete_channel_')[1])
        action = 'delete_channel'
    else:
        await query.answer()
        return

    loop = asyncio.get_running_loop()
    admin_group_id_str = _db_get_config(flask_app, 'admin_group_id', '', clone_id=clone_id)
    callback_chat_id = query.message.chat_id if query.message else None
    clicker_id = query.from_user.id if query.from_user else None

    def _load_report():
        with flask_app.app_context():
            from app.models import UserReport, GroupMember, GroupUser, BotGroup
            r = UserReport.query.get(report_id)
            if r is None:
                return None
            target_member = report_service.find_member_for_bot(
                GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
                bot_clone_id=clone_id, user_id=r.user_id,
            )
            submitter_member = None
            if r.submitter_id:
                submitter_member = report_service.find_member_for_bot(
                    GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
                    bot_clone_id=clone_id, user_id=r.submitter_id,
                )
            return {
                'id': r.id,
                'user_id': r.user_id,
                'submitter_id': r.submitter_id,
                'photo_file_id': r.photo_file_id,
                'status': r.status,
                'answers': report_service.parse_answers(r),
                'owner_known': r.owner_known,
                'clone_id': r.clone_id,
                'channel_msg_id': r.channel_msg_id,
                'channel_chat_id': r.channel_chat_id,
                'channel_msg_deleted': r.channel_msg_deleted,
                'admin_chat_id': r.admin_chat_id,
                'admin_message_id': r.admin_message_id,
                'target_identity': report_service.format_user_identity(
                    r.user_id, member=target_member),
                'submitter_identity': (
                    report_service.format_user_identity(
                        r.submitter_id, member=submitter_member)
                    if r.submitter_id else None
                ),
            }

    report = await loop.run_in_executor(None, _load_report)

    # Lightweight stand-in so evaluate_audit_access can read ownership fields
    # even when the row is missing (None) or we only have a snapshot dict.
    class _Snap:
        def __init__(self, d):
            if not d:
                return
            for k, v in d.items():
                setattr(self, k, v)

    snap = _Snap(report) if report else None

    clicker_is_admin = False
    if clicker_id is not None and report_service.clicker_counts_as_admin(clicker_id):
        clicker_is_admin = True
    elif clicker_id is not None and admin_group_id_str:
        try:
            try:
                admin_chat = int(str(admin_group_id_str).strip())
            except (TypeError, ValueError):
                admin_chat = str(admin_group_id_str).strip()
            member = await query.get_bot().get_chat_member(admin_chat, clicker_id)
            clicker_is_admin = report_service.clicker_counts_as_admin(
                clicker_id, getattr(member, 'status', None)
            )
        except Exception as e:
            logger.warning('audit admin check failed: %s', e)
            clicker_is_admin = False

    bot_id = getattr(getattr(context, 'bot', None), 'id', None) or getattr(query.get_bot(), 'id', None)
    access = report_service.evaluate_audit_access(
        report=snap,
        bot_clone_id=clone_id,
        callback_chat_id=callback_chat_id,
        configured_admin_group_id=admin_group_id_str,
        clicker_is_admin=clicker_is_admin,
        callback_sent_by_this_bot=report_service.callback_message_sent_by_bot(
            query.message, bot_id
        ),
        callback_message_id=getattr(query.message, 'message_id', None) if query.message else None,
    )
    if not access.allowed:
        await query.answer(access.message or '无权操作。', show_alert=True)
        return

    await query.answer()
    reviewer_name = report_service.escape_user_html(
        (query.from_user.full_name if query.from_user else None) or str(clicker_id or '')
    )

    if action == 'delete_channel':
        if report and report.get('channel_msg_deleted'):
            await _post_audit_result(f'ℹ️ 报告 #{report_id} 的频道消息已被删除。')
            return
        channel = (report or {}).get('channel_chat_id') or _db_get_config(
            flask_app, 'report_channel', '', clone_id=clone_id
        )
        msg_id = (report or {}).get('channel_msg_id')
        delete_ok = False
        already_gone = False
        if channel and msg_id:
            try:
                await query.get_bot().delete_message(chat_id=channel, message_id=msg_id)
                delete_ok = True
            except Exception as e:
                if report_service.telegram_message_already_gone(e):
                    already_gone = True
                else:
                    logger.warning(
                        'Failed to delete channel message for report #%s: %s', report_id, e
                    )
        elif not msg_id:
            await _post_audit_result(f'❌ 报告 #{report_id} 没有可删除的频道消息。')
            return

        def _mark_deleted():
            with flask_app.app_context():
                from app.models import UserReport
                from app import db
                r = UserReport.query.get(report_id)
                if r is None:
                    return report_service.ActionResult('refused', '报告不存在。')
                return report_service.finalize_delete_channel(
                    db, r, success=delete_ok, already_gone=already_gone
                )

        result = await loop.run_in_executor(None, _mark_deleted)
        if result.status == 'failed':
            await _post_audit_result(result.message)
            return
        if result.status == 'already_deleted':
            await _post_audit_result(result.message)
            return
        await _post_audit_result(result.message)
        return

    if action == 'reject':
        def _reject():
            with flask_app.app_context():
                from app.models import UserReport
                from app import db
                r = UserReport.query.get(report_id)
                if r is None:
                    return report_service.ActionResult('refused', '报告不存在。')
                return report_service.finalize_reject(db, r, clone_id)

        result = await loop.run_in_executor(None, _reject)
        await _post_audit_result(
            result.message if result.status != 'ok'
            else f'❌ 报告 #{report_id} 已被 {reviewer_name} 驳回。'
        )
        if result.status == 'ok' and result.notify_submitter and report and report.get('submitter_id'):
            try:
                await query.get_bot().send_message(
                    chat_id=report['submitter_id'],
                    text=(
                        f'❌ 抱歉，您提交的报告 #{report_id} 未通过审核。\n\n'
                        '请确保描述清晰并包含清晰的现场图片，欢迎重新提交。'
                    ),
                )
            except Exception as e:
                logger.warning(
                    'Failed to notify submitter %s of rejection: %s',
                    report['submitter_id'], e,
                )
        return

    # approve
    def _claim():
        with flask_app.app_context():
            from app.models import UserReport
            from app import db
            return report_service.claim_for_approve(db, UserReport, report_id)

    claim = await loop.run_in_executor(None, _claim)
    if claim.status == 'already_done':
        await _post_audit_result(claim.message)
        return
    if claim.status == 'in_progress':
        await _post_audit_result(claim.message)
        return
    if claim.status != 'claimed':
        await _post_audit_result(claim.message or '无法开始审核。')
        return

    channel = _db_get_config(flask_app, 'report_channel', '', clone_id=clone_id)
    channel_msg_id = claim.channel_msg_id if report_service.should_skip_publish(claim) else None
    channel_chat_used = claim.channel_chat_id if channel_msg_id else None
    published_this_attempt = None

    if channel and not channel_msg_id:
        push_media = _push_media_enabled(flask_app, clone_id=clone_id)
        template_caption = _build_push_caption(
            flask_app, report_id, (report or {}).get('answers') or [], clone_id=clone_id
        )
        identity_header = f"👤 被提交人：{(report or {}).get('target_identity') or report_service.format_user_identity((report or {}).get('user_id'))}"
        if report and report.get('submitter_identity'):
            identity_header += f"\n📤 提交人：{report['submitter_identity']}"
        caption = f"{identity_header}\n\n{template_caption}"
        try:
            if push_media and report and report.get('photo_file_id'):
                sent = await query.get_bot().send_photo(
                    chat_id=channel,
                    photo=report['photo_file_id'],
                    caption=caption,
                    parse_mode='HTML',
                )
            else:
                sent = await query.get_bot().send_message(
                    chat_id=channel,
                    text=caption,
                    parse_mode='HTML',
                )
            published_this_attempt = sent.message_id
            channel_msg_id = sent.message_id
            channel_chat_used = channel
        except Exception as e:
            logger.error('Failed to publish report to channel: %s', e)

            def _release():
                with flask_app.app_context():
                    from app.models import UserReport
                    from app import db
                    report_service.release_approve_claim(db, UserReport, report_id)

            await loop.run_in_executor(None, _release)
            await _post_audit_result(
                f'❌ 报告 #{report_id} 发布到频道失败，仍为待审核且未记入频道消息，可再次点击通过重试。'
            )
            return

        def _record():
            with flask_app.app_context():
                from app.models import UserReport
                from app import db
                try:
                    return report_service.record_published_channel(
                        db, UserReport, report_id, channel_msg_id, channel_chat_used,
                    )
                except Exception as exc:
                    logger.error('Failed to record channel message for report #%s: %s', report_id, exc)
                    return report_service.ActionResult(
                        'record_failed',
                        f'❌ 报告 #{report_id} 已发到频道（消息 ID：{channel_msg_id}），但未能写入数据库。'
                        '请勿再次点击通过（否则可能重复发帖）。请联系技术根据该消息 ID 补记后再完成审核。',
                        channel_msg_id=channel_msg_id,
                        channel_chat_id=channel_chat_used,
                    )

        recorded = await loop.run_in_executor(None, _record)
        if recorded.status == 'lost_race':
            # Exceptional: lease expired during publish and another worker won.
            if recorded.duplicate_msg_id and channel:
                try:
                    await query.get_bot().delete_message(
                        chat_id=channel, message_id=recorded.duplicate_msg_id
                    )
                except Exception as e:
                    logger.warning('Failed to drop lost-race channel post: %s', e)
            channel_msg_id = recorded.channel_msg_id
            channel_chat_used = recorded.channel_chat_id
            published_this_attempt = None
        elif recorded.status != 'ok':
            await _post_audit_result(recorded.message)
            return
        else:
            channel_msg_id = recorded.channel_msg_id
            channel_chat_used = recorded.channel_chat_id

    def _approve():
        with flask_app.app_context():
            from app.models import UserReport, SystemConfig, UserPoints, PointsLog, BotGroup
            from app import db
            r = UserReport.query.get(report_id)
            if r is None:
                return report_service.ActionResult('refused', '报告不存在。')
            try:
                reward_pts = int(SystemConfig.get_value(
                    _scoped_key('report_approval_points', clone_id), '0') or '0')
            except (ValueError, TypeError):
                reward_pts = 0
            points_group_chat_id = SystemConfig.get_value(
                _scoped_key('report_points_group_id', clone_id), ''
            ).strip()
            try:
                return report_service.finalize_approve(
                    db, r, clone_id,
                    channel_msg_id=channel_msg_id,
                    channel_chat_id=channel_chat_used or (channel if channel_msg_id else ''),
                    UserPoints=UserPoints,
                    PointsLog=PointsLog,
                    BotGroup=BotGroup,
                    reward_pts=reward_pts,
                    points_group_chat_id=points_group_chat_id,
                )
            except Exception as exc:
                logger.error('finalize_approve failed for report #%s: %s', report_id, exc)
                db.session.rollback()
                # Channel identity (if any) was already committed; release the
                # lease so a retry can finalize immediately.  CAS on status
                # prevents a double reward.  Do not tell the user to republish
                # if a channel message is already recorded.
                report_service.release_approve_claim(db, UserReport, report_id)
                if channel_msg_id:
                    return report_service.ActionResult(
                        'finalize_failed',
                        f'❌ 报告 #{report_id} 的频道消息已登记，但审核完成或积分发放未完成。'
                        '可再次点击通过以完成审核（不会重复发帖）。',
                        channel_msg_id=channel_msg_id,
                        channel_chat_id=channel_chat_used,
                        report_status='pending',
                    )
                return report_service.ActionResult(
                    'failed',
                    f'❌ 报告 #{report_id} 审核完成失败，仍为待审核，可再次点击通过重试。',
                    report_status='pending',
                )

    result = await loop.run_in_executor(None, _approve)

    if result.status in ('finalize_failed', 'failed', 'refused'):
        await _post_audit_result(result.message)
        return

    delete_keyboard = None
    if result.status == 'ok' and result.published:
        delete_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton('🗑️ 从频道删除', callback_data=f'audit_delete_channel_{report_id}'),
        ]])
    ok_msg = result.message
    if result.status == 'ok' and result.published:
        ok_msg = f'✅ 报告 #{report_id} 已由 {reviewer_name} 审核通过并发布到频道。'
        if result.awarded_points:
            ok_msg += f'\n🎁 奖励积分：+{result.awarded_points}'
    elif result.status == 'ok':
        ok_msg = f'✅ 报告 #{report_id} 已由 {reviewer_name} 审核通过' + (
            '（未配置报告频道，未发布）。' if not result.published else '。'
        )
        if result.awarded_points:
            ok_msg += f'\n🎁 奖励积分：+{result.awarded_points}'
    await _post_audit_result(ok_msg, update_keyboard=delete_keyboard)

    if result.status == 'ok' and result.notify_submitter and report and report.get('submitter_id'):
        try:
            stored_channel = result.channel_chat_id or channel
            msg = f'🎉 您提交的报告 #{report_id} 已审核通过'
            if result.published and result.channel_msg_id and stored_channel:
                link = _build_channel_link(stored_channel, result.channel_msg_id)
                msg += f'！\n\n👉 <a href="{link}">点击查看已发布报告</a>'
            if result.awarded_points:
                msg += f'\n\n🎁 奖励积分：+{result.awarded_points}'
            await query.get_bot().send_message(
                chat_id=report['submitter_id'],
                text=msg,
                parse_mode='HTML',
            )
        except Exception as e:
            logger.warning(
                'Failed to notify submitter %s of approval: %s',
                report['submitter_id'], e,
            )



# ──────────────────────────────────────────────────────────────────────────────
# 私聊"写报告"入口 – 用户输入"写报告"后由机器人引导输入被提交人
# ──────────────────────────────────────────────────────────────────────────────

async def report_start_by_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: user sends '写报告' in a private chat."""
    flask_app = _get_flask_app(context)
    if flask_app is None:
        await update.message.reply_text('❌ 服务暂时不可用，请稍后再试。')
        return ConversationHandler.END

    # Delete the trigger message and send a single persistent card.
    await _try_delete_message(context.bot, update.effective_chat.id, update.message.message_id)
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            '📝 请发送被提交人的用户名或 ID，例如：\n'
            '  • <code>@zhangsan</code>\n'
            '  • <code>https://t.me/zhangsan</code>\n'
            '  • <code>123456789</code>\n\n'
            '发送 /cancel 可取消。'
        ),
        parse_mode='HTML',
    )
    context.user_data['q_msg_id'] = msg.message_id
    return STEP_TARGET


async def report_receive_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive @username or numeric user_id, look up GroupMember, then start questions."""
    text = (update.message.text or '').strip()

    flask_app = _get_flask_app(context)
    if flask_app is None:
        await update.message.reply_text('❌ 服务暂时不可用，请稍后再试。')
        return ConversationHandler.END

    # Parse input: "@username" / "https://t.me/username" / "t.me/username" → extract username;
    # plain digits → int user_id
    lookup_username = None
    lookup_user_id = None
    if text.startswith('@'):
        lookup_username = text[1:].strip()
    else:
        m = _TG_URL_RE.match(text)
        if m:
            lookup_username = m.group(1)
        elif text.lstrip('-').isdigit():
            try:
                lookup_user_id = int(text)
            except ValueError:
                pass

    if not lookup_username and lookup_user_id is None:
        await _try_delete_message(context.bot, update.effective_chat.id, update.message.message_id)
        await _edit_or_reply(
            context, update.message, update.effective_chat.id,
            '⚠️ 格式不正确，请发送用户名（如 @zhangsan 或 https://t.me/zhangsan）或数字 ID（如 123456789）。\n\n'
            '📝 请发送被提交人的用户名或 ID，例如：\n'
            '  • <code>@zhangsan</code>\n'
            '  • <code>https://t.me/zhangsan</code>\n'
            '  • <code>123456789</code>\n\n'
            '发送 /cancel 可取消。',
            parse_mode='HTML',
        )
        return STEP_TARGET

    clone_id = _get_clone_id(context)

    def _find_member():
        from app.models import GroupMember, GroupUser, BotGroup
        with flask_app.app_context():
            return report_service.find_member_for_bot(
                GroupMember=GroupMember,
                GroupUser=GroupUser,
                BotGroup=BotGroup,
                bot_clone_id=clone_id,
                user_id=lookup_user_id,
                username=lookup_username,
            )

    loop = asyncio.get_running_loop()
    member = await loop.run_in_executor(None, _find_member)

    # Fallback: try Telegram API when user not found in GroupMember/GroupUser tables
    if member is None and lookup_username:
        try:
            tg_chat = await context.bot.get_chat(f'@{lookup_username}')
        except Exception as e:
            logger.debug('Telegram get_chat fallback failed for @%s: %s', lookup_username, e)
            tg_chat = None
        if tg_chat and tg_chat.type == 'private':
            tg_user_id = tg_chat.id
            # Re-try DB lookup by user_id in case the record exists under a different username
            def _find_by_tg_id():
                from app.models import GroupMember, GroupUser, BotGroup
                from app import db
                with flask_app.app_context():
                    m = report_service.find_member_for_bot(
                        GroupMember=GroupMember,
                        GroupUser=GroupUser,
                        BotGroup=BotGroup,
                        bot_clone_id=clone_id,
                        user_id=tg_user_id,
                    )
                    if m is None:
                        return None
                    # Cache tg_username on the matching GroupUser so future lookups
                    # work without requiring a Telegram API call.
                    group_ids = report_service.group_ids_for_clone(BotGroup, clone_id) or [-1]
                    gu = (
                        GroupUser.query
                        .filter(
                            GroupUser.group_id.in_(group_ids),
                            GroupUser.tg_id == tg_user_id,
                        )
                        .first()
                    )
                    if gu is not None and tg_chat and tg_chat.username:
                        try:
                            pd = json.loads(gu.profile_data or '{}')
                            if not pd.get('tg_username'):
                                pd['tg_username'] = tg_chat.username
                                gu.profile_data = json.dumps(pd, ensure_ascii=False)
                                db.session.commit()
                        except Exception:
                            logger.warning(
                                'Failed to cache tg_username for GroupUser tg_id=%s',
                                tg_user_id, exc_info=True,
                            )
                    return m

            member = await loop.run_in_executor(None, _find_by_tg_id)
            if member is None:
                # Synthesise a lightweight object from Telegram data so the rest of the
                # flow (identity display, report saving) works normally.
                # Required attributes: user_id, username, first_name, last_name
                from types import SimpleNamespace
                member = SimpleNamespace(
                    user_id=tg_chat.id,
                    username=tg_chat.username or '',
                    first_name=tg_chat.first_name or '',
                    last_name=tg_chat.last_name or '',
                )

    if member is None:
        hint = f'@{lookup_username}' if lookup_username else str(lookup_user_id)
        await _try_delete_message(context.bot, update.effective_chat.id, update.message.message_id)
        await _edit_or_reply(
            context, update.message, update.effective_chat.id,
            f'❌ 未找到用户 {hint}，请确认用户名或 ID 正确，然后重新发送。\n\n'
            '📝 请发送被提交人的用户名或 ID，例如：\n'
            '  • <code>@zhangsan</code>\n'
            '  • <code>https://t.me/zhangsan</code>\n'
            '  • <code>123456789</code>\n\n'
            '发送 /cancel 可取消。',
            parse_mode='HTML',
        )
        return STEP_TARGET

    target_user_id = member.user_id
    context.user_data[_TARGET_KEY] = target_user_id

    clone_id = _get_clone_id(context)
    questions = _load_questions(flask_app, clone_id=clone_id)
    context.user_data['questions'] = questions
    context.user_data['answers'] = []
    context.user_data['current_q_idx'] = 0

    # Delete the user's target input and edit the persistent card to show question 1.
    await _try_delete_message(context.bot, update.effective_chat.id, update.message.message_id)
    identity = _format_user_identity(target_user_id, member=member)
    total = len(questions)
    markup, has_web = _write_entry_markup(clone_id, target_user_id, flask_app)
    intro = f'✅ 已找到被提交人：{identity}\n\n'
    if has_web:
        intro += '可在网页填写，或直接在对话中回答下面的问题（备用入口）。\n\n'
    await _edit_or_reply(
        context, update.message, update.effective_chat.id,
        intro + f'📝 开始填写报告（共{total}步）\n\n'
        + _prompt_for_question(questions, 0, total),
        parse_mode='HTML',
        reply_markup=markup if has_web else None,
    )
    return STEP_QUESTION


async def report_chat_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the private-chat question flow from the「在对话中填写」button."""
    query = update.callback_query
    data = (query.data or '') if query else ''
    try:
        target_user_id = int(data.rsplit('_', 1)[-1])
    except (TypeError, ValueError):
        if query:
            await query.answer('链接无效。', show_alert=True)
        return ConversationHandler.END
    if query:
        await query.answer()

    flask_app = _get_flask_app(context)
    if flask_app is None:
        if query:
            await query.edit_message_text('❌ 服务暂时不可用，请稍后再试。')
        return ConversationHandler.END

    context.user_data[_TARGET_KEY] = target_user_id
    clone_id = _get_clone_id(context)
    questions = _load_questions(flask_app, clone_id=clone_id)
    context.user_data['questions'] = questions
    context.user_data['answers'] = []
    context.user_data['current_q_idx'] = 0
    total = len(questions)
    text = f'📝 开始填写报告（共{total}步）\n\n' + _prompt_for_question(questions, 0, total)
    try:
        await query.edit_message_text(text)
        context.user_data['q_msg_id'] = query.message.message_id if query.message else None
    except Exception:
        sent = await context.bot.send_message(chat_id=query.message.chat_id, text=text)
        context.user_data['q_msg_id'] = sent.message_id
    return STEP_QUESTION


# ──────────────────────────────────────────────────────────────────────────────
# Handler factory – call this for every Application that needs report handlers
# (main bot AND each clone bot).  Each Application must receive its *own*
# handler instances; sharing a single ConversationHandler object across
# multiple Application instances causes the clone bots' conversation flow to
# break because the handler's internal state becomes shared/corrupted.
# ──────────────────────────────────────────────────────────────────────────────

def make_report_handlers():
    """Return fresh (report_conv_handler, view_reports_handler, audit_callback_handler) instances."""
    # per_message=False (default) is intentional: conversation is tracked per user+chat.
    # Suppress the PTBUserWarning that fires when CallbackQueryHandler is used in this mode.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*per_message=False.*",
            category=UserWarning,
        )
        conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(r'(?i)^/start(?:@[A-Za-z0-9_]+)?\s+report_\d+') & filters.ChatType.PRIVATE,
                report_start,
            ),
            MessageHandler(
                filters.Regex(r'^写报告$') & filters.ChatType.PRIVATE,
                report_start_by_text,
            ),
            CallbackQueryHandler(
                report_chat_fallback,
                pattern=r'^report_chat_fallback_\d+$',
            ),
        ],
        states={
            STEP_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_receive_target)],
            STEP_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_step_question)],
            STEP_PHOTO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, report_step_photo),
                # Text fallback: tell the user to send a photo (or skip)
                MessageHandler(filters.TEXT & ~filters.COMMAND, report_step_photo),
                # Inline-keyboard: skip photo or restart
                CallbackQueryHandler(report_confirm_callback,
                                     pattern=r'^report_(skip_photo|restart|confirm_submit)$'),
            ],
            STEP_CONFIRM: [
                CallbackQueryHandler(report_confirm_callback,
                                     pattern=r'^report_(skip_photo|restart|confirm_submit)$'),
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, report_cancel),
        ],
        name='report_conversation',
        persistent=False,
    )
    view_handler = MessageHandler(
        filters.Regex(r'(?i)^/start(?:@[A-Za-z0-9_]+)?\s+view_\d+') & filters.ChatType.PRIVATE,
        view_reports,
    )
    audit_handler = CallbackQueryHandler(
        report_audit_or_view_callback,
        pattern=r'^(?:audit_(?:approve|reject|delete_channel)_\d+|report_view_\d+_\d+)$',
    )
    return conv, view_handler, audit_handler


# Module-level singletons kept for backward compatibility (used by main bot setup).
report_conv_handler, view_reports_handler, audit_callback_handler = make_report_handlers()
