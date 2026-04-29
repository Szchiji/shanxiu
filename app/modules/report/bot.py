"""
Report module – Telegram bot handlers.

Conversation flow (triggered by /start report_<user_id>):
  STEP_TIME   → ask fault time
  STEP_DESC   → ask fault description
  STEP_RESULT → ask process result
  STEP_PHOTO  → ask for a photo, then save & notify admins

View flow (triggered by /start view_<user_id>):
  Lists all approved reports for the target user and links to channel messages.

Audit callbacks (inline keyboard in admin group):
  audit_approve_<id>  → publish to channel, notify submitter
  audit_reject_<id>   → update status, notify submitter
"""

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
STEP_TIME, STEP_DESC, STEP_RESULT, STEP_PHOTO = range(4)

# Key used to store the target user_id inside conversation user_data
_TARGET_KEY = '_report_target_user_id'

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

    q1 = _db_get_config(flask_app, 'question_1', '请问故障发生的时间是？')
    await update.message.reply_text(f'📝 开始填写报告（共4步）\n\n第1步：{q1}')
    return STEP_TIME


async def report_step_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['fault_time'] = update.message.text.strip()
    flask_app = _get_flask_app(context)
    q2 = _db_get_config(flask_app, 'question_2', '请描述具体的故障现象？') if flask_app else '请描述具体的故障现象？'
    await update.message.reply_text(f'第2步：{q2}')
    return STEP_DESC


async def report_step_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['fault_desc'] = update.message.text.strip()
    flask_app = _get_flask_app(context)
    q3 = _db_get_config(flask_app, 'question_3', '最终的处理结果是什么？') if flask_app else '最终的处理结果是什么？'
    await update.message.reply_text(f'第3步：{q3}')
    return STEP_RESULT


async def report_step_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['process_result'] = update.message.text.strip()
    await update.message.reply_text('第4步：请发送现场照片 📷')
    return STEP_PHOTO


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

    submitter_id = update.effective_user.id
    target_user_id = context.user_data.get(_TARGET_KEY)
    fault_time = context.user_data.get('fault_time', '')
    fault_desc = context.user_data.get('fault_desc', '')
    process_result = context.user_data.get('process_result', '')

    def _save_report():
        with flask_app.app_context():
            from app.models import UserReport
            from app import db
            report = UserReport(
                user_id=target_user_id,
                submitter_id=submitter_id,
                fault_time=fault_time,
                fault_desc=fault_desc,
                process_result=process_result,
                photo_file_id=photo_file_id,
                status='pending',
            )
            db.session.add(report)
            db.session.commit()
            return report.id

    loop = asyncio.get_running_loop()
    report_id = await loop.run_in_executor(None, _save_report)

    # Notify admin group
    admin_group_id_str = _db_get_config(flask_app, 'admin_group_id', '')
    if admin_group_id_str:
        try:
            admin_group_id = int(admin_group_id_str)
            caption = (
                f"📋 <b>新报告待审核 #{report_id}</b>\n\n"
                f"👤 针对用户 ID：<code>{target_user_id}</code>\n"
                f"⏰ 故障时间：{fault_time}\n"
                f"🔧 故障现象：{fault_desc}\n"
                f"✅ 处理结果：{process_result}\n\n"
                f"提交者：<a href='tg://user?id={submitter_id}'>{submitter_id}</a>"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton('✅ 通过', callback_data=f'audit_approve_{report_id}'),
                    InlineKeyboardButton('❌ 驳回', callback_data=f'audit_reject_{report_id}'),
                ]
            ])
            await update.get_bot().send_photo(
                chat_id=admin_group_id,
                photo=photo_file_id,
                caption=caption,
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
    if flask_app is None:
        await query.edit_message_caption('❌ 服务暂时不可用。')
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
            return {
                'id': r.id,
                'user_id': r.user_id,
                'submitter_id': r.submitter_id,
                'fault_time': r.fault_time,
                'fault_desc': r.fault_desc,
                'process_result': r.process_result,
                'photo_file_id': r.photo_file_id,
                'status': r.status,
            }

    loop = asyncio.get_running_loop()
    report = await loop.run_in_executor(None, _get_report)

    if report is None:
        await query.edit_message_caption('❌ 报告不存在。')
        return

    if report['status'] != 'pending':
        await query.edit_message_caption(
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
        await query.edit_message_caption(
            f'❌ 报告 #{report_id} 已被 {reviewer_name} 驳回。'
        )
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

    # approve
    channel = _db_get_config(flask_app, 'report_channel', '')
    channel_msg_id = None

    if channel and report['photo_file_id']:
        caption = (
            f'📋 <b>认证用户报告 #{report_id}</b>\n\n'
            f'⏰ 故障时间：{report["fault_time"]}\n'
            f'🔧 故障现象：{report["fault_desc"]}\n'
            f'✅ 处理结果：{report["process_result"]}'
        )
        try:
            sent = await query.get_bot().send_photo(
                chat_id=channel,
                photo=report['photo_file_id'],
                caption=caption,
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
    await query.edit_message_caption(
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
        STEP_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_step_time)],
        STEP_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_step_desc)],
        STEP_RESULT: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_step_result)],
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
