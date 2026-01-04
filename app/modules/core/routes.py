from flask import Blueprint, render_template, request, redirect, session, jsonify
from app import db
from app.models import BotGroup, GroupUser, DEFAULT_FIELDS, DEFAULT_SYSTEM, AuthSession, AutoReply, ScheduledMessage
from app.services import sanitize_html_for_telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, ChatMember
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatMemberHandler, filters
from sqlalchemy.orm import joinedload
import os, jwt, time, json, asyncio, re, requests, math, secrets, string, hmac
from datetime import datetime, timedelta
import pytz

core_bp = Blueprint('core', __name__, url_prefix='/core', template_folder='templates')

# --- 全局变量 ---
global_ptb_app = None
global_bot_loop = None
global_flask_app = None  # 🆕 新增：持有 Flask App 实例

# Constants
EXPIRATION_CHECK_INTERVAL = 3600  # Check expired users every hour (in seconds)
JWT_TOKEN_EXPIRY_DAYS = 7  # JWT token validity for group access (in days)
MAX_CONCURRENT_BANS = 5  # Maximum concurrent ban operations to avoid rate limits
EXPIRED_USERS_BATCH_SIZE = 100  # Process expired users in batches to avoid memory issues
AUTH_SESSION_EXPIRY_MINUTES = 5  # Authentication session expiry time
SCHEDULED_MESSAGE_CHECK_INTERVAL = 60  # Check scheduled messages every minute (in seconds)

# Beijing timezone
BEIJING_TZ = pytz.timezone('Asia/Shanghai')

def generate_verification_code():
    """Generate a random 6-digit verification code"""
    return ''.join(secrets.choice(string.digits) for _ in range(6))

def generate_session_token():
    """Generate a secure random session token"""
    return secrets.token_urlsafe(32)

