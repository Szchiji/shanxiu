import json
import secrets
import hmac

from flask import render_template, request, flash, redirect, url_for, session
from app import db
from app.models import BotGroup, SystemConfig
from . import report_admin_bp
from .web_service import format_shanghai, shanghai_day_range_to_utc
from .web_security import SecurityError

_SIMPLE_CONFIG_KEYS = ['admin_group_id', 'report_channel', 'report_push_template', 'report_photo_prompt',
                       'report_approval_points', 'report_points_group_id']

_DEFAULTS = {
    'admin_group_id': '',
    'report_channel': '',
    'report_push_template': '📋 <b>认证用户报告 #{report_id}</b>\n\n{answers}',
    'report_require_photo': 'true',
    'report_push_media': 'true',
    'report_photo_prompt': '请发送预约聊天截图或付款截图 📷（必填）',
    'report_approval_points': '0',
    'report_points_group_id': '',
}

_DEFAULT_QUESTIONS = [
    {"text": "请问故障发生的时间是？", "required": True,
     "hint": "例如：2024-01-15 14:30"},
    {"text": "请描述具体的故障现象？", "required": True,
     "hint": "例如：设备无法启动，屏幕显示错误代码 E01"},
    {"text": "最终的处理结果是什么？", "required": True,
     "hint": "例如：已更换电源模块，设备恢复正常"},
]


def _scoped_key(key: str, clone_id) -> str:
    """Return a clone-scoped config key, or the bare key for the main bot."""
    if clone_id:
        return f'clone_{clone_id}_{key}'
    return key


@report_admin_bp.context_processor
def inject_context():
    """Provide the same template context as core_bp so base.html renders correctly."""
    data = {'all_groups': []}
    if session.get('logged_in'):
        clone_id = session.get('clone_id')
        data['all_groups'] = BotGroup.query.filter_by(clone_id=clone_id).order_by(
            BotGroup.is_active.desc(), BotGroup.updated_at.desc()
        ).all()
    gid = session.get('current_group_id')
    if gid:
        data['current_group'] = BotGroup.query.get(gid)
    return data


@report_admin_bp.route('/report_settings', methods=['GET', 'POST'])
def report_settings():
    if not session.get('logged_in'):
        return redirect('/core/')

    clone_id = session.get('clone_id')

    if request.method == 'POST':
        # Save simple text config keys
        for key in _SIMPLE_CONFIG_KEYS:
            val = request.form.get(key, '').strip()
            SystemConfig.set_value(_scoped_key(key, clone_id), val)

        # Save push media toggle (checkbox: present = true, absent = false)
        push_media = 'true' if request.form.get('report_push_media') else 'false'
        SystemConfig.set_value(_scoped_key('report_push_media', clone_id), push_media)

        # Save require photo toggle (checkbox: present = true, absent = false)
        require_photo = 'true' if request.form.get('report_require_photo') else 'false'
        SystemConfig.set_value(_scoped_key('report_require_photo', clone_id), require_photo)

        # Save dynamic questions submitted as serialised JSON from the form
        questions_raw = request.form.get('questions_json', '[]')
        try:
            questions = json.loads(questions_raw)
            # Normalise: keep only text + required, discard empty items
            questions = [
                {
                    "text": q.get("text", "").strip(),
                    "required": bool(q.get("required", True)),
                    "hint": q.get("hint", "").strip(),
                }
                for q in questions
                if q.get("text", "").strip()
            ]
        except (ValueError, TypeError):
            questions = _DEFAULT_QUESTIONS
        SystemConfig.set_value(_scoped_key('report_questions', clone_id),
                               json.dumps(questions, ensure_ascii=False))

        db.session.commit()
        flash('✅ 系统配置已保存！')
        return redirect(url_for('report_admin.report_settings'))

    # GET – load current settings
    current = {k: SystemConfig.get_value(_scoped_key(k, clone_id), _DEFAULTS.get(k, ''))
               for k in _SIMPLE_CONFIG_KEYS}
    current['report_push_media'] = SystemConfig.get_value(
        _scoped_key('report_push_media', clone_id), _DEFAULTS['report_push_media'])
    current['report_require_photo'] = SystemConfig.get_value(
        _scoped_key('report_require_photo', clone_id), _DEFAULTS['report_require_photo'])

    questions_json = SystemConfig.get_value(_scoped_key('report_questions', clone_id), '')
    try:
        questions = json.loads(questions_json) if questions_json else _DEFAULT_QUESTIONS
    except (ValueError, TypeError):
        questions = _DEFAULT_QUESTIONS

    return render_template(
        'admin_settings.html',
        settings=current,
        questions=questions,
        page='report_settings',
    )


from flask import abort, send_file
import io


def _require_admin():
    if not session.get('logged_in'):
        return redirect('/core/')
    return None


