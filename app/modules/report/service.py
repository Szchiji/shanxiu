"""Report-management domain logic (ownership, audit, captions, schema).

Telegram I/O stays in ``bot.py``.  These helpers are deliberately free of
python-telegram-bot types so unit tests can cover isolation, auth decisions,
escaping, retries and migrations without a live bot or production database.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import or_

logger = logging.getLogger(__name__)

# Telegram's anonymous-admin identity when a group admin hides their name.
ANONYMOUS_ADMIN_USER_ID = 1087968824

LEGACY_UNASSIGNED = 'legacy_unassigned'
OWNED = 'owned'
FOREIGN = 'foreign'

POINTS_REASON_PREFIX = '报告审核通过 #'

# Lease for the approve claim.  If a worker dies mid-publish, another may
# retry after this many seconds.  Not a permanent processing status.
APPROVE_CLAIM_SECONDS = 120

# ---------------------------------------------------------------------------
# Config key scoping
# ---------------------------------------------------------------------------

def scoped_key(key: str, clone_id) -> str:
    """Return a clone-scoped config key, or the bare key for the main bot."""
    if clone_id:
        return f'clone_{clone_id}_{key}'
    return key


def same_clone_id(left, right) -> bool:
    """Treat None / 0-less NULL as the main bot on both sides."""
    def _norm(value):
        if value is None or value == '':
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return value
        return None if value == 0 else value

    return _norm(left) == _norm(right)


# ---------------------------------------------------------------------------
# Chat id comparison
# ---------------------------------------------------------------------------

def normalize_chat_id(value) -> Optional[str]:
    """Canonical string for a Telegram chat id or public @username."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    digits = raw.lstrip('-')
    if digits.isdigit():
        try:
            return str(int(raw))
        except (TypeError, ValueError):
            return raw
    return raw.removeprefix('@').lower()


def chat_ids_equal(left, right) -> bool:
    a, b = normalize_chat_id(left), normalize_chat_id(right)
    return a is not None and a == b


# ---------------------------------------------------------------------------
# HTML escaping (user content only; templates stay trusted)
# ---------------------------------------------------------------------------

def escape_user_html(value) -> str:
    """Escape a user-provided or display-name string for Telegram HTML."""
    if value is None:
        return ''
    return html.escape(str(value), quote=True)


def build_answers_block(answers: list) -> str:
    """Format Q&A pairs with escaped question/answer text."""
    lines = []
    for item in answers or []:
        q = escape_user_html(item.get('question', ''))
        a = escape_user_html(item.get('answer', ''))
        lines.append(f'❓ {q}\n💬 {a}')
    return '\n\n'.join(lines)


def build_push_caption(template: str, report_id, answers: list) -> str:
    """Fill a trusted HTML template, escaping user answers and {qN} values.

    ``template`` is admin-configured and may contain legal Telegram HTML.
    User answers (including ``{answers}`` and ``{q1}``…``{qN}``) are escaped.
    """
    template = template or '📋 <b>认证用户报告 #{report_id}</b>\n\n{answers}'
    answers_block = build_answers_block(answers)
    result = template.replace('{report_id}', escape_user_html(report_id)).replace(
        '{answers}', answers_block
    )
    for i, item in enumerate(answers or [], 1):
        result = result.replace(f'{{q{i}}}', escape_user_html(item.get('answer', '')))
    return result


def format_user_identity(user_id, member=None, from_user=None) -> str:
    """Return an HTML identity string with escaped names/@usernames."""
    parts = []
    first_name = last_name = username = None
    if from_user is not None:
        first_name = getattr(from_user, 'first_name', None) or ''
        last_name = getattr(from_user, 'last_name', None) or ''
        username = getattr(from_user, 'username', None)
    elif member is not None:
        first_name = getattr(member, 'first_name', None) or ''
        last_name = getattr(member, 'last_name', None) or ''
        username = getattr(member, 'username', None)
    if first_name or last_name:
        name = ' '.join(filter(None, [first_name, last_name]))
        if name:
            parts.append(escape_user_html(name))
    if username:
        parts.append('@' + escape_user_html(str(username).lstrip('@')))
    parts.append(f'ID: <code>{escape_user_html(user_id)}</code>')
    return ' ｜ '.join(parts)


