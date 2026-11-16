"""HTTP routes for the report Telegram Mini App (no admin login)."""

from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile

from app import db, limiter
from app.models import (
    BotClone,
    BotGroup,
    GroupMember,
    GroupUser,
    ReportWebSession,
    ReportWebUpload,
    SystemConfig,
    UserReport,
)
from app.modules.report import service as report_service
from app.modules.report import web_service
from app.modules.report.web_security import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_REQUEST_BYTES,
    SecurityError,
    parse_start_token,
    public_https_origin_for_bot,
    validate_init_data,
)

logger = logging.getLogger(__name__)

report_web_bp = Blueprint(
    'report_web',
    __name__,
    template_folder='templates',
    url_prefix='/report',
)


@report_web_bp.before_request
def bound_request_size():
    # Flask 3.1 supports per-request limits; allow multipart overhead for the
    # 10 MiB file even if a lower global default exists. Other APIs stay small.
    request.max_content_length = (MAX_UPLOAD_REQUEST_BYTES
                                  if request.endpoint == 'report_web.api_upload' else 64 * 1024)


@report_web_bp.errorhandler(413)
def upload_too_large(_error):
    return jsonify({'ok': False, 'code': 'too_large', 'error': '请求过大，请精简内容；照片不能超过 10 MB。'}), 413


def _json_error(err: SecurityError, status=403):
    return jsonify({'ok': False, 'code': err.code, 'error': err.message}), status


def _bot_token_for_clone(clone_id):
    """Return the current bot's token.  Never log or return this value."""
    if clone_id:
        clone = BotClone.query.get(int(clone_id))
        if clone is None:
            return None
        try:
            return clone.get_bot_token()
        except Exception:
            logger.exception('failed to read clone token')
            return None
    return os.getenv('TG_BOT_TOKEN') or None


def _bearer_token():
    header = request.headers.get('Authorization') or ''
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    return ''


def _csrf_from_request():
    return (request.headers.get('X-CSRF-Token') or '').strip()


def webhook_url_for_clone(clone_id):
    if not clone_id:
        return None
    clone = BotClone.query.get(int(clone_id))
    return clone.webhook_url if clone is not None else None


def resolve_https_origin(clone_id):
    from app.modules.core.routes import get_clone_webhook_url

    stored = webhook_url_for_clone(clone_id)
    generated = get_clone_webhook_url(clone_id, stored) if clone_id else None
    return public_https_origin_for_bot(
        clone_id,
        webhook_url=stored or generated,
        railway_domain=os.getenv('RAILWAY_PUBLIC_DOMAIN', ''),
    )


def _ptb_app_for_clone(clone_id):
    from app.modules.core import routes as core_routes
    from app import bot_clone_manager

    if clone_id:
        info = bot_clone_manager.active_clones.get(int(clone_id))
        return (info or {}).get('app'), core_routes.global_bot_loop
    return core_routes.global_ptb_app, core_routes.global_bot_loop


def send_to_admin_group(*, clone_id, report_id, caption, photo_bytes, photo_mime):
    """Persist a notification claim before I/O. Ambiguous sends never auto-retry."""
    from telegram.error import BadRequest, Forbidden
    ptb_app, loop = _ptb_app_for_clone(clone_id)
    if (ptb_app is None or loop is None or not loop.is_running()
            or not getattr(ptb_app, 'running', False)):
        return False, 'bot_not_ready', None
    admin_chat = (SystemConfig.get_value(
        report_service.scoped_key('admin_group_id', clone_id), '') or '').strip()
    if not admin_chat:
        return False, 'no_admin_group', None
    try:
        admin_chat = int(admin_chat)
    except (TypeError, ValueError):
        pass
    # No HTML splitting: a bounded exact caption is validated before accepting
    # submission, including the configured channel template (see below).
    if len(caption.encode('utf-16-le')) // 2 > (1024 if photo_bytes else 4096):
        return False, 'content_too_long', None
    changed = (UserReport.query.filter(
        UserReport.id == report_id, UserReport.owner_known.is_(True),
        UserReport.clone_id == clone_id, UserReport.status == 'pending',
        UserReport.admin_message_id.is_(None),
        UserReport.notify_status.in_(['pending', 'failed']),
    ).update({'notify_status': 'sending', 'notify_claimed_at': datetime.utcnow(),
              'notify_attempts': UserReport.notify_attempts + 1}, synchronize_session=False))
    db.session.commit()
    if changed != 1:
        return False, 'already_or_unknown', None
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ 通过', callback_data=f'audit_approve_{report_id}'),
        InlineKeyboardButton('❌ 驳回', callback_data=f'audit_reject_{report_id}'),
    ]])
    async def _send():
        if photo_bytes:
            name = 'report.jpg' if photo_mime == 'image/jpeg' else 'report.png'
            return await ptb_app.bot.send_photo(
                chat_id=admin_chat, photo=InputFile(photo_bytes, filename=name),
                caption=caption, parse_mode='HTML', reply_markup=keyboard)
        return await ptb_app.bot.send_message(
            chat_id=admin_chat, text=caption, parse_mode='HTML', reply_markup=keyboard)
    try:
        sent = asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=40)
    except (BadRequest, Forbidden):
        web_service.mark_notify_result(db, UserReport, report_id,
                                      status='failed', error='telegram_rejected')
        return False, 'send_failed', None
    except Exception:
        # Timeout/network failures can mean Telegram accepted the message. Do
        # not cancel-and-resend or release the claim: that risks duplicate audit
        # messages. Retain the original image and require reconciliation.
        web_service.mark_notify_result(db, UserReport, report_id,
                                      status='unknown', error='delivery_unknown_do_not_resend')
        return False, 'delivery_unknown', None
    file_id = sent.photo[-1].file_id if getattr(sent, 'photo', None) else None
    if photo_bytes and not file_id:
        web_service.mark_notify_result(db, UserReport, report_id,
                                      status='unknown', error='photo_file_id_missing')
        return False, 'delivery_unknown', None
    try:
        # Review binding and photo file_id persist together, before blob cleanup.
        web_service.mark_notify_result(
            db, UserReport, report_id, status='notified',
            admin_chat_id=admin_chat, admin_message_id=sent.message_id,
            photo_file_id=file_id)
    except Exception:
        db.session.rollback()
        # The durable 'sending' state blocks unsafe re-send even if this DB
        # write fails. Do not return success or remove the original image.
        return False, 'delivery_unknown', None
    return True, 'ok', file_id