def get_beijing_now():
    """Get current time in Beijing timezone as naive datetime (for database storage)"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)

def get_beijing_today():
    """Get today's date at midnight in Beijing timezone as naive datetime"""
    now = datetime.now(BEIJING_TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

async def is_user_admin_in_group(bot, chat_id, user_id):
    """Check if a user is an administrator in a specific group"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        print(f"Error checking admin status: {e}")
        return False

# --- Webhook ---
@core_bp.route('/webhook', methods=['POST'])
def webhook():
    if not global_ptb_app or not global_bot_loop: return "Bot Not Ready", 503
    try:
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, global_ptb_app.bot)
        
        # 添加 Future 回调以捕获异步任务中的异常
        future = asyncio.run_coroutine_threadsafe(global_ptb_app.process_update(update), global_bot_loop)
        
        def check_future_exception(fut):
            try:
                exc = fut.exception()
                if exc:
                    import traceback
                    print(f"❌ Webhook 异步任务异常:")
                    print(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            except Exception as e:
                print(f"❌ 回调函数本身异常: {e}")
        
        future.add_done_callback(check_future_exception)
        return "OK", 200
    except Exception as e:
        print(f"❌ Webhook Error: {e}")
        return "Error", 200

# --- Context ---
@core_bp.context_processor
def inject_context():
    data = {'all_groups': []}
    if session.get('logged_in'):
        data['all_groups'] = BotGroup.query.order_by(BotGroup.is_active.desc(), BotGroup.updated_at.desc()).all()
    gid = session.get('current_group_id')
    if gid: data['current_group'] = BotGroup.query.get(gid)
    return data

def safe_int(val, default=0):
    if val is None: return default
    if isinstance(val, str) and val.strip() == '': return default
    try: return int(val)
    except: return default

def get_group_conf(group):
    conf = DEFAULT_SYSTEM.copy()
    if group and group.config:
        try:
            c = json.loads(group.config)
            if isinstance(c, dict) and 'config' in c: c = c['config']
            for k, v in c.items():
                if v is not None: conf[k] = v
        except: pass
    return conf

def get_group_fields(group):
    if group and group.fields_config:
        try: return json.loads(group.fields_config)
        except: pass
    return DEFAULT_FIELDS

# --- Web Routes ---
@core_bp.route('/')
def index(): return redirect('/core/select_group') if session.get('logged_in') else render_template('base.html', page='login')

@core_bp.route('/select_group')
def page_select_group():
    if not session.get('logged_in'): return redirect('/core')
    session.pop('current_group_id', None)
    groups = BotGroup.query.order_by(BotGroup.is_active.desc(), BotGroup.updated_at.desc()).all()
    return render_template('select_group.html', groups=groups)

@core_bp.route('/group/<int:gid>/dashboard')
def page_dashboard(gid):
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    stats = {'users': GroupUser.query.filter_by(group_id=gid).count(), 'online': GroupUser.query.filter_by(group_id=gid, online=True).count()}
    return render_template('dashboard.html', page='dashboard', group=group, stats=stats)

@core_bp.route('/group/<int:gid>/users')
def page_users(gid):
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    # Pagination parameters
    page = safe_int(request.args.get('page', 1), 1)
    per_page = safe_int(request.args.get('per_page', 50), 50)
    if per_page not in [20, 50, 100] or per_page <= 0: per_page = 50
    if page < 1: page = 1
    
    # Get total count and paginated users
    total_users = GroupUser.query.filter_by(group_id=gid).count()
    total_pages = math.ceil(total_users / per_page) if total_users > 0 else 1
    if page > total_pages: page = total_pages
    
    users = GroupUser.query.filter_by(group_id=gid).order_by(GroupUser.id.desc()).offset((page-1)*per_page).limit(per_page).all()
    for u in users:
        try: u.profile_dict = json.loads(u.profile_data) if u.profile_data else {}
        except: u.profile_dict = {}
    
    return render_template('users.html', page='users', group=group, users=users, fields=get_group_fields(group), 
                         current_page=page, total_pages=total_pages, per_page=per_page, total_users=total_users)

@core_bp.route('/group/<int:gid>/fields')
def page_fields(gid):
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    return render_template('fields.html', page='fields', group=group, fields_json=json.dumps(get_group_fields(group)))

@core_bp.route('/group/<int:gid>/settings')
def page_settings(gid):
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    return render_template('settings.html', page='settings', group=group, conf=get_group_conf(group), fields=get_group_fields(group))

@core_bp.route('/group/<int:gid>/auto_replies')
def page_auto_replies(gid):
    """自动回复管理页面"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    auto_replies = AutoReply.query.filter_by(group_id=gid).order_by(AutoReply.id.desc()).all()
    
    # 转换为JSON供前端使用
    auto_replies_json = json.dumps([{
        'id': ar.id,
        'trigger_keyword': ar.trigger_keyword,
        'media_type': ar.media_type,
        'media_url': ar.media_url,
        'content': ar.content,
        'links': ar.links,
        'delete_after': ar.delete_after,
        'remark': ar.remark,
        'is_active': ar.is_active
    } for ar in auto_replies], ensure_ascii=False)
    
    return render_template('auto_replies.html', page='auto_replies', group=group, 
                          auto_replies=auto_replies, auto_replies_json=auto_replies_json)

@core_bp.route('/group/<int:gid>/scheduled_messages')
def page_scheduled_messages(gid):
    """定时消息管理页面"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    scheduled_messages = ScheduledMessage.query.filter_by(group_id=gid).order_by(ScheduledMessage.id.desc()).all()
    
    # 转换为JSON供前端使用
    scheduled_messages_json = json.dumps([{
        'id': sm.id,
        'media_type': sm.media_type,
        'media_url': sm.media_url,
        'content': sm.content,
        'links': sm.links,
        'repeat_interval': sm.repeat_interval,
        'delete_previous': sm.delete_previous,
        'start_time': sm.start_time.isoformat() if sm.start_time else None,
        'stop_time': sm.stop_time.isoformat() if sm.stop_time else None,
        'remark': sm.remark,
        'is_active': sm.is_active,
        'last_sent_at': sm.last_sent_at.isoformat() if sm.last_sent_at else None
    } for sm in scheduled_messages], ensure_ascii=False)
    
    return render_template('scheduled_messages.html', page='scheduled_messages', group=group,
                          scheduled_messages=scheduled_messages, scheduled_messages_json=scheduled_messages_json)

# --- API Routes ---
@core_bp.route('/api/toggle_group', methods=['POST'])
def api_toggle_group():
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d:
        return jsonify({'status':'error','msg':'Missing request body'})
    if 'id' not in d:
        return jsonify({'status':'error','msg':'Missing group ID'})
    if 'action' not in d:
        return jsonify({'status':'error','msg':'Missing action'})
    
    try:
        group_id = int(d['id'])
    except (ValueError, TypeError):
        return jsonify({'status':'error','msg':'Invalid group ID'})
    
    g = BotGroup.query.get(group_id)
    if not g: return jsonify({'status':'error','msg':'Group not found'})
    
    if d['action'] == 'delete':
        GroupUser.query.filter_by(group_id=g.id).delete()
        db.session.delete(g)
    elif d['action'] == 'toggle':
        g.is_active = not g.is_active
    else:
        return jsonify({'status':'error','msg':'Invalid action'})
    
    db.session.commit()
    return jsonify({'status':'ok'})

@core_bp.route('/api/save_fields', methods=['POST'])
def api_save_fields():
    if not session.get('logged_in'): return jsonify({'status':'error'})
    d = request.json
    group = BotGroup.query.get(session['current_group_id'])
    
    fields_data = d.get('fields', d) if isinstance(d, dict) else d
    
    group.fields_config = json.dumps(fields_data, ensure_ascii=False)
    db.session.commit()
    return jsonify({'status':'ok'})

@core_bp.route('/api/save_settings', methods=['POST'])
def api_save_settings():
    if not session.get('logged_in'): return jsonify({'status':'error'})
    d = request.json
    group = BotGroup.query.get(d['group_id'])
    group.config = json.dumps(d['config'], ensure_ascii=False)
    db.session.commit()
    return jsonify({'status':'ok'})

@core_bp.route('/api/save_user', methods=['POST'])
def api_save_user():
    if not session.get('logged_in'): return jsonify({'status':'error'})
    d = request.json
    gid = d['group_id']
    uid = d.get('tg_id')
    if not uid: return jsonify({'status':'error','msg':'No ID'})
    
    u = GroupUser.query.filter_by(group_id=gid, tg_id=uid).first()
    if not u:
        u = GroupUser(group_id=gid, tg_id=uid)
        db.session.add(u)
    
    u.profile_data = json.dumps(d['profile'], ensure_ascii=False)
    add = safe_int(d.get('add_days'))
    if add != 0:
        base = u.expiration_date or get_beijing_now()
        u.expiration_date = base + timedelta(days=add)
        if add > 0 and u.is_banned:
            u.is_banned = False
            try: 
                group = BotGroup.query.get(gid)
                asyncio.run_coroutine_threadsafe(
                    global_ptb_app.bot.restrict_chat_member(
                        chat_id=group.chat_id,
                        user_id=u.tg_id,
                        permissions=ChatPermissions.all_permissions()
                    ),
                    global_bot_loop
                ).result(timeout=5)
            except Exception as e:
                print(f"Failed to unban user: {e}")

    db.session.commit()
    return jsonify({'status':'ok'})

@core_bp.route('/api/delete_user', methods=['POST'])
def api_delete_user():
    if not session.get('logged_in'): return jsonify({'status':'error'})
    GroupUser.query.filter_by(id=request.json['id']).delete()
    db.session.commit()
    return jsonify({'status':'ok'})

@core_bp.route('/api/search_users', methods=['POST'])
def api_search_users():
    if not session.get('logged_in'): return jsonify({'status':'error'})
    d = request.json
    keyword = d.get('keyword', '').strip()
    gid = session.get('current_group_id')
    
    if not gid or not keyword:
        return jsonify({'status':'error', 'msg':'Missing parameters'})
    
    group = BotGroup.query.get(gid)
    if not group:
        return jsonify({'status':'error', 'msg':'Group not found'})
    
    fields = get_group_fields(group)
    
    # Search users by keyword in profile_data
    # Requirement: "所有的查询只显示已经今日打卡的认证用户" (ALL queries should only show users who checked in today)
    today = get_beijing_today()
    users = GroupUser.query.filter(
        GroupUser.group_id == gid,
        GroupUser.online == True,
        GroupUser.checkin_time >= today,
        GroupUser.profile_data.contains(keyword)
    ).order_by(GroupUser.id.desc()).limit(200).all()
    
    result_users = []
    for u in users:
        try:
            profile = json.loads(u.profile_data) if u.profile_data else {}
        except (ValueError, TypeError, json.JSONDecodeError):
            profile = {}
        
        result_users.append({
            'id': u.id,
            'tg_id': u.tg_id,
            'profile': profile,
            'is_banned': u.is_banned,
            'expiration_date': u.expiration_date.strftime('%Y-%m-%d %H:%M:%S') if u.expiration_date else None
        })
    
    return jsonify({
        'status': 'ok',
        'users': result_users,
        'fields': fields
    })


@core_bp.route('/api/push_user', methods=['POST'])
def api_push_user():
    if not session.get('logged_in'): return jsonify({'status':'error'})
    try:
        user = GroupUser.query.get(request.json['id'])
        if not user: return jsonify({'status':'error','msg':'User not found'})
        
        group = BotGroup.query.get(user.group_id)
        conf = get_group_conf(group)
        cid = conf.get('push_channel_id')
        
        if not cid: return jsonify({'status':'error','msg':'请先在功能配置中填写推送频道ID'})
        
        tpl = conf.get('push_template', '用户: {tg_id}')
        text = tpl.replace('{tg_id}', str(user.tg_id)).replace('{onlineEmoji}', '🟢' if user.online else '🔴').replace('{序号}', str(user.id))
        
        p = json.loads(user.profile_data or '{}')
        for k,v in p.items(): text = text.replace(f'{{{k}}}', str(v)) 
        
        fields = get_group_fields(group)
        for f in fields:
            val = p.get(f['key'], '')
            text = text.replace(f"{{{f['label']}}}", str(val))

        # Sanitize HTML before sending to Telegram
        text = sanitize_html_for_telegram(text)

        asyncio.run_coroutine_threadsafe(
            global_ptb_app.bot.send_message(chat_id=cid, text=text, parse_mode='HTML'),
            global_bot_loop
        )
        return jsonify({'status':'ok'})
    except Exception as e:
        return jsonify({'status':'error','msg':str(e)})

# --- Auto Reply API Routes ---
@core_bp.route('/api/save_auto_reply', methods=['POST'])
def api_save_auto_reply():
    """保存自动回复规则"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d: return jsonify({'status':'error','msg':'Missing request body'})
    
    group_id = d.get('group_id')
    if not group_id: return jsonify({'status':'error','msg':'Missing group_id'})
    
    trigger_keyword = d.get('trigger_keyword', '').strip()
    if not trigger_keyword: return jsonify({'status':'error','msg':'触发关键词不能为空'})
    
    try:
        item_id = d.get('id')
        if item_id:
            # 编辑现有规则
            item = AutoReply.query.get(item_id)
            if not item: return jsonify({'status':'error','msg':'Rule not found'})
            if item.group_id != group_id: return jsonify({'status':'error','msg':'Permission denied'})
        else:
            # 新增规则
            item = AutoReply(group_id=group_id)
            db.session.add(item)
        
        item.trigger_keyword = trigger_keyword
        item.media_type = d.get('media_type', 'text')
        item.media_url = d.get('media_url', '').strip() or None
        item.content = d.get('content', '').strip() or None
        item.links = d.get('links', '[]')
        item.delete_after = safe_int(d.get('delete_after'), 0)
        item.remark = d.get('remark', '').strip() or None
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/toggle_auto_reply', methods=['POST'])
def api_toggle_auto_reply():
    """切换自动回复规则状态"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        item = AutoReply.query.get(d['id'])
        if not item: return jsonify({'status':'error','msg':'Rule not found'})
        item.is_active = not item.is_active
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_auto_reply', methods=['POST'])
def api_delete_auto_reply():
    """删除自动回复规则"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        item = AutoReply.query.get(d['id'])
        if not item: return jsonify({'status':'error','msg':'Rule not found'})
        db.session.delete(item)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

# --- Scheduled Message API Routes ---
@core_bp.route('/api/save_scheduled_message', methods=['POST'])
def api_save_scheduled_message():
    """保存定时消息"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d: return jsonify({'status':'error','msg':'Missing request body'})
    
    group_id = d.get('group_id')
    if not group_id: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        item_id = d.get('id')
        if item_id:
            # 编辑现有消息
            item = ScheduledMessage.query.get(item_id)
            if not item: return jsonify({'status':'error','msg':'Message not found'})
            if item.group_id != group_id: return jsonify({'status':'error','msg':'Permission denied'})
        else:
            # 新增消息
            item = ScheduledMessage(group_id=group_id)
            db.session.add(item)
        
        item.media_type = d.get('media_type', 'text')
        item.media_url = d.get('media_url', '').strip() or None
        item.content = d.get('content', '').strip() or None
        item.links = d.get('links', '[]')
        item.repeat_interval = safe_int(d.get('repeat_interval'), 0)
        item.delete_previous = d.get('delete_previous', False)
        
        # 解析时间
        start_time_str = d.get('start_time')
        stop_time_str = d.get('stop_time')
        item.start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        item.stop_time = datetime.fromisoformat(stop_time_str) if stop_time_str else None
        
        item.remark = d.get('remark', '').strip() or None
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/toggle_scheduled_message', methods=['POST'])
def api_toggle_scheduled_message():
    """切换定时消息状态"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        item = ScheduledMessage.query.get(d['id'])
        if not item: return jsonify({'status':'error','msg':'Message not found'})
        item.is_active = not item.is_active
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_scheduled_message', methods=['POST'])
def api_delete_scheduled_message():
    """删除定时消息"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        item = ScheduledMessage.query.get(d['id'])
        if not item: return jsonify({'status':'error','msg':'Message not found'})
        db.session.delete(item)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/magic_login')
def magic_login():
    token = request.args.get('token')
    try:
        data = jwt.decode(token, os.getenv('SECRET_KEY', 'default_secret_key'), algorithms=['HS256'])
        user_id = data.get('uid')
        chat_id = data.get('chat_id')
        
        # Check if user is ADMIN_ID (global admin)
        admin_id = safe_int(os.getenv('ADMIN_ID', 0))
        if admin_id and user_id == admin_id:
            session['logged_in'] = True
            return redirect('/core/select_group')
        
        # Check if user is admin in the specific group
        if chat_id and global_ptb_app:
            try:
                # Run async check in the bot loop
                future = asyncio.run_coroutine_threadsafe(
                    is_user_admin_in_group(global_ptb_app.bot, chat_id, user_id),
                    global_bot_loop
                )
                is_admin = future.result(timeout=5)
                if is_admin:
                    session['logged_in'] = True
                    return redirect('/core/select_group')
            except Exception as e:
                print(f"Error checking group admin: {e}")
        
        return "无权限访问后台，仅限机器人管理员", 403
    except Exception as e:
        print(f"Login error: {e}")
        return "Invalid Token", 403

@core_bp.route('/auth_verify/<session_token>')
def auth_verify_page(session_token):
    """Display verification page with code"""
    try:
        auth_session = AuthSession.query.filter_by(session_token=session_token).first()
        
        if not auth_session:
            return "无效的验证链接", 404
        
        # Check if session has expired
        if get_beijing_now() > auth_session.expires_at:
            return "验证链接已过期", 403
        
        # Check if already verified
        if auth_session.is_verified:
            return redirect('/core/select_group')
        
        return render_template('auth_verify.html', 
                             code=auth_session.verification_code,
                             session_token=session_token)
    except Exception as e:
        print(f"Error in auth_verify_page: {e}")
        return "服务器错误", 500

@core_bp.route('/api/check_auth_status', methods=['POST'])
def api_check_auth_status():
    """Check if authentication session has been verified"""
    if not session.get('logged_in'):
        data = request.json
        session_token = data.get('session_token')
        
        if not session_token:
            return jsonify({'status': 'error', 'msg': 'Missing session_token'})
        
        auth_session = AuthSession.query.filter_by(session_token=session_token).first()
        
        if not auth_session:
            return jsonify({'status': 'error', 'msg': 'Invalid session'})
        
        # Check if expired
        if get_beijing_now() > auth_session.expires_at:
            return jsonify({'status': 'expired'})
        
        # Check if verified
        if auth_session.is_verified:
            # Set session as logged in
            session['logged_in'] = True
            return jsonify({
                'status': 'verified',
                'redirect_url': '/core/select_group'
            })
        
        return jsonify({'status': 'pending'})
    else:
        return jsonify({'status': 'verified', 'redirect_url': '/core/select_group'})


@core_bp.route('/logout')
def logout():
    session.clear()
    return "已退出，请关闭窗口。"

# =======================
# 🤖 机器人逻辑 (核心)
# =======================

async def check_expired_users(context):
    """
    Periodic job to check for expired users and mute them in groups
    """
    if not global_flask_app:
        return
    
    def _sync_check():
        with global_flask_app.app_context():
            try:
                now = get_beijing_now()
                # Find expired users with a limit to avoid memory issues
                # Process in batches for large datasets
                expired_users = GroupUser.query.options(
                    joinedload(GroupUser.group)
                ).filter(
                    GroupUser.expiration_date.isnot(None),
                    GroupUser.expiration_date < now,
                    GroupUser.is_banned == False
                ).limit(EXPIRED_USERS_BATCH_SIZE).all()
                
                if expired_users:
                    print(f"🔍 Found {len(expired_users)} expired users to ban (batch limit: {EXPIRED_USERS_BATCH_SIZE})", flush=True)
                
                # Collect users to ban and prepare async operations
                # Also retrieve configurations here to avoid repeated context creation
                users_to_ban = []
                for user in expired_users:
                    if user.group and user.group.is_active:
                        # Get configuration in sync context
                        conf = get_group_conf(user.group)
                        ban_msg = conf.get('msg_expired_ban', '⛔️ <b>您的认证已过期，已被暂时禁言。请联系管理员续费。</b>')
                        users_to_ban.append((user, user.group, ban_msg))
                
                if not users_to_ban:
                    return None
                    
                # Mark users as banned in database first
                for user, group, _ in users_to_ban:
                    user.is_banned = True
                
                # Commit all changes at once
                db.session.commit()
                print(f"✅ Marked {len(users_to_ban)} users as banned in database", flush=True)
                
                # Return the list for async processing
                return users_to_ban
                            
            except Exception as e:
                print(f"Error in check_expired_users sync part: {e}")
                db.session.rollback()
                return None
    
    # Run sync DB operations in executor
    users_to_ban = await asyncio.get_running_loop().run_in_executor(None, _sync_check)
    
    if not users_to_ban:
        return
    
    # Now perform all async Telegram operations with rate limiting
    async def ban_user_async(user, group, ban_msg):
        """Ban a single user and send notification"""
        try:
            # Ban the user in the group
            await context.bot.restrict_chat_member(
                chat_id=group.chat_id,
                user_id=user.tg_id,
                permissions=ChatPermissions(can_send_messages=False)
            )
            print(f"⛔️ Banned expired user {user.tg_id} in group {group.title}", flush=True)
            
            # Try to send notification to user privately
            try:
                # Sanitize HTML before sending to Telegram
                sanitized_msg = sanitize_html_for_telegram(ban_msg)
                await context.bot.send_message(
                    chat_id=user.tg_id,
                    text=sanitized_msg,
                    parse_mode='HTML'
                )
            except Exception as e:
                # If private message fails, we don't send to group to avoid spam
                print(f"Failed to send ban notification to user {user.tg_id}: {e}")
                
        except Exception as e:
            print(f"Error banning user {user.tg_id}: {e}")
    
    # Use semaphore to limit concurrent operations and avoid Telegram API rate limits
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BANS)
    
    async def ban_with_limit(user, group, ban_msg):
        async with semaphore:
            await ban_user_async(user, group, ban_msg)
    
    # Run ban operations with rate limiting
    await asyncio.gather(*[ban_with_limit(user, group, ban_msg) for user, group, ban_msg in users_to_ban], return_exceptions=True)

async def check_scheduled_messages(context):
    """
    定时检查需要发送的消息
    """
    if not global_flask_app:
        return
    
    def _sync_check():
        with global_flask_app.app_context():
            try:
                now = get_beijing_now()
                # 查找所有需要发送的定时消息
                messages_to_send = []
                
                scheduled_messages = ScheduledMessage.query.options(
                    joinedload(ScheduledMessage.group)
                ).filter(
                    ScheduledMessage.is_active == True
                ).all()
                
                for msg in scheduled_messages:
                    # 检查群组是否活跃
                    if not msg.group or not msg.group.is_active:
                        continue
                    
                    # 检查开始时间
                    if msg.start_time and now < msg.start_time:
                        continue
                    
                    # 检查停止时间
                    if msg.stop_time and now > msg.stop_time:
                        continue
                    
                    # 检查是否需要发送
                    should_send = False
                    if msg.last_sent_at is None:
                        # 从未发送过
                        should_send = True
                    elif msg.repeat_interval > 0:
                        # 检查重复间隔
                        elapsed_minutes = (now - msg.last_sent_at).total_seconds() / 60
                        if elapsed_minutes >= msg.repeat_interval:
                            should_send = True
                    
                    if should_send:
                        messages_to_send.append({
                            'id': msg.id,
                            'chat_id': msg.group.chat_id,
                            'media_type': msg.media_type,
                            'media_url': msg.media_url,
                            'content': msg.content,
                            'links': msg.links,
                            'delete_previous': msg.delete_previous,
                            'last_message_id': msg.last_message_id
                        })
                
                return messages_to_send
                
            except Exception as e:
                print(f"Error in check_scheduled_messages sync part: {e}")
                return []
    
    # 在 executor 中运行同步 DB 操作
    messages_to_send = await asyncio.get_running_loop().run_in_executor(None, _sync_check)
    
    if not messages_to_send:
        return
    
    # 发送消息
    for msg_data in messages_to_send:
        try:
            chat_id = msg_data['chat_id']
            
            # 如果需要删除上一条消息
            if msg_data['delete_previous'] and msg_data['last_message_id']:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_data['last_message_id'])
                except Exception as e:
                    print(f"Failed to delete previous message: {e}")
            
            # 构建按钮
            buttons = []
            try:
                links = json.loads(msg_data['links'] or '[]')
                for link in links:
                    if link.get('text') and link.get('url'):
                        buttons.append([InlineKeyboardButton(link['text'], url=link['url'])])
            except:
                pass
            
            reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
            
            # 发送消息
            sent_message = None
            content = sanitize_html_for_telegram(msg_data['content'] or '')
            
            if msg_data['media_type'] == 'image' and msg_data['media_url']:
                sent_message = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=msg_data['media_url'],
                    caption=content,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            elif msg_data['media_type'] == 'video' and msg_data['media_url']:
                sent_message = await context.bot.send_video(
                    chat_id=chat_id,
                    video=msg_data['media_url'],
                    caption=content,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            elif content:
                sent_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=content,
                    parse_mode='HTML',
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            
            # 更新发送时间和消息ID
            if sent_message:
                def _update_sent(msg_id, sent_msg_id):
                    with global_flask_app.app_context():
                        scheduled_msg = ScheduledMessage.query.get(msg_id)
                        if scheduled_msg:
                            scheduled_msg.last_sent_at = get_beijing_now()
                            scheduled_msg.last_message_id = sent_msg_id
                            db.session.commit()
                
                await asyncio.get_running_loop().run_in_executor(
                    None, _update_sent, msg_data['id'], sent_message.message_id
                )
                print(f"✅ 定时消息已发送到群组 {chat_id}", flush=True)
                
        except Exception as e:
            print(f"Error sending scheduled message: {e}")

async def run_bot(app_instance):
    """
    初始化机器人，接收 Flask App 实例以便在回调中使用 Context
    """
    token = os.getenv('TG_BOT_TOKEN')
    if not token: 
        print("⚠️ 未设置 TG_BOT_TOKEN")
        return

    global global_bot_loop, global_flask_app
    global_bot_loop = asyncio.get_running_loop()
    global_flask_app = app_instance # 📦 存储 Flask App 实例

    print("🤖 正在初始化 Bot...", flush=True)
    app = Application.builder().token(token).build()
    
    global global_ptb_app
    global_ptb_app = app

    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(pagination_callback)) 
    app.add_handler(CommandHandler("start", cmd_start))
    
    # Add periodic job to check expired users
    app.job_queue.run_repeating(check_expired_users, interval=EXPIRATION_CHECK_INTERVAL, first=10)
    
    # Add periodic job to check scheduled messages
    app.job_queue.run_repeating(check_scheduled_messages, interval=SCHEDULED_MESSAGE_CHECK_INTERVAL, first=15)
    
    await app.initialize()
    await app.start()
    
    # 根据环境变量自动判断运行模式
    domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip()
    if domain:
        # Webhook 模式：注册 Webhook 地址到 Telegram
        webhook_url = f"https://{domain}/core/webhook"
        try:
            await app.bot.set_webhook(url=webhook_url)
            print(f"✅ Bot 初始化完成 (Webhook 模式)，Webhook URL: {webhook_url}", flush=True)
        except Exception as e:
            print(f"❌ Webhook 设置失败: {e}", flush=True)
            raise
    else:
        # Polling 模式：开始轮询拉取消息
        print("✅ Bot 初始化完成 (Polling 模式)，开始轮询...", flush=True)
        try:
            await app.updater.start_polling(drop_pending_updates=True)
            print("✅ Polling 已启动", flush=True)
        except Exception as e:
            print(f"❌ Polling 启动失败: {e}", flush=True)
            raise

def do_like(chat_id, message_id, emoji):
    token = os.getenv('TG_BOT_TOKEN')
    if not token or not emoji: return
    clean_emoji = emoji.strip()
    try: 
        url = f"https://api.telegram.org/bot{token}/setMessageReaction"
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": clean_emoji}]}, timeout=5)
    except Exception as e: print(f"❌ [Like] 请求异常: {e}", flush=True)

async def cmd_start(update: Update, context):
    print(f"✅ /start 命令被触发，用户 ID: {update.effective_user.id}")
    user_id = update.effective_user.id
    admin_id = safe_int(os.getenv('ADMIN_ID', 0))
    
    if user_id == admin_id:
        # Create authentication session for admin
        def _create_auth_session():
            with global_flask_app.app_context():
                # Clean up old sessions for this user
                AuthSession.query.filter_by(user_id=user_id).delete()
                
                # Create new auth session
                session_token = generate_session_token()
                verification_code = generate_verification_code()
                expires_at = get_beijing_now() + timedelta(minutes=AUTH_SESSION_EXPIRY_MINUTES)
                
                auth_session = AuthSession(
                    user_id=user_id,
                    session_token=session_token,
                    verification_code=verification_code,
                    is_verified=False,
                    expires_at=expires_at
                )
                db.session.add(auth_session)
                db.session.commit()
                
                return session_token
        
        # Create session in executor to avoid blocking
        session_token = await asyncio.get_running_loop().run_in_executor(None, _create_auth_session)
        
        domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '').rstrip('/')
        if domain:
            verify_url = f"https://{domain}/core/auth_verify/{session_token}"
        else:
            verify_url = f"http://localhost:5000/core/auth_verify/{session_token}"
        
        # Create inline keyboard with button
        keyboard = [[InlineKeyboardButton("🔐 点击验证身份", url=verify_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_html(
            "👋 <b>欢迎，管理员！</b>\n\n"
            "请点击下方按钮进入身份验证页面，\n"
            "将页面上的验证码发送给我以完成登录。",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_html(f"👋 你好！我是打卡机器人。\n你的 ID 是：<code>{user_id}</code>")


async def on_my_chat_member(update: Update, context):
    try:
        chat = update.effective_chat
        status = update.my_chat_member.new_chat_member.status
        user = update.effective_user
        
        if chat.type in ['group', 'supergroup'] and status in ['administrator', 'member']:
            # 使用全局 App Context
            with global_flask_app.app_context():
                g = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if not g:
                    g = BotGroup(chat_id=str(chat.id), title=chat.title, type=chat.type, is_active=True)
                    g.fields_config = json.dumps(DEFAULT_FIELDS, ensure_ascii=False)
                    db.session.add(g)
                    db.session.commit()
                    print(f"➕ 新群组注册: {chat.title}")
                
            domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
            if domain:
                # Check if user is admin in the group
                is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
                if is_admin:
                    token = jwt.encode({'uid': user.id, 'chat_id': chat.id, 'exp': time.time() + 86400 * JWT_TOKEN_EXPIRY_DAYS}, os.getenv('SECRET_KEY', 'default_secret_key'), algorithm='HS256')
                    url = f"https://{domain}/core/magic_login?token={token}"
                    try: 
                        await context.bot.send_message(chat.id, f"✅ 机器人已激活！\n\n👉 [点击进入后台管理]({url})\n\n⚠️ 注意：仅群组管理员可访问后台", parse_mode='Markdown')
                    except: pass
                else:
                    try: 
                        await context.bot.send_message(chat.id, f"✅ 机器人已激活！")
                    except: pass
    except Exception as e: print(f"Error in on_my_chat_member: {e}")

async def on_message(update: Update, context):
    if not global_flask_app: return
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not msg.text or not chat: return

        txt = msg.text.strip()
        
        # Check if this is a verification code from admin in private chat
        if chat.type == 'private':
            admin_id = safe_int(os.getenv('ADMIN_ID', 0))
            if user.id == admin_id and txt.isdigit() and len(txt) == 6:
                # Try to verify the code using constant-time comparison
                def _verify_code():
                    with global_flask_app.app_context():
                        # Get all pending sessions for this user
                        pending_sessions = AuthSession.query.filter_by(
                            user_id=user.id,
                            is_verified=False
                        ).all()
                        
                        # Use constant-time comparison to prevent timing attacks
                        for auth_session in pending_sessions:
                            if hmac.compare_digest(auth_session.verification_code, txt):
                                # Check if not expired
                                if get_beijing_now() <= auth_session.expires_at:
                                    auth_session.is_verified = True
                                    db.session.commit()
                                    return True
                                else:
                                    return False
                        return None
                
                result = await asyncio.get_running_loop().run_in_executor(None, _verify_code)
                
                if result is True:
                    await msg.reply_html("✅ <b>验证成功！</b>\n\n网页将自动跳转到管理后台。")
                    return
                elif result is False:
                    await msg.reply_html("⚠️ <b>验证码已过期</b>\n\n请重新发送 /start 获取新的验证码。")
                    return
                # If result is None, fall through to normal message processing

        # 使用全局 App Context
        with global_flask_app.app_context():
            # 1. 自动点赞
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group or not group.is_active:
                return
            
            conf = get_group_conf(group)
            if conf.get('auto_like'):
                db_user = GroupUser.query.filter_by(group_id=group.id, tg_id=user.id).first()
                if db_user:
                    emoji = conf.get('like_emoji', '❤️')
                    # 在线程中执行阻塞请求，避免卡顿
                    asyncio.get_running_loop().run_in_executor(None, do_like, chat.id, msg.message_id, emoji)
            
            # 2. 打卡
            checkin_cmds = [c.strip() for c in conf.get('checkin_cmd', '打卡').split(',')]
            if conf.get('checkin_open') and txt in checkin_cmds:
                db_user = GroupUser.query.filter_by(group_id=group.id, tg_id=user.id).first()
                if not db_user:
                    msg_text = sanitize_html_for_telegram(conf.get('msg_not_registered', '未认证'))
                    await msg.reply_html(msg_text)
                else:
                    # Check if user is expired and should be banned
                    if db_user.expiration_date and get_beijing_now() > db_user.expiration_date:
                        if not db_user.is_banned:
                            db_user.is_banned = True
                            db.session.commit()
                            try:
                                await context.bot.restrict_chat_member(
                                    chat_id=chat.id,
                                    user_id=user.id,
                                    permissions=ChatPermissions(can_send_messages=False)
                                )
                            except Exception as e:
                                print(f"Failed to ban user {user.id}: {e}")
                        # Send expiration notification privately to the user, not to the group
                        msg_text = sanitize_html_for_telegram(conf.get('msg_expired_ban', '⛔️ 您的认证已过期'))
                        try:
                            await context.bot.send_message(
                                chat_id=user.id,
                                text=msg_text,
                                parse_mode='HTML'
                            )
                        except Exception as e:
                            # If private message fails, just log the error - don't send to group
                            print(f"Failed to send expiration notification to user {user.id}: {e}")
                    else:
                        # Check if user has already checked in today
                        today = get_beijing_today()
                        if db_user.checkin_time and db_user.checkin_time >= today:
                            # User already checked in today, send repeat check-in message
                            msg_text = sanitize_html_for_telegram(conf.get('msg_repeat_checkin', '🔄 <b>今天已打卡</b>'))
                            r = await msg.reply_html(msg_text)
                            del_time = safe_int(conf.get('checkin_del_time'), 0)
                            if del_time > 0:
                                context.job_queue.run_once(lambda c: c.job.data.delete(), del_time, data=r)
                        else:
                            # First check-in today, proceed normally
                            db_user.checkin_time = get_beijing_now()
                            db_user.online = True
                            db.session.commit()
                            msg_text = sanitize_html_for_telegram(conf.get('msg_checkin_success', '打卡成功'))
                            r = await msg.reply_html(msg_text)
                            del_time = safe_int(conf.get('checkin_del_time'), 0)
                            if del_time > 0:
                                context.job_queue.run_once(lambda c: c.job.data.delete(), del_time, data=r)
                return

            # 3. 自动回复检查 (优先级高于查询)
            # Check for auto-reply first to avoid conflicts with query
            auto_reply = AutoReply.query.filter_by(
                group_id=group.id,
                trigger_keyword=txt,
                is_active=True
            ).first()
            
            if auto_reply:
                try:
                    # 构建按钮
                    buttons = []
                    try:
                        links = json.loads(auto_reply.links or '[]')
                        for link in links:
                            if link.get('text') and link.get('url'):
                                buttons.append([InlineKeyboardButton(link['text'], url=link['url'])])
                    except:
                        pass
                    
                    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
                    
                    # 发送回复
                    sent_reply = None
                    content = sanitize_html_for_telegram(auto_reply.content or '')
                    
                    if auto_reply.media_type == 'image' and auto_reply.media_url:
                        sent_reply = await msg.reply_photo(
                            photo=auto_reply.media_url,
                            caption=content,
                            parse_mode='HTML',
                            reply_markup=reply_markup
                        )
                    elif auto_reply.media_type == 'video' and auto_reply.media_url:
                        sent_reply = await msg.reply_video(
                            video=auto_reply.media_url,
                            caption=content,
                            parse_mode='HTML',
                            reply_markup=reply_markup
                        )
                    elif content:
                        sent_reply = await msg.reply_html(
                            content,
                            reply_markup=reply_markup,
                            disable_web_page_preview=True
                        )
                    
                    # 自动删除回复
                    if sent_reply and auto_reply.delete_after > 0:
                        context.job_queue.run_once(
                            lambda c: c.job.data.delete(),
                            auto_reply.delete_after,
                            data=sent_reply
                        )
                except Exception as e:
                    print(f"Auto reply error: {e}")
                return
            
            # 4. 查询功能
            query_cmds = [c.strip() for c in conf.get('query_cmd', '查询').split(',')]
            is_search = False
            kw = None
            
            if conf.get('query_open') and txt in query_cmds:
                is_search = True
            elif conf.get('query_filter_open'):
                for cmd in query_cmds:
                    if txt.startswith(cmd + " "):
                        kw = txt[len(cmd):].strip()
                        is_search = True
                        break
                # Treat as placeholder query keyword (short text that's not a command)
                if not is_search and 0 < len(txt) < 15 and not txt.startswith('/'):
                    kw = txt
                    is_search = True
            
            if is_search:
                fields = get_group_fields(group)
                # 关键：在这里调用查询，上下文已在上方 with 块中建立
                text_resp, markup, users = await do_query_page(chat.id, group.id, conf, fields, kw, 1)
                
                if users or (not kw and not users):
                    if not text_resp: text_resp = "😢 暂无数据"
                    sent = await msg.reply_html(text_resp, reply_markup=markup, disable_web_page_preview=True)
                    del_time = safe_int(conf.get('query_del_time'), 60)
                    if del_time > 0:
                        context.job_queue.run_once(lambda c: c.job.data.delete(), del_time, data=sent)
                return

    except Exception as e:
        print(f"Msg Error: {e}")

# --- 分页逻辑 ---
async def do_query_page(chat_id, group_id, conf, fields, kw=None, page=1):
    # ⚡️ 修复：移除 create_app()，假定外部已建立 Context，或者在这里使用全局 app
    # 为了兼容 pagination_callback 和 on_message，这里做个判断
    # 如果已经在 Context 中（如 on_message 调用），此代码块复用 Context 还是会正常工作？
    # Flask SQLAlchemy 的 Context 是 Thread Local 的。
    # 因为我们在 async 函数中，建议显式使用 global_flask_app
    
    if not global_flask_app: return None, None, None

    # 使用 run_in_executor 避免阻塞 Async Loop
    def _sync_query():
        nonlocal page
        with global_flask_app.app_context():
            today = get_beijing_today()
            # Always filter by today's check-in, whether it's a keyword search or not
            # Requirement: "所有的查询只显示已经今日打卡的认证用户" (ALL queries should only show users who checked in today)
            base = GroupUser.query.filter(
                GroupUser.group_id == group_id,
                GroupUser.online == True,
                GroupUser.checkin_time >= today
            )
            
            if kw:
                base = base.filter(GroupUser.profile_data.contains(kw))
                header = conf.get('msg_filter_header', '🔍 <b>筛选结果：</b>')
            else:
                header = conf.get('msg_query_header', '🔍 <b>今日在线：</b>')
                
            # Requirement 4: Sort by check-in time (earliest first)
            # Note: We're filtering by checkin_time >= today, so NULL values are already excluded
            users = base.order_by(GroupUser.checkin_time.asc()).all()
            if not users: return None, None, None
            
            page_size = safe_int(conf.get('page_size'), 10)
            total_pages = math.ceil(len(users) / page_size) or 1
            if page > total_pages: page = total_pages
            if page < 1: page = 1
            
            start = (page - 1) * page_size
            current_users = users[start:start+page_size]
            
            tpl = conf.get('template', '{tg_id}')
            f_map = {f['key']: f['label'] for f in fields}
            lines = []
            for idx, u in enumerate(current_users):
                try:
                    d = json.loads(u.profile_data or '{}')
                    l = tpl.replace("{onlineEmoji}", conf.get('online_emoji',''))
                    for k, lbl in f_map.items(): l = l.replace(f"{{{lbl}}}", str(d.get(k,'')))
                    l = l.replace("{序号}", str(start + idx + 1))
                    l = l.replace("{tg_id}", str(u.tg_id))
                    lines.append(re.sub(r'\{.*?\}', '', l))
                except: continue
                
            # Add page number display at the end of text
            text = header + "\n\n" + "\n".join(lines)
            text = text + f"\n\n📄 第 {page}/{total_pages} 页"
            
            # Sanitize HTML before sending to Telegram
            text = sanitize_html_for_telegram(text)
            
            buttons = []
            nav_row = []
            safe_kw = kw if kw else "None"
            if page > 1: nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"pg|{page-1}|{safe_kw}"))
            if page < total_pages: nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"pg|{page+1}|{safe_kw}"))
            if nav_row: buttons.append(nav_row)
            
            custom_btns = conf.get('custom_buttons', '')
            if custom_btns:
                try:
                    btn_list = json.loads(custom_btns)
                    row = []
                    for btn in btn_list:
                        row.append(InlineKeyboardButton(btn['text'], url=btn['url']))
                        if len(row) == 2:
                            buttons.append(row)
                            row = []
                    if row: buttons.append(row)
                except: pass
                
            return text, InlineKeyboardMarkup(buttons), users

    # 在 Executor 中运行同步 DB 操作
    return await asyncio.get_running_loop().run_in_executor(None, _sync_query)

async def pagination_callback(update: Update, context):
    query = update.callback_query
    if query.data == "noop": return await query.answer()
    try:
        parts = query.data.split('|')
        page = int(parts[1])
        kw = parts[2] if parts[2] != "None" else None
        
        chat = update.effective_chat
        
        # ⚡️ 修复：使用全局 Flask App
        if not global_flask_app: return await query.answer("System Starting...")

        # 上面 lambda 写法太绕，直接用同步函数包装即可：
        def _get_group_info():
            with global_flask_app.app_context():
                g = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if not g: return None, None, None
                return g.id, get_group_conf(g), get_group_fields(g)
        
        res = await asyncio.get_running_loop().run_in_executor(None, _get_group_info)
        if not res or not res[0]: return await query.answer("Expired")
        gid, conf, fields = res

        text, markup, _ = await do_query_page(chat.id, gid, conf, fields, kw, page)
        if text:
            await query.edit_message_text(text=text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)
    except Exception as e: 
        print(f"Page Error: {e}")
    await query.answer()