def build_channel_link(channel: str, msg_id: int) -> str:
    """Build a t.me / t.me/c link for a channel message."""
    chan = (channel or '').strip()
    stripped = chan.lstrip('-')
    if stripped.isdigit():
        numeric_id = stripped
        if numeric_id.startswith('100'):
            numeric_id = numeric_id[3:]
        return f'https://t.me/c/{numeric_id}/{msg_id}'
    chan = chan.removeprefix('@')
    return f'https://t.me/{chan}/{msg_id}'


def channel_identity_for_view(report, fallback_channel: str = '') -> str:
    """Prefer the channel recorded at publish time; never use another bot's config."""
    stored = (getattr(report, 'channel_chat_id', None) or '').strip()
    if stored:
        return stored
    return (fallback_channel or '').strip()


# ---------------------------------------------------------------------------
# Conversation /start intent (used so mid-report view links work)
# ---------------------------------------------------------------------------

_START_REPORT_RE = re.compile(r'^/start(?:@[A-Za-z0-9_]+)?\s+report_(\d+)', re.I)
_START_VIEW_RE = re.compile(r'^/start(?:@[A-Za-z0-9_]+)?\s+view_(\d+)', re.I)


def parse_start_target_user_id(text: str, kind: str):
    """Extract the target user id from ``/start[@Bot] report_<id>`` or ``view_<id>``.

    Returns None when the text is not a valid deep link of *kind*.
    """
    pattern = _START_REPORT_RE if kind == 'report' else _START_VIEW_RE
    match = pattern.match(text or '')
    return int(match.group(1)) if match else None


def start_command_intent(text: str) -> str:
    """Classify a command typed mid-report: report / view / start / other."""
    text = text or ''
    if _START_REPORT_RE.match(text):
        return 'report'
    if _START_VIEW_RE.match(text):
        return 'view'
    if text.startswith('/start'):
        return 'start'
    return 'other'


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

def report_ownership(report, bot_clone_id) -> str:
    """Return OWNED, FOREIGN, or LEGACY_UNASSIGNED for *report* vs this bot."""
    if report is None:
        return FOREIGN
    if not getattr(report, 'owner_known', False):
        return LEGACY_UNASSIGNED
    if same_clone_id(getattr(report, 'clone_id', None), bot_clone_id):
        return OWNED
    return FOREIGN


def is_visible_to_bot(report, bot_clone_id) -> bool:
    """Approved reports this bot may list in 查看报告.

    Historical rows with ``owner_known=False`` are never listed on any bot, so
    they are not silently attributed to the main bot and cannot leak across
    clones.  They remain in the database.
    """
    if report is None or getattr(report, 'status', None) != 'approved':
        return False
    return report_ownership(report, bot_clone_id) == OWNED


def visible_reports_query(UserReport, user_id, bot_clone_id):
    """SQLAlchemy query for approved, owned reports of *user_id* for this bot."""
    q = UserReport.query.filter_by(
        user_id=user_id,
        status='approved',
        owner_known=True,
    )
    if bot_clone_id is None:
        q = q.filter(UserReport.clone_id.is_(None))
    else:
        q = q.filter_by(clone_id=bot_clone_id)
    return q.order_by(UserReport.created_at.desc())


def attribute_report_to_bot(report, bot_clone_id) -> None:
    """Stamp a previously unassigned report as belonging to *bot_clone_id*."""
    report.owner_known = True
    report.clone_id = bot_clone_id


# ---------------------------------------------------------------------------
# Audit authorization (pure decision; caller supplies admin-check result)
# ---------------------------------------------------------------------------

@dataclass
class AuditAccess:
    allowed: bool
    reason: str = ''
    attribute_on_success: bool = False
    message: str = ''


def callback_message_sent_by_bot(message, bot_id) -> bool:
    """True only when the callback message was sent by *this* bot.

    Equivalent to ``message.from_user.id == context.bot.id``.  Missing
    ``from_user`` (e.g. InaccessibleMessage) fails closed.
    """
    if message is None or bot_id is None:
        return False
    from_user = getattr(message, 'from_user', None)
    from_id = getattr(from_user, 'id', None) if from_user is not None else None
    if from_id is None:
        return False
    try:
        return int(from_id) == int(bot_id)
    except (TypeError, ValueError):
        return False