def _parse_admin_filters(clone_id):
    from app.modules.report.web_service import admin_reports_query
    from app.models import UserReport, GroupMember, GroupUser, BotGroup
    from app.modules.report import service as report_service

    raw_target = (request.args.get('target') or '').strip()
    status = (request.args.get('status') or '').strip()
    date_from_s = (request.args.get('date_from') or '').strip()
    date_to_s = (request.args.get('date_to') or '').strip()
    try:
        page = max(1, int(request.args.get('page') or 1))
    except (TypeError, ValueError):
        page = 1

    target_user_id = None
    target_notice = ''
    if raw_target:
        username = None
        if raw_target.startswith('@'):
            username = raw_target[1:].strip()
        elif raw_target.lstrip('-').isdigit():
            try:
                target_user_id = int(raw_target)
            except ValueError:
                target_user_id = None
        else:
            username = raw_target
        if username and target_user_id is None:
            member = report_service.find_member_for_bot(
                GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
                bot_clone_id=clone_id, username=username,
            )
            if member is None:
                target_notice = f'未在当前机器人范围内找到用户 {raw_target}。'
                target_user_id = -1  # force empty
            else:
                target_user_id = member.user_id
        elif raw_target and target_user_id is None:
            target_notice = '请输入数字 ID 或用户名。'
            target_user_id = -1

    if status not in ('', 'pending', 'approved', 'rejected'):
        status = ''

    date_from = date_to = None
    try:
        date_from, date_to = shanghai_day_range_to_utc(date_from_s, date_to_s)
    except SecurityError:
        date_from = date_to = None
        target_notice = (target_notice + ' 日期格式无效。').strip()

    query = admin_reports_query(
        UserReport, clone_id=clone_id, target_user_id=target_user_id if target_user_id and target_user_id > 0 else (target_user_id if target_user_id == -1 else None),
        status=status or None, date_from=date_from, date_to=date_to,
    )
    if target_user_id == -1:
        query = query.filter(UserReport.id == -1)
    return {
        'query': query,
        'target': raw_target,
        'status': status,
        'date_from': date_from_s,
        'date_to': date_to_s,
        'page': page,
        'notice': target_notice,
    }


@report_admin_bp.route('/reports')
def report_list():
    gate = _require_admin()
    if gate:
        return gate
    clone_id = session.get('clone_id')
    from app.models import UserReport, GroupMember, GroupUser, BotGroup
    from app.modules.report import service as report_service

    filters = _parse_admin_filters(clone_id)
    page = filters['page']
    per_page = 20
    pagination = filters['query'].paginate(page=page, per_page=per_page, error_out=False)
    rows = []
    for r in pagination.items:
        member = report_service.find_member_for_bot(
            GroupMember=GroupMember, GroupUser=GroupUser, BotGroup=BotGroup,
            bot_clone_id=clone_id, user_id=r.user_id,
        )
        rows.append({
            'report': r,
            'target': report_service.format_user_identity(r.user_id, member=member),
            'created': format_shanghai(r.created_at),
        })
    return render_template(
        'admin_list.html',
        page='report_list',
        rows=rows,
        pagination=pagination,
        filters=filters,
    )


@report_admin_bp.route('/reports/<int:report_id>')
def report_detail(report_id):
    gate = _require_admin()
    if gate:
        return gate
    clone_id = session.get('clone_id')
    from app.models import UserReport, GroupMember, GroupUser, BotGroup, SystemConfig
    from app.modules.report import service as report_service
    from app.modules.report.web_service import admin_may_view_report

    r = UserReport.query.get(report_id)
    if r is None or not admin_may_view_report(r, clone_id):
        abort(404)
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
    fallback_channel = SystemConfig.get_value(
        report_service.scoped_key('report_channel', clone_id), ''
    )
    channel = report_service.channel_identity_for_view(r, fallback_channel)
    link = ''
    if r.channel_msg_id and channel and not r.channel_msg_deleted:
        link = report_service.build_channel_link(channel, r.channel_msg_id)
    session.setdefault('report_notify_csrf', secrets.token_urlsafe(32))
    return render_template(
        'admin_detail.html',
        page='report_list',
        report=r,
        answers=report_service.parse_answers(r),
        target=report_service.format_user_identity(r.user_id, member=target_member),
        submitter=report_service.format_user_identity(r.submitter_id, member=submitter_member) if r.submitter_id else '',
        created=format_shanghai(r.created_at),
        channel_link=link,
        has_photo=bool(r.photo_file_id),
        notify_csrf=session['report_notify_csrf'],
    )


@report_admin_bp.route('/reports/<int:report_id>/photo')
def report_photo(report_id):
    gate = _require_admin()
    if gate:
        return gate
    clone_id = session.get('clone_id')
    from app.models import UserReport
    from app.modules.report.web_service import admin_may_view_report
    r = UserReport.query.get(report_id)
    if r is None or not r.photo_file_id or not admin_may_view_report(r, clone_id):
        abort(404)
    from app.modules.report.web_routes import _ptb_app_for_clone
    import asyncio
    ptb_app, loop = _ptb_app_for_clone(clone_id)
    if ptb_app is None or loop is None:
        abort(503)

    async def _download():
        file = await ptb_app.bot.get_file(r.photo_file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        return buf.getvalue()

    try:
        data = asyncio.run_coroutine_threadsafe(_download(), loop).result(timeout=30)
    except Exception:
        abort(502)
    return send_file(io.BytesIO(data), mimetype='image/jpeg', download_name=f'report-{report_id}.jpg')


@report_admin_bp.route('/reports/<int:report_id>/retry-notify', methods=['POST'])
def report_retry_notify(report_id):
    gate = _require_admin()
    if gate:
        return gate
    from app.models import UserReport
    from .web_service import admin_may_view_report
    from .web_routes import retry_report_notification
    csrf = request.form.get('csrf', '')
    if not csrf or not hmac.compare_digest(csrf, session.get('report_notify_csrf', '')):
        abort(403)
    report = db.session.get(UserReport, report_id)
    if report is None or not admin_may_view_report(report, session.get('clone_id')):
        abort(404)
    if report.status != 'pending' or report.notify_status not in ('pending', 'failed'):
        flash('不可重试：已投递或投递状态未知；请先核对审核群，避免重复消息。', 'warning')
    else:
        ok, reason = retry_report_notification(report)
        flash('审核群通知已投递。' if ok else f'通知未确认投递（{reason}），附件保留。',
              'success' if ok else 'warning')
    return redirect(url_for('report_admin.report_detail', report_id=report_id))
