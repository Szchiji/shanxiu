from flask import render_template, request, flash, redirect, url_for
from app import db
from app.models import SystemConfig
from . import report_admin_bp

_CONFIG_KEYS = ['admin_group_id', 'report_channel', 'question_1', 'question_2', 'question_3']

_DEFAULTS = {
    'admin_group_id': '',
    'report_channel': '',
    'question_1': '请问故障发生的时间是？',
    'question_2': '请描述具体的故障现象？',
    'question_3': '最终的处理结果是什么？',
}


@report_admin_bp.route('/report_settings', methods=['GET', 'POST'])
def report_settings():
    if request.method == 'POST':
        for key in _CONFIG_KEYS:
            val = request.form.get(key, '').strip()
            SystemConfig.set_value(key, val)
        db.session.commit()
        flash('✅ 系统配置已保存！')
        return redirect(url_for('report_admin.report_settings'))

    current = {k: SystemConfig.get_value(k, _DEFAULTS.get(k, '')) for k in _CONFIG_KEYS}
    return render_template('admin_settings.html', settings=current)