def evaluate_audit_access(
    *,
    report,
    bot_clone_id,
    callback_chat_id,
    configured_admin_group_id,
    clicker_is_admin: bool,
    callback_sent_by_this_bot: bool,
    callback_message_id=None,
) -> AuditAccess:
    """Decide whether an audit callback may proceed.

    Required: configured admin group, callback message from that group,
    the callback message was sent by this bot, the clicker is an administrator
    (or anonymous admin) of that group, and the report is *already* owned by
    this bot.  If the report stored the original review message id / chat,
    the callback must match those exactly.

    Unassigned historical reports are never attributed from a callback, even
    when several bots share the same admin group or a button is forged with
    another report's id.  They stay in the database.
    """
    if not normalize_chat_id(configured_admin_group_id):
        return AuditAccess(
            False, 'no_admin_group',
            message='未配置管理员审核群组，无法审核。',
        )
    if not chat_ids_equal(callback_chat_id, configured_admin_group_id):
        return AuditAccess(
            False, 'wrong_chat',
            message='无权操作：审核消息不在已配置的审核群中。',
        )
    if not callback_sent_by_this_bot:
        return AuditAccess(
            False, 'not_this_bot',
            message='无权操作：审核消息不是当前机器人发送的。',
        )
    if not clicker_is_admin:
        return AuditAccess(
            False, 'not_admin',
            message='无权操作：您不是该审核群的管理员。',
        )
    if report is None:
        return AuditAccess(False, 'missing', message='报告不存在。')

    ownership = report_ownership(report, bot_clone_id)
    if ownership == FOREIGN:
        return AuditAccess(
            False, 'foreign',
            message='该报告不属于当前机器人，已拒绝操作。',
        )
    if ownership == LEGACY_UNASSIGNED:
        return AuditAccess(
            False, 'legacy_unassigned',
            message=(
                '该报告为历史上未确认归属的记录，无法核对其原始审核消息，'
                '已拒绝认领与操作。记录仍保留在数据库中。'
            ),
        )

    stored_chat = getattr(report, 'admin_chat_id', None)
    stored_msg = getattr(report, 'admin_message_id', None)
    if stored_chat and not chat_ids_equal(callback_chat_id, stored_chat):
        return AuditAccess(
            False, 'review_chat_mismatch',
            message='无权操作：这不是该报告的原始审核消息。',
        )
    if stored_msg is not None:
        try:
            same_msg = int(callback_message_id) == int(stored_msg)
        except (TypeError, ValueError):
            same_msg = False
        if not same_msg:
            return AuditAccess(
                False, 'review_message_mismatch',
                message='无权操作：这不是该报告的原始审核消息。',
            )
    return AuditAccess(True, 'owned')


def clicker_counts_as_admin(user_id, member_status: Optional[str] = None) -> bool:
    """True when getChatMember says admin/creator, or the clicker is Anonymous Admin."""
    if user_id is not None and int(user_id) == ANONYMOUS_ADMIN_USER_ID:
        return True
    return member_status in ('creator', 'administrator')


# ---------------------------------------------------------------------------
# Approve / reject / delete outcomes
# ---------------------------------------------------------------------------

@dataclass
class ActionResult:
    status: str  # ok, rejected, already_done, failed, refused, already_deleted
    message: str
    report_status: Optional[str] = None
    channel_msg_id: Optional[int] = None
    channel_chat_id: Optional[str] = None
    awarded_points: int = 0
    duplicate_msg_id: Optional[int] = None  # published by a lost race; caller should delete
    attributed: bool = False
    published: bool = False
    notify_submitter: bool = False


def parse_answers(report) -> list:
    answers = []
    raw = getattr(report, 'answers', None)
    if raw:
        try:
            answers = json.loads(raw)
        except (ValueError, TypeError):
            answers = []
    if not answers:
        if getattr(report, 'fault_time', None):
            answers.append({"question": "故障时间", "answer": report.fault_time})
        if getattr(report, 'fault_desc', None):
            answers.append({"question": "故障现象", "answer": report.fault_desc})
        if getattr(report, 'process_result', None):
            answers.append({"question": "处理结果", "answer": report.process_result})
    return answers


def telegram_message_already_gone(exc: BaseException) -> bool:
    """True only when a delete error means the message is already absent.

    ``message can't be deleted`` is *not* absence: Telegram uses it when the
    message is too old (48h limit) or the bot lacks permission.  Those must
    not mark the channel post as deleted.
    """
    text = str(exc).lower()
    absence = (
        'message to delete not found',
        'message to be deleted not found',
        'message not found',
        'message_id_invalid',
        'message identifier is invalid',
    )
    return any(n in text for n in absence)


def should_skip_publish(report) -> bool:
    """True when a previous attempt already stored a live channel message."""
    return bool(
        getattr(report, 'channel_msg_id', None)
        and not getattr(report, 'channel_msg_deleted', False)
    )