def review_caption(report):
    answers = report_service.parse_answers(report)
    template = SystemConfig.get_value(
        report_service.scoped_key('report_push_template', report.clone_id), '')
    body = report_service.build_push_caption(template, report.id, answers)
    target = report_service.find_member_for_bot(
        GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
        bot_clone_id=report.clone_id, user_id=report.user_id)
    submitter = report_service.find_member_for_bot(
        GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
        bot_clone_id=report.clone_id, user_id=report.submitter_id)
    return report_service.build_contact_caption(
        body, report_service.compact_contact_identity(report.user_id, member=target),
        report_service.compact_contact_identity(report.submitter_id, member=submitter) if report.submitter_id else None,
        review_report_id=report.id)



def retry_report_notification(report):
    blobs = web_service.uploads_for_report(ReportWebUpload, report.id)
    blob = blobs[0] if blobs else None
    ok, reason, file_id = send_to_admin_group(
        clone_id=report.clone_id, report_id=report.id, caption=review_caption(report),
        photo_bytes=blob.data if blob else None,
        photo_mime=blob.content_type if blob else None)
    if ok and (blob is None or file_id):
        web_service.delete_report_uploads(db, ReportWebUpload, report.id)
    return ok, reason


@report_web_bp.route('/app')
def webapp_page():
    """Shell page.  Sensitive data is loaded only after a validated session."""
    return render_template('webapp.html')


@report_web_bp.route('/api/session', methods=['POST'])
@limiter.limit('30 per hour')
def api_session():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'code': 'bad_input', 'error': '请求格式无效。'}), 400
    init_data = payload.get('initData') or ''
    start_token = payload.get('startToken') or request.args.get('s') or ''
    try:
        start = parse_start_token(start_token)
        token = _bot_token_for_clone(start['clone_id'])
        if not token:
            raise SecurityError('no_bot', '当前机器人不可用。')
        verified = validate_init_data(init_data, token)
    except SecurityError as err:
        return _json_error(err)

    web_service.purge_expired_web_state(db, ReportWebSession, ReportWebUpload)
    created = web_service.create_web_session(
        db, ReportWebSession,
        telegram_user_id=verified['user']['id'],
        clone_id=start['clone_id'],
        target_user_id=start['target_user_id'],
        mode=start['mode'],
    )
    return jsonify({
        'ok': True,
        'token': created['token'],
        'csrf': created['csrf'],
        'submit_key': created['submit_key'] if start['mode'] == 'write' else None,
        'mode': start['mode'],
        'target_user_id': start['target_user_id'],
    })


def _load_session_or_error():
    try:
        return web_service.load_valid_session(ReportWebSession, _bearer_token())
    except SecurityError as err:
        return err


@report_web_bp.route('/api/bootstrap', methods=['GET'])
@limiter.limit('60 per hour')
def api_bootstrap():
    session_row = _load_session_or_error()
    if isinstance(session_row, SecurityError):
        return _json_error(session_row)
    try:
        if session_row.mode == 'view':
            data = web_service.view_payload_for_session(
                UserReport, SystemConfig, GroupMember, GroupUser, BotGroup, session_row,
                page=request.args.get('page', 1), per_page=request.args.get('per_page', 20),
                sort=request.args.get('sort', 'newest'),
            )
        else:
            data = web_service.write_bootstrap(
                SystemConfig, GroupMember, GroupUser, BotGroup, session_row,
            )
    except SecurityError as err:
        return _json_error(err)
    return jsonify({'ok': True, 'mode': session_row.mode, **data})


