"""Domain logic for the report Mini App and admin read-only list."""

from __future__ import annotations

import hmac as hmac_mod
import re
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text

from app.modules.report import service as report_service
from app.modules.report.web_security import (
    MAX_ANSWER_CHARS,
    SESSION_TTL_SECONDS,
    SecurityError,
    new_urlsafe_token,
    normalize_image,
    sha256_hex,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SHANGHAI_TZ_NAME = 'Asia/Shanghai'
SHANGHAI_TZ = ZoneInfo(SHANGHAI_TZ_NAME)
TIMEZONE_LABEL = '北京时间'
VIEW_PAGE_DEFAULT = 20
VIEW_PAGE_MAX = 50

# Title may come from a short answer whose question looks like a name field.
_NAME_QUESTION_HINTS = ('艺名', '老师姓名', '老师名字', '老师名', '教师姓名', '姓名', '名字', '昵称', '真名', '艺人')
_NAME_QUESTION_EXACT = {'老师', '教师', '导师', '联系人'}
# Do not treat these as a person's name, even when they are the first question.
_TITLE_EXCLUDE_HINTS = (
    '地区', '区域', '城市', '省份', '地址', '地点', '时间', '日期',
    '电话', '手机', '微信', '备注', '说明', '描述', '现象', '结果',
    '故障', '内容', '详情', '评价', '评分',
)
_TITLE_MAX = 24
_SUMMARY_LINE_MAX = 80


def plain_user_identity(user_id, member=None) -> str:
    """Plain-text identity for the Mini App JSON (frontend escapes it)."""
    parts = []
    first = last = username = ''
    if member is not None:
        first = getattr(member, 'first_name', None) or ''
        last = getattr(member, 'last_name', None) or ''
        username = getattr(member, 'username', None) or ''
    name = ' '.join(filter(None, [first, last])).strip()
    if name:
        parts.append(name)
    if username:
        parts.append('@' + str(username).lstrip('@'))
    parts.append(f'ID: {user_id}')
    return ' ｜ '.join(parts)


def as_utc(dt):
    """Treat naive datetimes as UTC (application and production Postgres convention)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_shanghai(dt, *, with_time=True, style='full', with_label=True) -> str:
    """Format a stored timestamp in Asia/Shanghai and label it 北京时间."""
    if dt is None:
        return ''
    local = as_utc(dt).astimezone(SHANGHAI_TZ)
    if style == 'short':
        text = local.strftime('%m-%d %H:%M')
    elif with_time:
        text = local.strftime('%Y-%m-%d %H:%M')
    else:
        text = local.strftime('%Y-%m-%d')
    if with_label:
        return f'{text}（{TIMEZONE_LABEL}）'
    return text


def display_question_text(text: str) -> str:
    """Strip an outer 【】 wrapper and a trailing colon for display only."""
    t = (text or '').strip()
    match = re.fullmatch(r'【([^】]*)】\s*[：:]*', t)
    if match:
        t = match.group(1).strip()
    return t.rstrip('：:').strip()


def _clip_display(text: str, limit: int) -> str:
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + '…'


def list_title_and_summary(answers, report_id):
    """Pick a short recognizable name title; summarize other non-empty answers.

    Never uses a region/time-like first question as a person's name.  Falls
    back to the report number when no name-like field is present.
    """
    items = []
    for raw in answers or []:
        if not isinstance(raw, dict):
            continue
        question = display_question_text(raw.get('question') or '')
        answer = re.sub(r'\s+', ' ', str(raw.get('answer') or '')).strip()
        items.append((question, answer))

    title = None
    title_index = None
    for index, (question, answer) in enumerate(items):
        if not answer or len(answer) > 40:
            continue
        if any(hint in question for hint in _TITLE_EXCLUDE_HINTS):
            continue
        if question in _NAME_QUESTION_EXACT or any(hint in question for hint in _NAME_QUESTION_HINTS):
            title = _clip_display(answer, _TITLE_MAX)
            title_index = index
            break
    if not title:
        title = f'报告 #{report_id}'

    lines = []
    for index, (_question, answer) in enumerate(items):
        if not answer or index == title_index:
            continue
        lines.append(_clip_display(answer, _SUMMARY_LINE_MAX))
        if len(lines) >= 2:
            break
    return title, '\n'.join(lines)


def shanghai_day_range_to_utc(date_from_s, date_to_s):
    """Convert Asia/Shanghai calendar dates to naive-UTC bounds for filtering."""
    date_from = date_to = None
    try:
        if date_from_s:
            start = datetime.strptime(date_from_s, '%Y-%m-%d')
            date_from = start.replace(tzinfo=SHANGHAI_TZ).astimezone(timezone.utc).replace(tzinfo=None)
        if date_to_s:
            end_day = datetime.strptime(date_to_s, '%Y-%m-%d')
            end = datetime(end_day.year, end_day.month, end_day.day, 23, 59, 59, 999999, tzinfo=SHANGHAI_TZ)
            date_to = end.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError as exc:
        raise SecurityError('bad_date', '日期格式无效。') from exc
    return date_from, date_to


def ensure_report_web_schema(db) -> list:
    """Create Mini App session/upload tables if missing.  Idempotent, no data loss."""
    from sqlalchemy import inspect as sa_inspect

    dialect = getattr(getattr(db.engine, 'dialect', None), 'name', '') or ''
    inspector = sa_inspect(db.engine)
    applied = []
    existing = set(inspector.get_table_names())
    for stmt, table in report_web_schema_statements(dialect):
        if table in existing and 'CREATE TABLE' in stmt:
            continue
        try:
            with db.engine.connect() as conn:
                conn.execute(sa_text(stmt))
                conn.commit()
            applied.append(stmt)
        except Exception as exc:
            logger.debug('ensure_report_web_schema skip %s: %s', stmt, exc)
    return applied


def report_web_schema_statements(dialect_name: str):
    """Return (ddl, table_name) pairs.  IF NOT EXISTS on PostgreSQL and SQLite."""
    blob = 'BYTEA' if dialect_name == 'postgresql' else 'BLOB'
    id_type = 'SERIAL PRIMARY KEY' if dialect_name == 'postgresql' else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    big = 'BIGINT' if dialect_name == 'postgresql' else 'INTEGER'
    ts = 'TIMESTAMP'
    sessions = f'''CREATE TABLE IF NOT EXISTS report_web_sessions (
        id {id_type},
        token_hash VARCHAR(64) NOT NULL UNIQUE,
        telegram_user_id {big} NOT NULL,
        clone_id INTEGER NULL,
        target_user_id {big} NOT NULL,
        mode VARCHAR(16) NOT NULL,
        csrf_hash VARCHAR(64) NOT NULL,
        submit_key_hash VARCHAR(64) NULL UNIQUE,
        submitted_report_id INTEGER NULL,
        created_at {ts} DEFAULT CURRENT_TIMESTAMP,
        expires_at {ts} NOT NULL
    )'''
    uploads = f'''CREATE TABLE IF NOT EXISTS report_web_uploads (
        id {id_type},
        session_id INTEGER NOT NULL REFERENCES report_web_sessions(id),
        upload_token_hash VARCHAR(64) NOT NULL UNIQUE,
        data {blob} NOT NULL,
        content_type VARCHAR(64) NOT NULL,
        size INTEGER NOT NULL,
        created_at {ts} DEFAULT CURRENT_TIMESTAMP,
        report_id INTEGER NULL
    )'''
    idx1 = 'CREATE INDEX IF NOT EXISTS ix_report_web_sessions_telegram_user_id ON report_web_sessions (telegram_user_id)'
    idx2 = 'CREATE INDEX IF NOT EXISTS ix_report_web_sessions_expires_at ON report_web_sessions (expires_at)'
    idx3 = 'CREATE INDEX IF NOT EXISTS ix_report_web_uploads_session_id ON report_web_uploads (session_id)'
    idx4 = 'CREATE INDEX IF NOT EXISTS ix_report_web_uploads_report_id ON report_web_uploads (report_id)'
    if dialect_name == 'sqlite':
        alter = 'ALTER TABLE report_web_uploads ADD COLUMN report_id INTEGER'
    else:
        alter = 'ALTER TABLE report_web_uploads ADD COLUMN IF NOT EXISTS report_id INTEGER'
    return (
        (sessions, 'report_web_sessions'),
        (uploads, 'report_web_uploads'),
        (idx1, 'report_web_sessions'),
        (idx2, 'report_web_sessions'),
        (idx3, 'report_web_uploads'),
        (alter, 'report_web_uploads'),
        (idx4, 'report_web_uploads'),
    )


def purge_expired_web_state(db, ReportWebSession, ReportWebUpload, *, now=None) -> int:
    """Delete expired sessions and unattached uploads.  Keep pending notify blobs."""
    now = now or datetime.utcnow()
    sess = db.session
    pending_ids = {
        row.session_id
        for row in sess.query(ReportWebUpload).filter(ReportWebUpload.report_id.isnot(None)).all()
        if row.session_id is not None
    }
    expired_ids = [
        row.id for row in
        sess.query(ReportWebSession).filter(ReportWebSession.expires_at <= now).all()
        if row.id not in pending_ids
    ]
    if not expired_ids:
        return 0
    sess.query(ReportWebUpload).filter(
        ReportWebUpload.session_id.in_(expired_ids),
        ReportWebUpload.report_id.is_(None),
    ).delete(synchronize_session=False)
    deleted = sess.query(ReportWebSession).filter(
        ReportWebSession.id.in_(expired_ids)
    ).delete(synchronize_session=False)
    sess.commit()
    return deleted


def create_web_session(db, ReportWebSession, *, telegram_user_id, clone_id,
                       target_user_id, mode, ttl=SESSION_TTL_SECONDS, now=None):
    now = now or datetime.utcnow()
    token = new_urlsafe_token(32)
    csrf = new_urlsafe_token(24)
    submit_key = new_urlsafe_token(24)
    row = ReportWebSession(
        token_hash=sha256_hex(token),
        telegram_user_id=int(telegram_user_id),
        clone_id=clone_id,
        target_user_id=int(target_user_id),
        mode=mode,
        csrf_hash=sha256_hex(csrf),
        submit_key_hash=sha256_hex(submit_key) if mode == 'write' else None,
        submitted_report_id=None,
        expires_at=now + timedelta(seconds=int(ttl)),
    )
    db.session.add(row)
    db.session.commit()
    return {
        'session': row,
        'token': token,
        'csrf': csrf,
        'submit_key': submit_key if mode == 'write' else None,
    }


def load_valid_session(ReportWebSession, token: str, *, now=None):
    if not isinstance(token, str) or not token or len(token) > 256:
        raise SecurityError('no_session', '会话无效，请从机器人重新打开。')
    now = now or datetime.utcnow()
    row = ReportWebSession.query.filter_by(token_hash=sha256_hex(token)).first()
    if row is None or row.expires_at <= now:
        raise SecurityError('no_session', '会话无效或已过期，请从机器人重新打开。')
    return row


def hmac_eq(stored_hex: str, computed_hex: str) -> bool:
    if not stored_hex or not computed_hex:
        return False
    return hmac_mod.compare_digest(str(stored_hex), str(computed_hex))


def require_csrf(session_row, csrf: str):
    if not isinstance(csrf, str) or len(csrf) > 256 or not csrf or not hmac_eq(session_row.csrf_hash, sha256_hex(csrf)):
        raise SecurityError('bad_csrf', '请求无效，请刷新后重试。')


def require_write_session(session_row):
    if session_row.mode != 'write':
        raise SecurityError('bad_mode', '当前会话不能提交报告。')


def require_view_session(session_row):
    if session_row.mode != 'view':
        raise SecurityError('bad_mode', '当前会话不能查看报告。')


def load_questions_and_flags(SystemConfig, clone_id):
    """Return questions list plus require_photo / photo_prompt for this bot."""

    def _val(key, default):
        return SystemConfig.get_value(report_service.scoped_key(key, clone_id), default)

    raw = _val('report_questions', '')
    default_questions = [
        {"text": "请问故障发生的时间是？", "required": True, "hint": "例如：2024-01-15 14:30"},
        {"text": "请描述具体的故障现象？", "required": True, "hint": "例如：设备无法启动，屏幕显示错误代码 E01"},
        {"text": "最终的处理结果是什么？", "required": True, "hint": "例如：已更换电源模块，设备恢复正常"},
    ]
    try:
        questions = json.loads(raw) if raw else default_questions
        if not isinstance(questions, list) or not questions:
            questions = default_questions
    except (ValueError, TypeError):
        questions = default_questions
    cleaned = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        text = (item.get('text') or '').strip()
        if not text:
            continue
        cleaned.append({
            'text': text,
            'required': bool(item.get('required', True)),
            'hint': (item.get('hint') or '').strip(),
        })
    if not cleaned:
        cleaned = default_questions
    require_photo = (_val('report_require_photo', 'true') or 'true').lower() != 'false'
    photo_prompt = _val('report_photo_prompt', '请发送预约聊天截图或付款截图 📷（必填）')
    return {
        'questions': cleaned,
        'require_photo': require_photo,
        'photo_prompt': photo_prompt,
    }


def normalize_answers(questions: list, answers: list) -> list:
    if not isinstance(answers, list):
        raise SecurityError('bad_answers', '请完整填写全部问题后再提交。')
    if len(answers) != len(questions):
        raise SecurityError('bad_answers', '请完整填写全部问题后再提交。')
    out = []
    for q, raw in zip(questions, answers):
        if raw is not None and not isinstance(raw, (str, int, float)):
            raise SecurityError('bad_answers', '答案格式无效，请重新填写。')
        text = '' if raw is None else str(raw).strip()
        if q.get('required', True) and not text:
            raise SecurityError('bad_answers', f'请填写：{q["text"]}')
        if len(text) > MAX_ANSWER_CHARS:
            raise SecurityError(
                'bad_answers',
                f'单题答案不能超过 {MAX_ANSWER_CHARS} 字，请缩短后再提交。',
            )
        out.append({'question': q['text'], 'answer': text})
    return out


def store_upload(db, ReportWebUpload, session_row, data: bytes):
    require_write_session(session_row)
    if session_row.submitted_report_id:
        raise SecurityError('already_submitted', '报告已提交，不能修改附件。')
    normalized, mime = normalize_image(data)
    db.session.query(ReportWebUpload).filter_by(session_id=session_row.id, report_id=None).delete(
        synchronize_session=False
    )
    token = new_urlsafe_token(24)
    row = ReportWebUpload(
        session_id=session_row.id,
        upload_token_hash=sha256_hex(token),
        data=normalized,
        content_type=mime,
        size=len(normalized),
        report_id=None,
    )
    db.session.add(row)
    db.session.commit()
    return {'upload_token': token, 'content_type': mime, 'size': len(normalized)}


def load_upload(ReportWebUpload, session_row, upload_token: str):
    if not upload_token or not isinstance(upload_token, str) or len(upload_token) > 256:
        return None
    return ReportWebUpload.query.filter_by(
        session_id=session_row.id,
        upload_token_hash=sha256_hex(upload_token),
    ).first()


def uploads_for_report(ReportWebUpload, report_id):
    return ReportWebUpload.query.filter_by(report_id=int(report_id)).all()


def delete_report_uploads(db, ReportWebUpload, report_id):
    db.session.query(ReportWebUpload).filter_by(report_id=int(report_id)).delete(
        synchronize_session=False
    )
    db.session.commit()


def delete_session_uploads(db, ReportWebUpload, session_id: int):
    db.session.query(ReportWebUpload).filter(
        ReportWebUpload.session_id == session_id,
        ReportWebUpload.report_id.is_(None),
    ).delete(synchronize_session=False)
    db.session.commit()


def view_payload_for_session(UserReport, SystemConfig, GroupMember, GroupUser, BotGroup,
                             session_row, *, page=1, per_page=VIEW_PAGE_DEFAULT, sort='newest'):
    require_view_session(session_row)
    try:
        page = int(page)
        per_page = int(per_page)
    except (TypeError, ValueError) as exc:
        raise SecurityError('bad_page', '页码无效。') from exc
    if page < 1 or page > 100000:
        raise SecurityError('bad_page', '页码无效。')
    per_page = min(max(per_page, 1), VIEW_PAGE_MAX)
    sort = report_service.normalize_view_sort(sort)
    query = report_service.visible_reports_query(
        UserReport, session_row.target_user_id, session_row.clone_id, sort=sort,
    )
    total = query.count()
    reports = query.offset((page - 1) * per_page).limit(per_page).all()
    member = report_service.find_member_for_bot(
        GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
        bot_clone_id=session_row.clone_id, user_id=session_row.target_user_id,
    )
    contact = report_service.resolve_contact_display(
        session_row.target_user_id, member=member,
    )
    items = []
    for r in reports:
        title, summary = list_title_and_summary(report_service.parse_answers(r), r.id)
        items.append({
            'id': r.id,
            'title': title,
            'summary': summary,
            'date': format_shanghai(r.created_at, style='short', with_label=False),
        })
    return {
        'target_identity': f"{contact['name']} · {contact['user_id']}",
        'target_contact': contact,
        'target_user_id': session_row.target_user_id,
        'reports': items,
        'page': page,
        'per_page': per_page,
        'total': total,
        'has_more': (page * per_page) < total,
        'sort': sort,
        'timezone': TIMEZONE_LABEL,
    }


def view_report_detail(UserReport, SystemConfig, session_row, report_id):
    require_view_session(session_row)
    report = report_service.visible_reports_query(
        UserReport, session_row.target_user_id, session_row.clone_id
    ).filter(UserReport.id == report_id).first()
    if report is None:
        raise SecurityError('not_found', '报告不存在或当前入口不可查看。')
    fallback = SystemConfig.get_value(report_service.scoped_key('report_channel', session_row.clone_id), '')
    channel = report_service.channel_identity_for_view(report, fallback)
    link = ''
    if report.channel_msg_id and channel and not report.channel_msg_deleted:
        link = report_service.build_channel_link(channel, report.channel_msg_id)
    answers = report_service.parse_answers(report)
    title, _summary = list_title_and_summary(answers, report.id)
    return {
        'id': report.id,
        'title': title,
        'date': format_shanghai(report.created_at),
        'answers': [
            {
                'question': display_question_text(a.get('question', '')),
                'answer': a.get('answer', ''),
            }
            for a in answers
        ],
        'channel_link': link,
    }


def write_bootstrap(SystemConfig, GroupMember, GroupUser, BotGroup, session_row):
    require_write_session(session_row)
    cfg = load_questions_and_flags(SystemConfig, session_row.clone_id)
    member = report_service.find_member_for_bot(
        GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
        bot_clone_id=session_row.clone_id, user_id=session_row.target_user_id,
    )
    identity = plain_user_identity(session_row.target_user_id, member=member)
    return {
        'target_identity': identity,
        'target_user_id': session_row.target_user_id,
        'questions': cfg['questions'],
        'require_photo': cfg['require_photo'],
        'photo_prompt': cfg['photo_prompt'],
        'already_submitted': bool(session_row.submitted_report_id),
        'submitted_report_id': session_row.submitted_report_id,
        'max_answer_chars': MAX_ANSWER_CHARS,
        'max_upload_bytes': 10 * 1024 * 1024,
        'timezone': TIMEZONE_LABEL,
    }


def _lock_session_row(db, ReportWebSession, session_id):
    """Lock the write session row for this transaction (Postgres FOR UPDATE)."""
    dialect = getattr(getattr(db.engine, 'dialect', None), 'name', '') or ''
    query = db.session.query(ReportWebSession).filter(ReportWebSession.id == int(session_id))
    if dialect == 'postgresql':
        query = query.with_for_update()
    elif dialect == 'sqlite':
        # SQLite has no row locks. Take the reserved write lock before reading
        # the binding. An existing DBAPI transaction already belongs to this
        # unit of work; the conditional binding below remains the final CAS.
        connection = db.session.connection().connection.driver_connection
        if not connection.in_transaction:
            db.session.execute(sa_text('BEGIN IMMEDIATE'))
    else:
        raise SecurityError('unsupported_database', '当前数据库不支持安全提交。')
    return query.populate_existing().one_or_none()


def commit_web_submission(db, ReportWebSession, UserReport, ReportWebUpload, *,
                          session_id, submit_key, answers, raw_answers,
                          upload=None):
    """Atomically insert the report and bind it to the session in one commit.

    Concurrent callers: the winner commits one report; losers roll back and
    return the same report id.  A crash before commit rolls back both the
    row and the binding, so a retry is safe.
    """
    sess = db.session
    try:
        row = _lock_session_row(db, ReportWebSession, session_id)
        if row is None or row.expires_at <= datetime.utcnow():
            raise SecurityError('no_session', '会话无效或已过期，请从机器人重新打开。')
        require_write_session(row)
        if not isinstance(submit_key, str) or len(submit_key) > 256 or not submit_key or not hmac_eq(row.submit_key_hash or '', sha256_hex(submit_key)):
            raise SecurityError('bad_submit_key', '提交凭证无效，请刷新后重试。')
        if row.submitted_report_id:
            existing = sess.query(UserReport).filter_by(id=row.submitted_report_id).one_or_none()
            sess.commit()
            return 'duplicate', existing or row.submitted_report_id, row

        report = UserReport(**report_service.new_report_values(
            user_id=row.target_user_id,
            submitter_id=row.telegram_user_id,
            raw_answers=raw_answers,
            answers=answers,
            photo_file_id=None,
            bot_clone_id=row.clone_id,
        ))
        report.created_at = datetime.utcnow()
        report.notify_status = 'pending'
        report.notify_attempts = 0
        report.notify_claimed_at = None
        report.notify_error = None
        sess.add(report)
        sess.flush()

        updated = (
            sess.query(ReportWebSession)
            .filter(
                ReportWebSession.id == row.id,
                ReportWebSession.submitted_report_id.is_(None),
            )
            .update({'submitted_report_id': report.id}, synchronize_session=False)
        )
        if updated != 1:
            sess.rollback()
            again = sess.query(ReportWebSession).filter_by(id=session_id).one_or_none()
            existing = None
            if again and again.submitted_report_id:
                existing = sess.query(UserReport).filter_by(id=again.submitted_report_id).one_or_none()
            return 'duplicate', existing or (again.submitted_report_id if again else None), again

        if upload is not None:
            upload_query = sess.query(ReportWebUpload).filter_by(
                id=upload.id, session_id=row.id, upload_token_hash=upload.upload_token_hash,
                report_id=None)
            if db.engine.dialect.name == 'postgresql':
                upload_query = upload_query.with_for_update()
            upload_row = upload_query.populate_existing().one_or_none()
            if upload_row is None:
                raise SecurityError('bad_image', '附件已变化，请重新上传后提交。')
            upload_row.report_id = report.id
        sess.refresh(row)
        sess.commit()
        return 'ok', report, row
    except SecurityError:
        sess.rollback()
        raise
    except Exception:
        sess.rollback()
        raise


def mark_notify_result(db, UserReport, report_id, *, status, error=None,
                       admin_chat_id=None, admin_message_id=None, photo_file_id=None):
    report = db.session.query(UserReport).filter_by(id=int(report_id)).one_or_none()
    if report is None:
        return None
    report.notify_status = status
    report.notify_error = (error or '')[:200] or None
    if status == 'notified':
        if admin_chat_id is not None:
            report.admin_chat_id = str(admin_chat_id)
        if admin_message_id is not None:
            report.admin_message_id = int(admin_message_id)
        if photo_file_id:
            report.photo_file_id = photo_file_id
        report.notify_error = None
    db.session.commit()
    return report


def report_has_pending_attachment(ReportWebUpload, report) -> bool:
    if report is None:
        return False
    if report.photo_file_id:
        return True
    return ReportWebUpload.query.filter_by(report_id=report.id).first() is not None


def can_retry_admin_notify(report) -> bool:
    if report is None or report.status != 'pending':
        return False
    if report.admin_message_id:
        return False
    return (report.notify_status or 'pending') in ('pending', 'failed')


def admin_reports_query(UserReport, *, clone_id, target_user_id=None, status=None,
                        date_from=None, date_to=None):
    """Owned reports only (including pending/rejected).  Never includes legacy unassigned."""
    q = UserReport.query.filter(UserReport.owner_known.is_(True))
    if clone_id:
        q = q.filter(UserReport.clone_id == int(clone_id))
    else:
        q = q.filter(UserReport.clone_id.is_(None))
    if target_user_id:
        q = q.filter(UserReport.user_id == int(target_user_id))
    if status:
        q = q.filter(UserReport.status == status)
    if date_from is not None:
        q = q.filter(UserReport.created_at >= date_from)
    if date_to is not None:
        q = q.filter(UserReport.created_at <= date_to)
    return q.order_by(UserReport.id.desc())


def admin_may_view_report(report, bot_clone_id) -> bool:
    return report_service.report_ownership(report, bot_clone_id) == report_service.OWNED