def points_reason(report_id: int) -> str:
    return f'{POINTS_REASON_PREFIX}{report_id}'


def find_points_group(BotGroup, group_chat_id: str, bot_clone_id):
    """Resolve the points group for this bot only (never another clone)."""
    if not (group_chat_id or '').strip():
        return None
    wanted = normalize_chat_id(group_chat_id)
    q = BotGroup.query
    if bot_clone_id is None:
        q = q.filter(BotGroup.clone_id.is_(None))
    else:
        q = q.filter_by(clone_id=bot_clone_id)
    for group in q.all():
        if chat_ids_equal(group.chat_id, wanted) or chat_ids_equal(group.chat_id, group_chat_id):
            return group
    return None


def groups_for_clone(BotGroup, bot_clone_id):
    q = BotGroup.query
    if bot_clone_id is None:
        return q.filter(BotGroup.clone_id.is_(None))
    return q.filter_by(clone_id=bot_clone_id)


def group_ids_for_clone(BotGroup, bot_clone_id) -> list:
    return [g.id for g in groups_for_clone(BotGroup, bot_clone_id).all()]


def group_user_to_namespace(gu):
    """Convert a GroupUser ORM row to a SimpleNamespace like GroupMember."""
    try:
        pd = json.loads(gu.profile_data or '{}')
    except (ValueError, TypeError):
        pd = {}
    username = pd.get('tg_username') or pd.get('username') or ''
    if not username:
        for v in pd.values():
            if isinstance(v, str) and v.startswith('@') and len(v) > 1:
                username = v[1:]
                break
    return SimpleNamespace(
        user_id=gu.tg_id,
        username=username,
        first_name=pd.get('first_name') or pd.get('name') or '',
        last_name=pd.get('last_name') or '',
    )


def find_member_for_bot(
    *,
    GroupMember,
    GroupUser,
    BotGroup,
    bot_clone_id,
    user_id=None,
    username=None,
):
    """Look up a member only inside groups owned by the current bot."""
    from sqlalchemy import func

    group_ids = group_ids_for_clone(BotGroup, bot_clone_id)
    if not group_ids:
        return None

    if user_id is not None:
        member = (
            GroupMember.query
            .filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id)
            .first()
        )
        if member is not None:
            return member
        gu = (
            GroupUser.query
            .filter(GroupUser.group_id.in_(group_ids), GroupUser.tg_id == user_id)
            .first()
        )
        return group_user_to_namespace(gu) if gu is not None else None

    if not username:
        return None
    uname = username.lstrip('@')
    member = (
        GroupMember.query
        .filter(
            GroupMember.group_id.in_(group_ids),
            func.lower(GroupMember.username) == uname.lower(),
        )
        .first()
    )
    if member is not None:
        return member

    escaped = uname.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    candidates = (
        GroupUser.query
        .filter(
            GroupUser.group_id.in_(group_ids),
            GroupUser.profile_data.ilike(f'%{escaped}%', escape='\\'),
        )
        .all()
    )
    for c in candidates:
        try:
            pd = json.loads(c.profile_data or '{}')
        except (ValueError, TypeError):
            pd = {}
        for v in pd.values():
            normalized = v[1:] if isinstance(v, str) and v.startswith('@') else v
            if isinstance(v, str) and normalized.lower() == uname.lower():
                return group_user_to_namespace(c)
    return None


def new_report_values(*, user_id, submitter_id, raw_answers, answers, photo_file_id, bot_clone_id):
    """Field dict for a newly submitted report (always attributed to this bot)."""
    first = raw_answers[0] if raw_answers else ''
    second = raw_answers[1] if len(raw_answers) > 1 else ''
    third = raw_answers[2] if len(raw_answers) > 2 else ''
    return dict(
        user_id=user_id,
        submitter_id=submitter_id,
        fault_time=first or '',
        fault_desc=second or '',
        process_result=third or '',
        photo_file_id=photo_file_id,
        answers=json.dumps(answers, ensure_ascii=False),
        status='pending',
        clone_id=bot_clone_id,
        owner_known=True,
        points_awarded=False,
        admin_chat_id=None,
        admin_message_id=None,
        approve_claimed_at=None,
    )