@report_web_bp.route('/api/reports/<int:report_id>')
@limiter.limit('120 per hour')
def api_report_detail(report_id):
    session_row = _load_session_or_error()
    if isinstance(session_row, SecurityError):
        return _json_error(session_row)
    try:
        report = web_service.view_report_detail(UserReport, SystemConfig, session_row, report_id)
    except SecurityError as err:
        return _json_error(err, status=404 if err.code == 'not_found' else 403)
    return jsonify({'ok': True, 'report': report})


@report_web_bp.route('/api/upload', methods=['POST'])
@limiter.limit('20 per hour')
def api_upload():
    session_row = _load_session_or_error()
    if isinstance(session_row, SecurityError):
        return _json_error(session_row)
    try:
        web_service.require_csrf(session_row, _csrf_from_request())
        web_service.require_write_session(session_row)
        upload = request.files.get('photo')
        if upload is None:
            raise SecurityError('bad_image', '请选择一张图片。')
        data = upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise SecurityError('bad_image', '请上传 JPG、PNG、WebP 或 GIF 图片，且不超过 10 MB。')
        result = web_service.store_upload(db, ReportWebUpload, session_row, data)
    except SecurityError as err:
        return _json_error(err, status=400)
    return jsonify({'ok': True, **result})


@report_web_bp.route('/api/submit', methods=['POST'])
@limiter.limit('15 per hour')
def api_submit():
    session_row = _load_session_or_error()
    if isinstance(session_row, SecurityError):
        return _json_error(session_row)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'code': 'bad_input', 'error': '请求格式无效。'}), 400
    try:
        web_service.require_csrf(session_row, _csrf_from_request() or payload.get('csrf') or '')
        web_service.require_write_session(session_row)
        if session_row.submitted_report_id:
            key = payload.get('submit_key')
            from .web_security import sha256_hex
            if not isinstance(key, str) or len(key) > 256 or not web_service.hmac_eq(session_row.submit_key_hash, sha256_hex(key)):
                raise SecurityError('bad_submit_key', '提交凭证无效。')
            return jsonify({'ok': True, 'duplicate': True,
                            'report_id': session_row.submitted_report_id,
                            'message': f'报告已保存（#{session_row.submitted_report_id}），请勿重复提交。'})
        cfg = web_service.load_questions_and_flags(SystemConfig, session_row.clone_id)
        answers = web_service.normalize_answers(cfg['questions'], payload.get('answers'))
        upload_token = payload.get('upload_token') or ''
        upload = web_service.load_upload(ReportWebUpload, session_row, upload_token)
        if cfg['require_photo'] and upload is None:
            raise SecurityError('photo_required', '请先上传照片后再提交。')

        # Check both review and future channel body before accepting a report.
        # Bound the rendered HTML (conservatively including tags/entities) so
        # neither caption nor text can become permanently undeliverable.
        provisional = UserReport(**report_service.new_report_values(
            user_id=session_row.target_user_id, submitter_id=session_row.telegram_user_id,
            raw_answers=[item['answer'] for item in answers], answers=answers,
            photo_file_id=None, bot_clone_id=session_row.clone_id))
        provisional.id = 9223372036854775807
        caption = review_caption(provisional)
        limit = 1024 if upload is not None else 4096
        if len(caption.encode('utf-16-le')) // 2 > limit:
            raise SecurityError('content_too_long',
                f'问题、答案、身份和模板合计超过 Telegram {limit} 字限制，请缩短答案；尚未提交。')

        state, report, session_row = web_service.commit_web_submission(
            db, ReportWebSession, UserReport, ReportWebUpload,
            session_id=session_row.id, submit_key=payload.get('submit_key'),
            answers=answers, raw_answers=[item['answer'] for item in answers],
            upload=upload,
        )
        if state == 'duplicate':
            report_id = report.id if hasattr(report, 'id') else report
            return jsonify({'ok': True, 'duplicate': True, 'report_id': report_id,
                            'message': f'报告已保存（#{report_id}），请勿重复提交。'})

        ok, reason = retry_report_notification(report)
        if ok:
            message = f'报告已提交（#{report.id}）。管理员审核通过后将发布到报告频道。'
        elif reason == 'no_admin_group':
            message = f'报告已提交（#{report.id}），但未配置审核群，请联系管理员。'
        elif reason == 'bot_not_ready':
            message = f'报告已保存（#{report.id}），通知审核群暂未发出，请稍后再由管理员查看，勿重复提交。'
        else:
            message = f'报告已提交（#{report.id}），但通知审核群失败，请联系管理员查看，勿重复提交。'
        return jsonify({'ok': True, 'report_id': report.id, 'notified': ok, 'message': message})
    except SecurityError as err:
        return _json_error(err, status=400)
    except Exception:
        logger.exception('report web submit failed')
        db.session.rollback()
        return jsonify({'ok': False, 'code': 'server', 'error': '提交失败，请稍后再试。'}), 500
