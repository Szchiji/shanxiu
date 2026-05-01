"""
Report module – Telegram bot handlers.

Conversation flow (triggered by /start report_<user_id>):
  Questions are loaded dynamically from SystemConfig key ``report_questions``
  (JSON array of ``{"text": str, "required": bool}``).
  After all questions the bot optionally asks for a photo depending on
  the ``report_push_media`` config flag.

View flow (triggered by /start view_<user_id>):
  Lists all approved reports for the target user and links to channel messages.

Audit callbacks (inline keyboard in admin group):
  audit_approve_<id>  → publish to channel, notify submitter
  audit_reject_<id>   → update status, notify submitter
"""

import json
import logging
import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
STEP_QUESTION, STEP_PHOTO = range(2)

# Key used to store the target user_id inside conversation user_data
_TARGET_KEY = '_report_target_user_id'

_DEFAULT_QUESTIONS = [
    {"text": "请问故障发生的时间是？", "required": True},
    {"text": "请描述具体的故障现象？", "required": True},
    {"text": "最终的处理结果是什么？", "required": True},
]

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_flask_app(context: ContextTypes.DEFAULT_TYPE):
    """Return the Flask app stored in bot_data by run_bot()."""
    return context.application.bot_data.get('flask_app')


def _db_get_config(flask_app, key: str, default: str = '') -> str:
    with flask_app.app_context():
        from app.models import SystemConfig
        return SystemConfig.get_value(key, default)


def _load_questions(flask_app) -> list:
    """Return the configured question list, falling back to defaults."""
    raw = _db_get_config(flask_app, 'report_questions', '')
    try:
        qs = json.loads(raw) if raw else _DEFAULT_QUESTIONS
        return qs if qs else _DEFAULT_QUESTIONS
    except (ValueError, TypeError):
        return _DEFAULT_QUESTIONS


def _push_media_enabled(flask_app) -> bool:
    """Return True if channel push should include the photo."""
    val = _db_get_config(flask_app, 'report_push_media', 'true')
    return val.lower() != 'false'


def _build_answers_block(answers: list) -> str:
    """Format a list of {question, answer} dicts as a readable text block."""
    lines = []
    for item in answers:
        q = item.get('question', '')
        a = item.get('answer', '')
        lines.append(f'❓ {q}\n💬 {a}')
    return '\n\n'.join(lines)


def _build_push_caption(flask_app, report_id: int, answers: list) -> str:
    """Build the channel-push caption from the configured template."""
    template = _db_get_config(
        flask_app,
        'report_push_template',
        '📋 <b>认证用户报告 #{report_id}</b>\n\n{answers}',
    )
    answers_block = _build_answers_block(answers)
    return template.replace('{report_id}', str(report_id)).replace('{answers}', answers_block)


def _prompt_for_question(questions: list, idx: int, total: int) -> str:
    q = questions[idx]
    optional_hint = '' if q.get('required', True) else '（选填，输入 - 可跳过）'
    return f'第{idx + 1}/{total}步：{q["text"]}{optional_hint}'


# ──────────────────────────────────────────────────────────────────────────────
# Shared save-and-notify helper
# ──────────────────────────────────────────────────────────────────────────────