def claim_for_approve(db, UserReport, report_id, *, now=None, lease_seconds=APPROVE_CLAIM_SECONDS) -> ActionResult:
    """Atomically lease a pending report for approval (portable CAS, not a stuck state).

    Only one worker can hold a live lease.  After *lease_seconds* (or after
    ``release_approve_claim``) another worker may retry.  Already-approved
    rows are not claimed.
    """
    now = now or datetime.utcnow()
    expired_before = now - timedelta(seconds=lease_seconds)
    updated = (
        UserReport.query.filter(
            UserReport.id == report_id,
            UserReport.status == 'pending',
            or_(
                UserReport.approve_claimed_at.is_(None),
                UserReport.approve_claimed_at <= expired_before,
            ),
        ).update({'approve_claimed_at': now}, synchronize_session=False)
    )
    db.session.commit()
    r = UserReport.query.get(report_id)
    if r is None:
        return ActionResult('refused', '报告不存在。')
    if updated == 1:
        return ActionResult(
            status='claimed',
            message='',
            report_status='pending',
            channel_msg_id=r.channel_msg_id,
            channel_chat_id=r.channel_chat_id,
        )
    if r.status == 'approved':
        return ActionResult(
            status='already_done',
            message=f'ℹ️ 报告 #{report_id} 早已审核通过。',
            report_status='approved',
            channel_msg_id=r.channel_msg_id,
            channel_chat_id=r.channel_chat_id,
            notify_submitter=False,
        )
    if r.status != 'pending':
        return ActionResult(
            status='already_done',
            message=f'ℹ️ 该报告已被处理（当前状态：{r.status}）。',
            report_status=r.status,
            notify_submitter=False,
        )
    return ActionResult(
        status='in_progress',
        message='报告正在处理中，请稍候，不要重复点击。',
        report_status='pending',
        channel_msg_id=r.channel_msg_id,
        channel_chat_id=r.channel_chat_id,
    )


def release_approve_claim(db, UserReport, report_id) -> None:
    """Drop a live lease after a failed publish that did not produce a channel post."""
    UserReport.query.filter_by(id=report_id, status='pending').update(
        {'approve_claimed_at': None}, synchronize_session=False
    )
    db.session.commit()


def record_published_channel(db, UserReport, report_id, channel_msg_id, channel_chat_id) -> ActionResult:
    """Persist the channel identity *before* flipping status, first writer wins.

    Does not change ``status`` or award points.  If another worker already
    stored a different message id, returns ``lost_race`` with that id.
    """
    r = UserReport.query.get(report_id)
    if r is None:
        return ActionResult('refused', '报告不存在。')
    if r.channel_msg_id and int(r.channel_msg_id) == int(channel_msg_id):
        return ActionResult(
            status='ok',
            message='',
            channel_msg_id=r.channel_msg_id,
            channel_chat_id=r.channel_chat_id,
            report_status=r.status,
        )
    if r.channel_msg_id:
        return ActionResult(
            status='lost_race',
            message=f'报告 #{report_id} 已登记其他频道消息，本次发布未写入。',
            channel_msg_id=r.channel_msg_id,
            channel_chat_id=r.channel_chat_id,
            duplicate_msg_id=channel_msg_id,
            report_status=r.status,
        )
    updated = UserReport.query.filter(
        UserReport.id == report_id,
        UserReport.channel_msg_id.is_(None),
    ).update(
        {
            'channel_msg_id': channel_msg_id,
            'channel_chat_id': (str(channel_chat_id).strip() if channel_chat_id else None),
            'channel_msg_deleted': False,
        },
        synchronize_session=False,
    )
    db.session.commit()
    r = UserReport.query.get(report_id)
    if updated != 1:
        if r and r.channel_msg_id and int(r.channel_msg_id) != int(channel_msg_id):
            return ActionResult(
                status='lost_race',
                message=f'报告 #{report_id} 已登记其他频道消息，本次发布未写入。',
                channel_msg_id=r.channel_msg_id,
                channel_chat_id=r.channel_chat_id,
                duplicate_msg_id=channel_msg_id,
                report_status=r.status,
            )
    return ActionResult(
        status='ok',
        message='',
        channel_msg_id=r.channel_msg_id if r else channel_msg_id,
        channel_chat_id=(r.channel_chat_id if r else channel_chat_id),
        report_status=r.status if r else 'pending',
    )


def store_admin_review_message(db, UserReport, report_id, admin_chat_id, admin_message_id) -> None:
    """Record the original audit message sent by this bot."""
    r = UserReport.query.get(report_id)
    if r is None:
        return
    r.admin_chat_id = str(admin_chat_id).strip() if admin_chat_id is not None else None
    r.admin_message_id = int(admin_message_id) if admin_message_id is not None else None
    db.session.commit()


