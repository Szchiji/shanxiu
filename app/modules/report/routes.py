import json

from flask import render_template, request, flash, redirect, url_for, session
from app import db
from app.models import BotGroup, SystemConfig
from . import report_admin_bp

_SIMPLE_CONFIG_KEYS = ['admin_group_id', 'report_channel', 'report_push_template']

_DEFAULTS = {
    'admin_group_id': '',
    'report_channel': '',
    'report_push_template': '📋 <b>认证用户报告 #{report_id}</b>\n\n{answers}',
    'report_push_media': 'true',
}

_DEFAULT_QUESTIONS = [
    {"text": "请问故障发生的时间是？", "required": True},
    {"text": "请描述具体的故障现象？", "required": True},
    {"text": "最终的处理结果是什么？", "required": True},
]


@report_admin_bp.context_processor
def inject_context():
    """Provide the same template context as core_bp so base.html renders correctly."""
    data = {'all_groups': []}
    if session.get('logged_in'):
        data['all_groups'] = BotGroup.query.order_by(
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

    if request.method == 'POST':
        # Save simple text config keys
        for key in _SIMPLE_CONFIG_KEYS:
            val = request.form.get(key, '').strip()
            SystemConfig.set_value(key, val)

        # Save push media toggle (checkbox: present = true, absent = false)
        push_media = 'true' if request.form.get('report_push_media') else 'false'
        SystemConfig.set_value('report_push_media', push_media)

        # Save dynamic questions submitted as serialised JSON from the form
        questions_raw = request.form.get('questions_json', '[]')
        try:
            questions = json.loads(questions_raw)
            # Normalise: keep only text + required, discard empty items
            questions = [
                {"text": q.get("text", "").strip(), "required": bool(q.get("required", True))}
                for q in questions
                if q.get("text", "").strip()
            ]
        except (ValueError, TypeError):
            questions = _DEFAULT_QUESTIONS
        SystemConfig.set_value('report_questions', json.dumps(questions, ensure_ascii=False))

        db.session.commit()
        flash('✅ 系统配置已保存！')
        return redirect(url_for('report_admin.report_settings'))

    # GET – load current settings
    current = {k: SystemConfig.get_value(k, _DEFAULTS.get(k, '')) for k in _SIMPLE_CONFIG_KEYS}
    current['report_push_media'] = SystemConfig.get_value('report_push_media', _DEFAULTS['report_push_media'])

    questions_json = SystemConfig.get_value('report_questions', '')
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