async def _save_and_notify(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_file_id):
    flask_app = _get_flask_app(context)
    questions = context.user_data.get('questions', _DEFAULT_QUESTIONS)
    raw_answers = context.user_data.get('answers', [])

    # Build structured answers list
    answers = [
        {"question": questions[i]["text"], "answer": raw_answers[i]}
        for i in range(min(len(questions), len(raw_answers)))
    ]

    submitter_id = update.effective_user.id if update.effective_user else None
    target_user_id = context.user_data.get(_TARGET_KEY)

    def _save():
        with flask_app.app_context():
            from app.models import UserReport
            from app import db
            r = UserReport(
                user_id=target_user_id,
                submitter_id=submitter_id,
                # Legacy columns – populated from first three answers for backward compat
                fault_time=raw_answers[0] if len(raw_answers) > 0 else '',
                fault_desc=raw_answers[1] if len(raw_answers) > 1 else '',
                process_result=raw_answers[2] if len(raw_answers) > 2 else '',
                photo_file_id=photo_file_id,
                answers=json.dumps(answers, ensure_ascii=False),
                status='pending',
            )
            db.session.add(r)
            db.session.commit()
            return r.id

    loop = asyncio.get_running_loop()
    report_id = await loop.run_in_executor(None, _save)

    # Notify admin group
    admin_group_id_str = _db_get_config(flask_app, 'admin_group_id', '')
    if admin_group_id_str:
        try:
            admin_group_id = int(admin_group_id_str)
            answers_block = _build_answers_block(answers)
            caption = (
                f"📋 <b>新报告待审核 #{report_id}</b>\n\n"
                f"👤 针对用户 ID：<code>{target_user_id}</code>\n\n"
                f"{answers_block}\n\n"
                f"提交者：<a href='tg://user?id={submitter_id}'>{submitter_id}</a>"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton('✅ 通过', callback_data=f'audit_approve_{report_id}'),
                    InlineKeyboardButton('❌ 驳回', callback_data=f'audit_reject_{report_id}'),
                ]
            ])
            if photo_file_id:
                await update.get_bot().send_photo(
                    chat_id=admin_group_id,
                    photo=photo_file_id,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            else:
                await update.get_bot().send_message(
                    chat_id=admin_group_id,
                    text=caption,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
        except Exception as e:
            logger.error(f'Failed to notify admin group: {e}')

    await update.message.reply_text(
        '🎉 报告已提交！\n\n管理员审核通过后，将发布到报告频道，并通知您。'
    )
    context.user_data.clear()
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────────
# ConversationHandler – write a report
# ──────────────────────────────────────────────────────────────────────────────

async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /start report_<user_id>"""
    import re as _re
    text = update.message.text or ''
    m = _re.search(r'/start report_(\d+)', text)
    if not m:
        await update.message.reply_text('❌ 链接无效，请重新点击报告链接。')
        return ConversationHandler.END

    target_user_id = int(m.group(1))
    context.user_data[_TARGET_KEY] = target_user_id

    flask_app = _get_flask_app(context)
    if flask_app is None:
        await update.message.reply_text('❌ 服务暂时不可用，请稍后再试。')
        return ConversationHandler.END

    questions = _load_questions(flask_app)
    context.user_data['questions'] = questions
    context.user_data['answers'] = []
    context.user_data['current_q_idx'] = 0

    total = len(questions)
    await update.message.reply_text(
        f'📝 开始填写报告（共{total}步）\n\n' + _prompt_for_question(questions, 0, total)
    )
    return STEP_QUESTION


async def report_step_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flask_app = _get_flask_app(context)
    questions = context.user_data.get('questions', _DEFAULT_QUESTIONS)
    idx = context.user_data.get('current_q_idx', 0)
    answer = update.message.text.strip()

    # Handle optional-skip marker
    if answer == '-' and not questions[idx].get('required', True):
        answer = ''

    context.user_data['answers'].append(answer)
    idx += 1
    context.user_data['current_q_idx'] = idx

    total = len(questions)
    if idx < total:
        await update.message.reply_text(_prompt_for_question(questions, idx, total))
        return STEP_QUESTION

    # All questions answered – check if photo is needed
    if flask_app and _push_media_enabled(flask_app):
        await update.message.reply_text(f'第{total + 1}步：请发送现场照片 📷')
        return STEP_PHOTO

    # No photo required – save and finish directly
    return await _save_and_notify(update, context, None)


async def report_step_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flask_app = _get_flask_app(context)
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
        await update.message.reply_text('❌ 请发送一张图片。')
        return STEP_PHOTO

    return await _save_and_notify(update, context, photo_file_id)


async def report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text('❌ 已取消报告填写。')
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────────
# View reports handler
# ──────────────────────────────────────────────────────────────────────────────

async def view_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /start view_<user_id>"""
    import re as _re
    text = update.message.text or ''
    m = _re.search(r'/start view_(\d+)', text)
    if not m:
        await update.message.reply_text('❌ 链接无效，请重新点击查看链接。')
        return

    target_user_id = int(m.group(1))
    flask_app = _get_flask_app(context)
    if flask_app is None:
        await update.message.reply_text('❌ 服务暂时不可用，请稍后再试。')
        return

    def _get_reports():
        with flask_app.app_context():
            from app.models import UserReport, SystemConfig
            reports = UserReport.query.filter_by(
                user_id=target_user_id, status='approved'
            ).order_by(UserReport.created_at.desc()).all()
            channel = SystemConfig.get_value('report_channel', '')
            return reports, channel

    loop = asyncio.get_running_loop()
    reports, channel = await loop.run_in_executor(None, _get_reports)

    if not reports:
        await update.message.reply_text(
            f'📭 该认证用户（ID: {target_user_id}）目前还没有任何历史报告。\n\n'
            '要不要成为第一个给 TA 写报告的人？'
        )
        return

    lines = [f'📋 <b>用户 {target_user_id} 的历史报告（共 {len(reports)} 份）：</b>\n']
    for i, r in enumerate(reports, 1):
        date_str = r.created_at.strftime('%Y-%m-%d') if r.created_at else '未知日期'
        line = f'🔹 {i}. {date_str} ｜ {r.fault_time or ""}'
        if r.channel_msg_id and channel:
            # Build a link to the channel message
            chan = channel.removeprefix('@')
            link = f'https://t.me/{chan}/{r.channel_msg_id}'
            line += f'\n   👉 <a href="{link}">点击查看报告详情</a>'
        lines.append(line)

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML', disable_web_page_preview=True)


# ──────────────────────────────────────────────────────────────────────────────
# Audit callback handler
# ──────────────────────────────────────────────────────────────────────────────

async def audit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ''
    flask_app = _get_flask_app(context)

    # Helper: edit the admin-group message regardless of whether it is a photo
    # message (has caption) or a plain text message.
    async def _edit_admin_message(text: str, reply_markup=None):
        kwargs = dict(parse_mode='HTML', reply_markup=reply_markup)
        if query.message and query.message.caption is not None:
            await query.edit_message_caption(caption=text, **kwargs)
        else:
            await query.edit_message_text(text=text, **kwargs)

    if flask_app is None:
        await _edit_admin_message('❌ 服务暂时不可用。')
        return

    if data.startswith('audit_approve_'):
        report_id = int(data.split('audit_approve_')[1])
        action = 'approve'
    elif data.startswith('audit_reject_'):
        report_id = int(data.split('audit_reject_')[1])
        action = 'reject'
    else:
        return

    def _get_report():
        with flask_app.app_context():
            from app.models import UserReport
            r = UserReport.query.get(report_id)
            if r is None:
                return None
            answers = []
            if r.answers:
                try:
                    answers = json.loads(r.answers)
                except (ValueError, TypeError):
                    pass
            # Fall back to legacy columns when answers JSON is absent
            if not answers:
                if r.fault_time:
                    answers.append({"question": "故障时间", "answer": r.fault_time})
                if r.fault_desc:
                    answers.append({"question": "故障现象", "answer": r.fault_desc})
                if r.process_result:
                    answers.append({"question": "处理结果", "answer": r.process_result})
            return {
                'id': r.id,
                'user_id': r.user_id,
                'submitter_id': r.submitter_id,
                'photo_file_id': r.photo_file_id,
                'status': r.status,
                'answers': answers,
            }

    loop = asyncio.get_running_loop()
    report = await loop.run_in_executor(None, _get_report)

    if report is None:
        await _edit_admin_message('❌ 报告不存在。')
        return

    if report['status'] != 'pending':
        await _edit_admin_message(
            f'ℹ️ 该报告已被处理（当前状态：{report["status"]}）。'
        )
        return

    reviewer_name = query.from_user.full_name or str(query.from_user.id)

    if action == 'reject':
        def _reject():
            with flask_app.app_context():
                from app.models import UserReport
                from app import db
                r = UserReport.query.get(report_id)
                if r:
                    r.status = 'rejected'
                    db.session.commit()

        await loop.run_in_executor(None, _reject)
        await _edit_admin_message(f'❌ 报告 #{report_id} 已被 {reviewer_name} 驳回。')
        submitter_id = report.get('submitter_id')
        if submitter_id:
            try:
                await query.get_bot().send_message(
                    chat_id=submitter_id,
                    text=(
                        f'❌ 抱歉，您提交的报告 #{report_id} 未通过审核。\n\n'
                        '请确保描述清晰并包含清晰的现场图片，欢迎重新提交。'
                    ),
                )
            except Exception as e:
                logger.warning(f'Failed to notify submitter {submitter_id} of rejection: {e}')
        return

    # approve – build caption from template
    channel = _db_get_config(flask_app, 'report_channel', '')
    channel_msg_id = None
    push_media = _push_media_enabled(flask_app)
    caption = _build_push_caption(flask_app, report_id, report['answers'])

    if channel:
        try:
            if push_media and report['photo_file_id']:
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
            channel_msg_id = sent.message_id
        except Exception as e:
            logger.error(f'Failed to publish report to channel: {e}')

    def _approve(msg_id):
        with flask_app.app_context():
            from app.models import UserReport
            from app import db
            r = UserReport.query.get(report_id)
            if r:
                r.status = 'approved'
                if msg_id:
                    r.channel_msg_id = msg_id
                db.session.commit()

    await loop.run_in_executor(None, _approve, channel_msg_id)
    await _edit_admin_message(
        f'✅ 报告 #{report_id} 已由 {reviewer_name} 审核通过并发布到频道。'
    )
    submitter_id = report.get('submitter_id')
    if submitter_id:
        try:
            channel = _db_get_config(flask_app, 'report_channel', '')
            msg = f'🎉 您提交的报告 #{report_id} 已审核通过'
            if channel_msg_id and channel:
                chan = channel.removeprefix('@')
                link = f'https://t.me/{chan}/{channel_msg_id}'
                msg += f'！\n\n👉 <a href="{link}">点击查看已发布报告</a>'
            await query.get_bot().send_message(
                chat_id=submitter_id,
                text=msg,
                parse_mode='HTML',
            )
        except Exception as e:
            logger.warning(f'Failed to notify submitter {submitter_id} of approval: {e}')


# ──────────────────────────────────────────────────────────────────────────────
# Handler objects to register in run_bot()
# ──────────────────────────────────────────────────────────────────────────────

report_conv_handler = ConversationHandler(
    entry_points=[
        MessageHandler(
            filters.Regex(r'^/start report_\d+') & filters.ChatType.PRIVATE,
            report_start,
        )
    ],
    states={
        STEP_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_step_question)],
        STEP_PHOTO: [
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, report_step_photo)
        ],
    },
    fallbacks=[
        MessageHandler(filters.COMMAND, report_cancel),
    ],
    name='report_conversation',
    persistent=False,
)

view_reports_handler = MessageHandler(
    filters.Regex(r'^/start view_\d+') & filters.ChatType.PRIVATE,
    view_reports,
)

audit_callback_handler = CallbackQueryHandler(
    audit_callback,
    pattern=r'^audit_(approve|reject)_\d+$',
)