def award_approval_points(
    *,
    db,
    UserPoints,
    PointsLog,
    BotGroup,
    report,
    bot_clone_id,
    reward_pts: int,
    points_group_chat_id: str,
) -> int:
    """Credit the submitter once.  Returns points actually awarded (0 if skipped)."""
    if reward_pts <= 0 or not getattr(report, 'submitter_id', None):
        return 0
    if getattr(report, 'points_awarded', False):
        return 0
    reason = points_reason(report.id)
    existing = PointsLog.query.filter_by(
        user_id=report.submitter_id,
        reason=reason,
    ).first()
    if existing is not None:
        report.points_awarded = True
        return 0

    target_group = find_points_group(BotGroup, points_group_chat_id, bot_clone_id)
    if target_group is None:
        return 0

    # Extra guard: a log with the same reason in this group.
    existing = PointsLog.query.filter_by(
        group_id=target_group.id,
        user_id=report.submitter_id,
        reason=reason,
    ).first()
    if existing is not None:
        report.points_awarded = True
        return 0

    pts = UserPoints.query.filter_by(
        group_id=target_group.id,
        user_id=report.submitter_id,
    ).first()
    if pts is None:
        pts = UserPoints(
            group_id=target_group.id,
            user_id=report.submitter_id,
            points_balance=0,
        )
        db.session.add(pts)
    # Flag is set only after both the balance change and the log row exist on
    # the same session; the caller commits them together with status='approved'.
    # If this function raises, the caller must not commit, so the flag stays off.
    pts.points_balance += reward_pts
    db.session.add(PointsLog(
        group_id=target_group.id,
        user_id=report.submitter_id,
        points_change=reward_pts,
        reason=reason,
        balance_after=pts.points_balance,
    ))
    db.session.flush()
    report.points_awarded = True
    return reward_pts


def _reject_conflict_result(report) -> ActionResult:
    if getattr(report, 'channel_msg_id', None):
        return ActionResult(
            status='conflict',
            message=(
                f'❌ 报告 #{report.id} 已有频道消息，不能驳回。'
                '请先完成通过审核，或从频道删除后再处理。'
            ),
            report_status=report.status,
            channel_msg_id=report.channel_msg_id,
            channel_chat_id=report.channel_chat_id,
            notify_submitter=False,
        )
    return ActionResult(
        status='in_progress',
        message=f'报告 #{report.id} 正在通过审核中，不能驳回。请稍候。',
        report_status=report.status or 'pending',
        notify_submitter=False,
    )


def finalize_reject(db, report, bot_clone_id=None, attribute: bool = False, *, now=None) -> ActionResult:
    """Mark pending report rejected with an atomic CAS.

    ``attribute`` is accepted for compatibility but ignored: unassigned
    historical reports must not be claimed during audit.

    Reject is refused when another worker holds a live approve lease, or when
    a channel message has already been recorded — otherwise a concurrent
    approve could publish while the row is marked rejected.
    """
    now = now or datetime.utcnow()
    expired_before = now - timedelta(seconds=APPROVE_CLAIM_SECONDS)
    Model = type(report)

    if report.status == 'rejected':
        return ActionResult(
            status='already_done',
            message=f'ℹ️ 报告 #{report.id} 早已被驳回。',
            report_status='rejected',
            attributed=bool(report.owner_known),
            notify_submitter=False,
        )
    if report.status != 'pending':
        return ActionResult(
            status='already_done',
            message=f'ℹ️ 该报告已被处理（当前状态：{report.status}）。',
            report_status=report.status,
            notify_submitter=False,
        )

    updated = (
        db.session.query(Model)
        .filter(
            Model.id == report.id,
            Model.status == 'pending',
            Model.channel_msg_id.is_(None),
            or_(
                Model.approve_claimed_at.is_(None),
                Model.approve_claimed_at <= expired_before,
            ),
        )
        .update(
            {'status': 'rejected', 'approve_claimed_at': None},
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.session.expire(report)
        fresh = Model.query.get(report.id)
        if fresh is None:
            return ActionResult('refused', '报告不存在。')
        if fresh.status == 'rejected':
            return ActionResult(
                status='already_done',
                message=f'ℹ️ 报告 #{fresh.id} 早已被驳回。',
                report_status='rejected',
                attributed=bool(fresh.owner_known),
                notify_submitter=False,
            )
        if fresh.status != 'pending':
            return ActionResult(
                status='already_done',
                message=f'ℹ️ 该报告已被处理（当前状态：{fresh.status}）。',
                report_status=fresh.status,
                notify_submitter=False,
            )
        return _reject_conflict_result(fresh)

    db.session.commit()
    db.session.expire(report)
    report = Model.query.get(report.id)
    return ActionResult(
        status='ok',
        message=f'❌ 报告 #{report.id} 已驳回。',
        report_status='rejected',
        attributed=bool(report.owner_known),
        notify_submitter=True,
    )


def finalize_approve(
    db,
    report,
    bot_clone_id,
    *,
    channel_msg_id,
    channel_chat_id,
    attribute: bool = False,
    UserPoints=None,
    PointsLog=None,
    BotGroup=None,
    reward_pts: int = 0,
    points_group_chat_id: str = '',
) -> ActionResult:
    """Commit approval, stored channel identity, and one-time points in one transaction.

    ``attribute`` is ignored: audit never claims unassigned historical rows.
    Channel identity should already have been recorded by
    ``record_published_channel`` when a post was sent; this function will also
    write it if still empty.  ``approve_claimed_at`` is cleared on success.
    Status and points_awarded are committed together; a raised error rolls
    both back so the flag cannot be set without an actual credit.
    """
    if report.status == 'approved':
        stored = report.channel_msg_id
        # A lost-race extra post (lease expired mid-publish) is exceptional,
        # not the normal approval path.
        extra = channel_msg_id if (channel_msg_id and stored and int(channel_msg_id) != int(stored)) else None
        db.session.commit()
        return ActionResult(
            status='already_done',
            message=f'ℹ️ 报告 #{report.id} 早已审核通过。',
            report_status='approved',
            channel_msg_id=stored,
            channel_chat_id=report.channel_chat_id,
            awarded_points=0,
            duplicate_msg_id=extra,
            notify_submitter=False,
        )
    if report.status != 'pending':
        extra = channel_msg_id if (channel_msg_id and report.channel_msg_id and int(channel_msg_id) != int(report.channel_msg_id or 0)) else None
        db.session.commit()
        return ActionResult(
            status='already_done',
            message=f'ℹ️ 该报告已被处理（当前状态：{report.status}）。',
            report_status=report.status,
            duplicate_msg_id=extra,
            notify_submitter=False,
        )

    # CAS: only one worker can flip pending → approved in this transaction.
    # Status, channel identity and points stay uncommitted until the end, so a
    # later error rolls all three back together.
    Model = type(report)
    claimed_rows = (
        db.session.query(Model)
        .filter(Model.id == report.id, Model.status == 'pending')
        .update(
            {'status': 'approved', 'approve_claimed_at': None},
            synchronize_session=False,
        )
    )
    if claimed_rows != 1:
        db.session.expire(report)
        report = Model.query.get(report.id)
        if report is None:
            return ActionResult('refused', '报告不存在。')
        stored = report.channel_msg_id
        extra = channel_msg_id if (channel_msg_id and stored and int(channel_msg_id) != int(stored)) else None
        return ActionResult(
            status='already_done',
            message=f'ℹ️ 该报告已被处理（当前状态：{report.status}）。' if report.status != 'approved'
            else f'ℹ️ 报告 #{report.id} 早已审核通过。',
            report_status=report.status,
            channel_msg_id=stored,
            channel_chat_id=report.channel_chat_id,
            awarded_points=0,
            duplicate_msg_id=extra,
            notify_submitter=False,
        )

    db.session.refresh(report)
    if channel_msg_id and not report.channel_msg_id:
        report.channel_msg_id = channel_msg_id
        report.channel_msg_deleted = False
    if channel_chat_id and not report.channel_chat_id:
        report.channel_chat_id = str(channel_chat_id).strip()

    awarded = 0
    if UserPoints is not None and PointsLog is not None and BotGroup is not None:
        awarded = award_approval_points(
            db=db,
            UserPoints=UserPoints,
            PointsLog=PointsLog,
            BotGroup=BotGroup,
            report=report,
            bot_clone_id=bot_clone_id,
            reward_pts=reward_pts,
            points_group_chat_id=points_group_chat_id,
        )
    db.session.commit()

    published = bool(report.channel_msg_id and not report.channel_msg_deleted)
    if published:
        msg = f'✅ 报告 #{report.id} 已审核通过并发布到频道。'
    elif channel_chat_id or channel_msg_id:
        # Caller thought it published but we have no stored id — shouldn't happen.
        msg = f'✅ 报告 #{report.id} 已审核通过。'
    else:
        msg = f'✅ 报告 #{report.id} 已审核通过（未配置报告频道，未发布）。'
    if awarded:
        msg += f'\n🎁 奖励积分：+{awarded}'
    return ActionResult(
        status='ok',
        message=msg,
        report_status='approved',
        channel_msg_id=report.channel_msg_id,
        channel_chat_id=report.channel_chat_id,
        awarded_points=awarded,
        attributed=bool(report.owner_known),
        published=published,
        notify_submitter=True,
    )


def finalize_delete_channel(db, report, *, success: bool, already_gone: bool) -> ActionResult:
    """Mark the channel message deleted only when the API succeeded or it is gone."""
    if report.channel_msg_deleted:
        return ActionResult(
            status='already_deleted',
            message=f'ℹ️ 报告 #{report.id} 的频道消息已被删除。',
            report_status=report.status,
        )
    if not success and not already_gone:
        return ActionResult(
            status='failed',
            message=f'❌ 删除报告 #{report.id} 的频道消息失败，记录未更改，可重试。',
            report_status=report.status,
        )
    report.channel_msg_deleted = True
    db.session.commit()
    return ActionResult(
        status='ok',
        message=f'🗑️ 报告 #{report.id} 的频道消息已删除。',
        report_status=report.status,
    )


# ---------------------------------------------------------------------------
# Idempotent schema repair (startup + migrate_database.py)
# ---------------------------------------------------------------------------

# (table, column, DDL fragment after ADD COLUMN)
_USER_REPORT_COLUMNS = (
    ('answers', 'TEXT NULL'),
    ('channel_msg_deleted', "BOOLEAN DEFAULT FALSE"),
    ('channel_chat_id', 'VARCHAR(64) NULL'),
    ('clone_id', 'INTEGER NULL'),
    ('owner_known', "BOOLEAN NOT NULL DEFAULT FALSE"),
    ('points_awarded', "BOOLEAN NOT NULL DEFAULT FALSE"),
    ('admin_chat_id', 'VARCHAR(64) NULL'),
    ('admin_message_id', 'INTEGER NULL'),
    ('approve_claimed_at', 'TIMESTAMP NULL'),
)


def user_report_schema_statements(dialect_name: str) -> list:
    """Return idempotent DDL statements for the current dialect.

    Does not backfill ``owner_known`` or ``clone_id``: historical rows stay
    unassigned so they are not attributed to the main bot or leaked to a clone.
    """
    stmts = []
    for col, typ in _USER_REPORT_COLUMNS:
        if dialect_name == 'sqlite':
            # SQLite accepts IF NOT EXISTS on ADD COLUMN from 3.35+; callers
            # also swallow "duplicate column" errors for older versions.
            stmts.append(
                f'ALTER TABLE user_reports ADD COLUMN {col} {typ}'
            )
        else:
            stmts.append(
                f'ALTER TABLE user_reports ADD COLUMN IF NOT EXISTS {col} {typ}'
            )
    if dialect_name == 'postgresql':
        stmts.append('ALTER TABLE user_reports ALTER COLUMN fault_time TYPE TEXT')
        stmts.append(
            'CREATE INDEX IF NOT EXISTS ix_user_reports_clone_id ON user_reports (clone_id)'
        )
    elif dialect_name == 'sqlite':
        stmts.append(
            'CREATE INDEX IF NOT EXISTS ix_user_reports_clone_id ON user_reports (clone_id)'
        )
    return stmts


def ensure_user_report_schema(db) -> list:
    """Apply missing user_reports columns.  Returns statements that succeeded.

    Safe to run on every startup.  Existing values are never overwritten or
    deleted.  ``owner_known`` defaults to false for newly added columns, so
    pre-existing rows remain unassigned.
    """
    from sqlalchemy import inspect as sa_inspect, text as sa_text

    dialect = getattr(getattr(db.engine, 'dialect', None), 'name', '') or ''
    inspector = sa_inspect(db.engine)
    if not inspector.has_table('user_reports'):
        return []

    applied = []
    for stmt in user_report_schema_statements(dialect):
        try:
            with db.engine.connect() as conn:
                conn.execute(sa_text(stmt))
                conn.commit()
            applied.append(stmt)
        except Exception as exc:
            logger.debug('ensure_user_report_schema skip %s: %s', stmt, exc)
    return applied
