from flask import Blueprint, render_template, request, redirect, session, jsonify
from app import db
from app.models import (BotGroup, GroupUser, DEFAULT_FIELDS, DEFAULT_SYSTEM, AuthSession, AutoReply, ScheduledMessage, StartMessage,
                        GroupEntryExitSettings, SpamProtection, TimedGroupControl, InvitationActivity, ForcedChannelSubscription,
                        PointsRule, PointsAutoReply, PointsAuction, PointsLog, UserPoints, GroupLottery, MemberLevel,
                        UserNameChange, GroupBottomButton, SyncGroupMessages, SyncMessageLog, OtherSettings, BotClone, LotteryMessageCount,
                        InactiveUserSettings, KeywordFilter, MessageStatistics, GroupVote, VoteRecord, QuizGame, QuizSession, 
                        QuizAnswer, RedPacket, RedPacketClaim, AdminActionLog)
from app.services import sanitize_html_for_telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, ChatMember, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, LinkPreviewOptions
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatMemberHandler, filters
from sqlalchemy.orm import joinedload
from sqlalchemy import or_, cast, String, func
import os, jwt, time, json, asyncio, re, requests, math, secrets, string, hmac, csv, io, logging, traceback, random
from datetime import datetime, timedelta
import pytz
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from io import BytesIO

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
MAX_LOTTERY_PARTICIPANTS = 100  # Maximum participants to consider in lottery fallback
MAX_LOTTERY_MESSAGE_COUNT_RECORDS = 10000  # Maximum message count records to load for lottery
MAX_AUCTION_WINNERS = 100  # Maximum winners in auction/lottery ranking

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

def build_inline_keyboard_from_links(links):
    """
    将链接列表转换为内联键盘，支持多个按钮在一行显示
    
    Args:
        links: 链接列表，每个链接应包含 text, url, 以及可选的 row 和 order 字段
        
    Returns:
        List of button rows for InlineKeyboardMarkup
    """
    if not links:
        return []
    
    # Group buttons by row
    rows_dict = {}
    for link in links:
        if not link.get('text') or not link.get('url'):
            continue
        
        row_num = link.get('row', 0)
        order = link.get('order', 0)
        
        if row_num not in rows_dict:
            rows_dict[row_num] = []
        
        rows_dict[row_num].append({
            'button': InlineKeyboardButton(link['text'], url=link['url']),
            'order': order
        })
    
    # Sort rows and buttons within rows
    keyboard = []
    for row_num in sorted(rows_dict.keys()):
        # Sort buttons in this row by order
        row_buttons = sorted(rows_dict[row_num], key=lambda x: x['order'])
        keyboard.append([btn['button'] for btn in row_buttons])
    
    return keyboard

async def is_user_admin_in_group(bot, chat_id, user_id):
    """Check if a user is an administrator in a specific group"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        print(f"Error checking admin status: {e}")
        return False

def get_muted_permissions():
    """返回完全禁言的权限设置（统一管理以避免代码重复）
    
    使用最简单、最兼容的配置：只禁止发送消息
    这是最兼容的方式，避免某些 Telegram 版本或配置下的 API 调用失败
    
    Returns:
        ChatPermissions: 禁止发送消息的权限对象
    """
    return ChatPermissions(
        can_send_messages=False
    )

def get_unrestricted_permissions():
    """返回完全开放的权限设置（统一管理以避免代码重复）
    
    Returns:
        ChatPermissions: 所有权限都被允许的权限对象
    """
    return ChatPermissions(
        can_send_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )

def log_admin_action(group_id, admin_id, admin_name, action_type, target_user_id=None, target_user_name=None, details=None):
    """记录管理员操作到数据库
    
    Args:
        group_id: 群组ID (数据库ID)
        admin_id: 管理员Telegram ID
        admin_name: 管理员名称
        action_type: 操作类型 (kick, ban, mute, unmute, etc.)
        target_user_id: 目标用户Telegram ID (可选)
        target_user_name: 目标用户名称 (可选)
        details: 详细信息 (可选)
    """
    if not global_flask_app:
        return
    
    try:
        with global_flask_app.app_context():
            log = AdminActionLog(
                group_id=group_id,
                admin_id=admin_id,
                admin_name=admin_name,
                action_type=action_type,
                target_user_id=target_user_id,
                target_user_name=target_user_name,
                details=details
            )
            db.session.add(log)
            db.session.commit()
    except Exception as e:
        print(f"Error logging admin action: {e}")


def convert_chat_id_to_int(chat_id, group_id=None, group_title=None):
    """Helper function to convert chat_id to integer for Telegram API.
    
    Args:
        chat_id: The chat_id string to convert
        group_id: Optional group ID for error messages
        group_title: Optional group title for error messages
        
    Returns:
        The chat_id as an integer, or None if conversion fails
    """
    try:
        return int(chat_id)
    except (ValueError, TypeError) as e:
        error_msg = f"Invalid chat_id '{chat_id}'"
        if group_id:
            error_msg += f" for group {group_id}"
        if group_title:
            error_msg += f" ({group_title})"
        error_msg += f": {e}"
        print(error_msg)
        return None

def unban_user_in_group(group_id, user_tg_id):
    """Helper function to unban a user in a Telegram group by lifting all restrictions.
    
    Args:
        group_id: The database group ID
        user_tg_id: The Telegram user ID
        
    Returns:
        True if successful, False otherwise
    """
    try:
        group = BotGroup.query.get(group_id)
        if not group:
            print(f"Group {group_id} not found for unbanning user {user_tg_id}")
            return False
        
        # Convert chat_id to integer for Telegram API
        chat_id_int = convert_chat_id_to_int(group.chat_id, group.id)
        if chat_id_int is None:
            return False
        
        asyncio.run_coroutine_threadsafe(
            global_ptb_app.bot.restrict_chat_member(
                chat_id=chat_id_int,
                user_id=user_tg_id,
                permissions=get_unrestricted_permissions()
            ),
            global_bot_loop
        ).result(timeout=5)
        print(f"✅ User {user_tg_id} unbanned in group {group.chat_id}")
        return True
    except Exception as e:
        print(f"Failed to unban user {user_tg_id} in group {group.chat_id} (chat_id type: {type(group.chat_id).__name__}): {e}")
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
    
    # Enhanced statistics
    total_users = GroupUser.query.filter_by(group_id=gid).count()
    online_users = GroupUser.query.filter_by(group_id=gid, online=True).count()
    
    # Get today's check-ins
    today = get_beijing_today()
    today_checkins = GroupUser.query.filter(
        GroupUser.group_id == gid,
        GroupUser.checkin_time >= today
    ).count()
    
    # Get expired/banned users
    now = get_beijing_now()
    expired_users = GroupUser.query.filter(
        GroupUser.group_id == gid,
        GroupUser.expiration_date.isnot(None),
        GroupUser.expiration_date < now
    ).count()
    
    banned_users = GroupUser.query.filter_by(group_id=gid, is_banned=True).count()
    
    # Get module counts
    auto_replies_count = AutoReply.query.filter_by(group_id=gid, is_active=True).count()
    scheduled_msgs_count = ScheduledMessage.query.filter_by(group_id=gid, is_active=True).count()
    start_msgs_count = StartMessage.query.filter_by(group_id=gid, is_active=True).count()
    
    stats = {
        'users': total_users,
        'online': online_users,
        'today_checkins': today_checkins,
        'expired': expired_users,
        'banned': banned_users,
        'auto_replies': auto_replies_count,
        'scheduled_msgs': scheduled_msgs_count,
        'start_msgs': start_msgs_count
    }
    
    return render_template('dashboard.html', page='dashboard', group=group, stats=stats)

@core_bp.route('/group/<int:gid>/users')
def page_users(gid):
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    # Pagination parameters with enhanced options
    page = safe_int(request.args.get('page', 1), 1)
    per_page = safe_int(request.args.get('per_page', 20), 20)
    # Support larger page sizes including 100
    if per_page not in [10, 20, 50, 100] or per_page <= 0: per_page = 20
    if page < 1: page = 1
    
    # Search parameter
    search_query = request.args.get('search', '').strip()
    
    # Build query with optional search filter
    query = GroupUser.query.filter_by(group_id=gid)
    if search_query:
        # Search in tg_id and profile_data
        query = query.filter(
            or_(
                cast(GroupUser.tg_id, String).contains(search_query),
                GroupUser.profile_data.contains(search_query)
            )
        )
    
    # Get total count and paginated users
    total_users = query.count()
    total_pages = math.ceil(total_users / per_page) if total_users > 0 else 1
    if page > total_pages: page = total_pages
    
    users = query.order_by(GroupUser.id.desc()).offset((page-1)*per_page).limit(per_page).all()
    for u in users:
        try: u.profile_dict = json.loads(u.profile_data) if u.profile_data else {}
        except: u.profile_dict = {}
    
    return render_template('users.html', page='users', group=group, users=users, fields=get_group_fields(group), 
                         current_page=page, total_pages=total_pages, per_page=per_page, total_users=total_users, search=search_query)

@core_bp.route('/group/<int:gid>/members')
def page_group_members(gid):
    """群成员列表页面 - 显示所有有积分记录的用户（包括未认证用户）"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    # Pagination parameters
    page = safe_int(request.args.get('page', 1), 1)
    per_page = safe_int(request.args.get('per_page', 20), 20)
    if per_page not in [10, 20, 50, 100] or per_page <= 0: per_page = 20
    if page < 1: page = 1
    
    # Search and filter parameters
    search_query = request.args.get('search', '').strip()
    filter_type = request.args.get('filter', 'all')  # all, verified, unverified
    
    # 🆕 Query all users with points records (UserPoints table)
    query = UserPoints.query.filter_by(group_id=gid)
    
    # Apply search filter
    if search_query:
        # Search by user_id
        query = query.filter(cast(UserPoints.user_id, String).contains(search_query))
    
    # Apply filter type
    if filter_type == 'verified':
        # Only show users who are in GroupUser table
        verified_user_ids = db.session.query(GroupUser.tg_id).filter_by(group_id=gid).subquery()
        query = query.filter(UserPoints.user_id.in_(verified_user_ids))
    elif filter_type == 'unverified':
        # Only show users who are NOT in GroupUser table
        verified_user_ids = db.session.query(GroupUser.tg_id).filter_by(group_id=gid).subquery()
        query = query.filter(~UserPoints.user_id.in_(verified_user_ids))
    
    # Sort by points balance descending
    query = query.order_by(UserPoints.points_balance.desc())
    
    # Get paginated results
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    user_points_list = pagination.items
    
    # Get verified user information for these users
    user_ids = [up.user_id for up in user_points_list]
    verified_users = {}
    if user_ids:
        verified = GroupUser.query.filter(
            GroupUser.group_id == gid,
            GroupUser.tg_id.in_(user_ids)
        ).all()
        verified_users = {u.tg_id: u for u in verified}
    
    # Get member levels
    levels = MemberLevel.query.filter_by(group_id=gid).order_by(MemberLevel.required_points.desc()).all()
    
    # Build member list with computed properties
    members = []
    for up in user_points_list:
        verified_user = verified_users.get(up.user_id)
        
        # Calculate user level
        user_level = None
        for level in levels:
            if up.points_balance >= level.required_points:
                user_level = level
                break
        
        # Parse profile data for verified users
        profile_dict = {}
        if verified_user:
            try:
                profile_dict = json.loads(verified_user.profile_data) if verified_user.profile_data else {}
            except:
                profile_dict = {}
        
        members.append({
            'user_id': up.user_id,
            'points': up.points_balance,
            'is_verified': verified_user is not None,
            'verified_user': verified_user,
            'profile_dict': profile_dict,
            'level': user_level,
            'updated_at': up.updated_at
        })
    
    # Calculate statistics
    total_users = UserPoints.query.filter_by(group_id=gid).count()
    total_verified = GroupUser.query.filter_by(group_id=gid).count()
    total_points = db.session.query(func.sum(UserPoints.points_balance)).filter(
        UserPoints.group_id == gid
    ).scalar() or 0
    
    return render_template('group_members.html', 
                         page='group_members', 
                         group=group, 
                         members=members,
                         pagination=pagination,
                         search=search_query,
                         filter_type=filter_type,
                         stats={
                             'total_users': total_users,
                             'total_verified': total_verified,
                             'total_points': total_points
                         },
                         now=datetime.now())

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
    
    # Pagination parameters with enhanced options
    page = safe_int(request.args.get('page', 1), 1)
    per_page = safe_int(request.args.get('per_page', 20), 20)
    # Support larger page sizes including 100
    if per_page not in [10, 20, 50, 100] or per_page <= 0: per_page = 20
    if page < 1: page = 1
    
    # Search parameter
    search_query = request.args.get('search', '').strip()
    
    # Build query with optional search filter
    query = AutoReply.query.filter_by(group_id=gid)
    if search_query:
        # Search in trigger_keyword and remark
        query = query.filter(
            or_(
                AutoReply.trigger_keyword.contains(search_query),
                AutoReply.remark.contains(search_query)
            )
        )
    
    # Get total count and paginated results
    total_items = query.count()
    total_pages = math.ceil(total_items / per_page) if total_items > 0 else 1
    if page > total_pages: page = total_pages
    
    auto_replies = query.order_by(AutoReply.id.desc()).offset((page-1)*per_page).limit(per_page).all()
    
    return render_template('auto_replies.html', page='auto_replies', group=group, 
                          auto_replies=auto_replies,
                          current_page=page, total_pages=total_pages, per_page=per_page, total_items=total_items, search=search_query)

@core_bp.route('/group/<int:gid>/scheduled_messages')
def page_scheduled_messages(gid):
    """定时消息管理页面"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    # Pagination parameters with enhanced options
    page = safe_int(request.args.get('page', 1), 1)
    per_page = safe_int(request.args.get('per_page', 20), 20)
    # Support larger page sizes including 100
    if per_page not in [10, 20, 50, 100] or per_page <= 0: per_page = 20
    if page < 1: page = 1
    
    # Search parameter
    search_query = request.args.get('search', '').strip()
    
    # Build query with optional search filter
    query = ScheduledMessage.query.filter_by(group_id=gid)
    if search_query:
        # Search in content and remark
        query = query.filter(
            or_(
                ScheduledMessage.content.contains(search_query),
                ScheduledMessage.remark.contains(search_query)
            )
        )
    
    # Get total count and paginated results
    total_items = query.count()
    total_pages = math.ceil(total_items / per_page) if total_items > 0 else 1
    if page > total_pages: page = total_pages
    
    scheduled_messages = query.order_by(ScheduledMessage.id.desc()).offset((page-1)*per_page).limit(per_page).all()
    
    return render_template('scheduled_messages.html', page='scheduled_messages', group=group,
                          scheduled_messages=scheduled_messages,
                          current_page=page, total_pages=total_pages, per_page=per_page, total_items=total_items, search=search_query)

@core_bp.route('/group/<int:gid>/start_messages')
def page_start_messages(gid):
    """自定义 /start 消息管理页面"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    # Pagination parameters with enhanced options
    page = safe_int(request.args.get('page', 1), 1)
    per_page = safe_int(request.args.get('per_page', 20), 20)
    # Support larger page sizes including 100
    if per_page not in [10, 20, 50, 100] or per_page <= 0: per_page = 20
    if page < 1: page = 1
    
    # Get total count and paginated results
    total_items = StartMessage.query.filter_by(group_id=gid).count()
    total_pages = math.ceil(total_items / per_page) if total_items > 0 else 1
    if page > total_pages: page = total_pages
    
    start_messages = StartMessage.query.filter_by(group_id=gid).order_by(StartMessage.message_type.asc(), StartMessage.id.desc()).offset((page-1)*per_page).limit(per_page).all()
    
    # 转换为JSON供前端使用
    start_messages_json = json.dumps([{
        'id': sm.id,
        'message_type': sm.message_type,
        'media_type': sm.media_type,
        'media_url': sm.media_url,
        'content': sm.content,
        'links': sm.links,
        'is_active': sm.is_active
    } for sm in start_messages], ensure_ascii=False)
    
    return render_template('start_messages.html', page='start_messages', group=group,
                          start_messages=start_messages, start_messages_json=start_messages_json,
                          current_page=page, total_pages=total_pages, per_page=per_page, total_items=total_items)

@core_bp.route('/group/<int:gid>/entry_exit_settings')
def page_entry_exit_settings(gid):
    """进退群设置"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    settings = GroupEntryExitSettings.query.filter_by(group_id=gid).first()
    if not settings:
        settings = GroupEntryExitSettings(group_id=gid)
        db.session.add(settings)
        db.session.commit()
    return render_template('entry_exit_settings.html', page='entry_exit_settings', group=group, settings=settings)

@core_bp.route('/group/<int:gid>/spam_protection')
def page_spam_protection(gid):
    """垃圾防护"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    settings = SpamProtection.query.filter_by(group_id=gid).first()
    whitelist_users = json.loads(settings.whitelist_users if settings and settings.whitelist_users else '[]')
    return render_template('spam_protection.html', page='spam_protection', group=group, settings=settings, whitelist_users=whitelist_users)

@core_bp.route('/group/<int:gid>/timed_group_control')
def page_timed_group_control(gid):
    """定时开关群"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    settings = TimedGroupControl.query.filter_by(group_id=gid).first()
    return render_template('timed_group_control.html', page='timed_group_control', group=group, settings=settings)

@core_bp.route('/group/<int:gid>/other_settings')
def page_other_settings(gid):
    """其他设置"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    settings = OtherSettings.query.filter_by(group_id=gid).first()
    if not settings:
        settings = OtherSettings(group_id=gid)
        db.session.add(settings)
        db.session.commit()
    return render_template('other_settings.html', page='other_settings', group=group, settings=settings)

@core_bp.route('/group/<int:gid>/invitation_activity')
def page_invitation_activity(gid):
    """邀请活动"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    settings = InvitationActivity.query.filter_by(group_id=gid).first()
    if not settings:
        settings = InvitationActivity(group_id=gid)
        db.session.add(settings)
        db.session.commit()
    return render_template('invitation_activity.html', page='invitation_activity', group=group, settings=settings)

@core_bp.route('/group/<int:gid>/forced_channel_subscription')
def page_forced_channel_subscription(gid):
    """强制订阅频道"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    settings = ForcedChannelSubscription.query.filter_by(group_id=gid).first()
    if not settings:
        settings = ForcedChannelSubscription(group_id=gid)
        db.session.add(settings)
        db.session.commit()
    return render_template('forced_channel_subscription.html', page='forced_channel_subscription', group=group, settings=settings)

@core_bp.route('/group/<int:gid>/points_rules')
def page_points_rules(gid):
    """积分规则"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    rules = PointsRule.query.filter_by(group_id=gid).all()
    return render_template('points_rules.html', page='points_rules', group=group, rules=rules)

@core_bp.route('/group/<int:gid>/points_auto_reply')
def page_points_auto_reply(gid):
    """积分自动回复"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    replies = PointsAutoReply.query.filter_by(group_id=gid).all()
    return render_template('points_auto_reply.html', page='points_auto_reply', group=group, replies=replies)

@core_bp.route('/group/<int:gid>/points_auction')
def page_points_auction(gid):
    """积分竞拍"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    auctions = PointsAuction.query.filter_by(group_id=gid).order_by(PointsAuction.created_at.desc()).all()
    return render_template('points_auction.html', page='points_auction', group=group, auctions=auctions)

@core_bp.route('/group/<int:gid>/points_log')
def page_points_log(gid):
    """积分日志"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    logs = PointsLog.query.filter_by(group_id=gid).order_by(PointsLog.created_at.desc()).limit(100).all()
    return render_template('points_log.html', page='points_log', group=group, logs=logs)

@core_bp.route('/group/<int:gid>/group_lottery')
def page_group_lottery(gid):
    """群抽奖"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    lotteries = GroupLottery.query.filter_by(group_id=gid).order_by(GroupLottery.created_at.desc()).all()
    return render_template('group_lottery.html', page='group_lottery', group=group, lotteries=lotteries)

@core_bp.route('/group/<int:gid>/member_level')
def page_member_level(gid):
    """成员等级"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    levels = MemberLevel.query.filter_by(group_id=gid).order_by(MemberLevel.required_points).all()
    return render_template('member_level.html', page='member_level', group=group, levels=levels)

@core_bp.route('/group/<int:gid>/user_name_change')
def page_user_name_change(gid):
    """用户改名监控"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    changes = UserNameChange.query.filter_by(group_id=gid).order_by(UserNameChange.changed_at.desc()).limit(100).all()
    return render_template('user_name_change.html', page='user_name_change', group=group, changes=changes)

@core_bp.route('/group/<int:gid>/group_bottom_button')
def page_group_bottom_button(gid):
    """群底部按钮"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    buttons = GroupBottomButton.query.filter_by(group_id=gid).order_by(GroupBottomButton.row_position, GroupBottomButton.button_order).all()
    
    # Convert buttons to JSON-serializable dictionaries
    buttons_json = json.dumps([{
        'id': b.id,
        'button_text': b.button_text,
        'button_url': b.button_url,
        'button_callback': b.button_callback,
        'button_order': b.button_order,
        'row_position': b.row_position,
        'trigger_keyword': b.trigger_keyword,
        'is_active': b.is_active
    } for b in buttons], ensure_ascii=False)
    
    return render_template('group_bottom_button.html', page='group_bottom_button', group=group, buttons=buttons, buttons_json=buttons_json)

@core_bp.route('/group/<int:gid>/sync_group_messages')
def page_sync_group_messages(gid):
    """同步群消息"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    settings = SyncGroupMessages.query.filter_by(source_group_id=gid).first()
    if not settings:
        # Don't create a new record here to avoid constraint violation
        # Instead, pass None and let the template handle empty state
        settings = None
    return render_template('sync_group_messages.html', page='sync_group_messages', group=group, settings=settings)


@core_bp.route('/group/<int:gid>/sync_message_logs')
def page_sync_message_logs(gid):
    """同步消息日志"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    page = safe_int(request.args.get('page', 1), 1)
    per_page = safe_int(request.args.get('per_page', 20), 20)
    if per_page not in [10, 20, 50, 100]: per_page = 20
    
    # Query sync logs ordered by most recent first
    query = SyncMessageLog.query.filter_by(source_group_id=gid).order_by(SyncMessageLog.synced_at.desc())
    total = query.count()
    logs = query.offset((page - 1) * per_page).limit(per_page).all()
    
    total_pages = math.ceil(total / per_page) if total > 0 else 1
    
    return render_template('sync_message_logs.html', page='sync_message_logs', group=group,
                         logs=logs, current_page=page, per_page=per_page,
                         total_pages=total_pages, total_items=total)

@core_bp.route('/bot_clones')
def page_bot_clones():
    """机器人克隆管理 - 只在主机器人后台显示"""
    if not session.get('logged_in'): return redirect('/core')
    
    clones = BotClone.query.order_by(BotClone.created_at.desc()).all()
    
    # Convert to JSON for JavaScript
    clones_json = json.dumps([{
        'id': c.id,
        'clone_name': c.clone_name,
        'bot_token': c.bot_token,
        'owner_user_id': c.owner_user_id,
        'admin_user_ids': c.admin_user_ids,
        'is_active': c.is_active,
        'expiration_date': c.expiration_date.isoformat() if c.expiration_date else None,
        'webhook_url': c.webhook_url,
        'description': c.description
    } for c in clones], ensure_ascii=False)
    
    return render_template('bot_clones.html', page='bot_clones', clones=clones, clones_json=clones_json, beijing_now=get_beijing_now())

@core_bp.route('/group/<int:gid>/inactive_user_settings')
def page_inactive_user_settings(gid):
    """不活跃用户设置"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    settings = InactiveUserSettings.query.filter_by(group_id=gid).first()
    return render_template('inactive_user_settings.html', page='inactive_user_settings', group=group, settings=settings)

@core_bp.route('/group/<int:gid>/keyword_filter')
def page_keyword_filter(gid):
    """关键词过滤"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    filters_list = KeywordFilter.query.filter_by(group_id=gid).order_by(KeywordFilter.created_at.desc()).all()
    
    # Convert to JSON for JavaScript
    filters_json = json.dumps([{
        'id': f.id,
        'keyword': f.keyword,
        'filter_type': f.filter_type,
        'match_type': f.match_type,
        'action': f.action,
        'is_active': f.is_active
    } for f in filters_list], ensure_ascii=False)
    
    return render_template('keyword_filter.html', page='keyword_filter', group=group, 
                         filters=filters_list, filters_json=filters_json)

@core_bp.route('/group/<int:gid>/message_statistics')
def page_message_statistics(gid):
    """消息统计"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    # Get statistics for the last 7 days
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=7)
    
    stats = db.session.query(
        MessageStatistics.date,
        db.func.sum(MessageStatistics.message_count).label('total_messages'),
        db.func.count(db.func.distinct(MessageStatistics.user_id)).label('active_users')
    ).filter(
        MessageStatistics.group_id == gid,
        MessageStatistics.date >= start_date,
        MessageStatistics.date <= end_date
    ).group_by(MessageStatistics.date).order_by(MessageStatistics.date.desc()).all()
    
    # Get top users
    top_users = db.session.query(
        MessageStatistics.user_id,
        db.func.sum(MessageStatistics.message_count).label('total_messages')
    ).filter(
        MessageStatistics.group_id == gid,
        MessageStatistics.date >= start_date,
        MessageStatistics.date <= end_date
    ).group_by(MessageStatistics.user_id).order_by(
        db.func.sum(MessageStatistics.message_count).desc()
    ).limit(10).all()
    
    return render_template('message_statistics.html', page='message_statistics', 
                         group=group, stats=stats, top_users=top_users, 
                         start_date=start_date, end_date=end_date)

@core_bp.route('/group/<int:gid>/group_votes')
def page_group_votes(gid):
    """群投票管理"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    votes = GroupVote.query.filter_by(group_id=gid).order_by(GroupVote.created_at.desc()).all()
    
    # Convert to JSON for JavaScript
    votes_json = json.dumps([{
        'id': v.id,
        'title': v.title,
        'description': v.description,
        'options': json.loads(v.options),
        'vote_type': v.vote_type,
        'max_choices': v.max_choices,
        'is_anonymous': v.is_anonymous,
        'allow_revote': v.allow_revote,
        'start_time': v.start_time.isoformat() if v.start_time else None,
        'end_time': v.end_time.isoformat() if v.end_time else None,
        'status': v.status
    } for v in votes], ensure_ascii=False)
    
    return render_template('group_votes.html', page='group_votes', group=group, 
                         votes=votes, votes_json=votes_json)

@core_bp.route('/group/<int:gid>/quiz_games')
def page_quiz_games(gid):
    """问答游戏管理"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    quizzes = QuizGame.query.filter_by(group_id=gid).order_by(QuizGame.created_at.desc()).all()
    
    # Convert to JSON for JavaScript
    quizzes_json = json.dumps([{
        'id': q.id,
        'question': q.question,
        'answers': json.loads(q.answers),
        'correct_answer_index': q.correct_answer_index,
        'explanation': q.explanation,
        'points_reward': q.points_reward,
        'time_limit': q.time_limit,
        'difficulty': q.difficulty,
        'category': q.category,
        'is_active': q.is_active
    } for q in quizzes], ensure_ascii=False)
    
    return render_template('quiz_games.html', page='quiz_games', group=group, 
                         quizzes=quizzes, quizzes_json=quizzes_json)

@core_bp.route('/group/<int:gid>/red_packet_settings')
def page_red_packet_settings(gid):
    """红包设置"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    # Get recent red packets
    packets = RedPacket.query.filter_by(group_id=gid).order_by(
        RedPacket.created_at.desc()
    ).limit(20).all()
    
    return render_template('red_packet_settings.html', page='red_packet_settings', 
                         group=group, packets=packets)


@core_bp.route('/group/<int:gid>/admin_logs')
def page_admin_logs(gid):
    """管理员操作日志页面"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    
    # Get pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = 50
    
    # Get admin logs
    pagination = AdminActionLog.query.filter_by(
        group_id=gid
    ).order_by(
        AdminActionLog.created_at.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    
    logs = pagination.items
    
    return render_template('admin_logs.html', page='admin_logs', 
                          group=group, logs=logs, pagination=pagination)


@core_bp.route('/group/<int:gid>/backup')
def page_backup(gid):
    """配置备份页面"""
    if not session.get('logged_in'): return redirect('/core')
    session['current_group_id'] = gid
    group = BotGroup.query.get_or_404(gid)
    return render_template('backup.html', page='backup', group=group)


@core_bp.route('/group/<int:gid>/backup/export')
def api_export_config(gid):
    """导出群组配置为 JSON"""
    if not session.get('logged_in'): 
        return jsonify({'error': 'Not authenticated'}), 401
    
    group = BotGroup.query.get_or_404(gid)
    
    # Build configuration object
    config = {
        'group_info': {
            'title': group.title,
            'chat_id': group.chat_id,
            'type': group.type
        },
        'config': json.loads(group.config) if group.config else {},
        'fields_config': json.loads(group.fields_config) if group.fields_config else [],
        'auto_replies': [
            {
                'trigger_keyword': ar.trigger_keyword,
                'media_type': ar.media_type,
                'media_url': ar.media_url,
                'content': ar.content,
                'links': json.loads(ar.links) if ar.links else [],
                'delete_after': ar.delete_after,
                'remark': ar.remark,
                'is_active': ar.is_active
            }
            for ar in AutoReply.query.filter_by(group_id=gid).all()
        ],
        'scheduled_messages': [
            {
                'media_type': sm.media_type,
                'media_url': sm.media_url,
                'content': sm.content,
                'links': json.loads(sm.links) if sm.links else [],
                'repeat_interval': sm.repeat_interval,
                'delete_previous': sm.delete_previous,
                'start_time': sm.start_time.isoformat() if sm.start_time else None,
                'stop_time': sm.stop_time.isoformat() if sm.stop_time else None,
                'remark': sm.remark,
                'is_active': sm.is_active
            }
            for sm in ScheduledMessage.query.filter_by(group_id=gid).all()
        ],
        'points_rules': [
            {
                'rule_name': pr.rule_name,
                'rule_type': pr.rule_type,
                'points_amount': pr.points_amount,
                'is_active': pr.is_active
            }
            for pr in PointsRule.query.filter_by(group_id=gid).all()
        ],
        'member_levels': [
            {
                'level_name': ml.level_name,
                'required_points': ml.required_points,
                'permissions': json.loads(ml.permissions) if ml.permissions else {},
                'badge_emoji': ml.badge_emoji
            }
            for ml in MemberLevel.query.filter_by(group_id=gid).all()
        ],
        'export_time': datetime.now().isoformat()
    }
    
    return jsonify(config)


@core_bp.route('/group/<int:gid>/backup/import', methods=['POST'])
def api_import_config(gid):
    """导入群组配置"""
    if not session.get('logged_in'): 
        return jsonify({'error': 'Not authenticated'}), 401
    
    group = BotGroup.query.get_or_404(gid)
    
    try:
        config = request.get_json()
        
        # Validate config structure
        if not isinstance(config, dict):
            return jsonify({'error': '配置格式错误'}), 400
        
        # Update group config
        if 'config' in config and isinstance(config['config'], dict):
            group.config = json.dumps(config['config'], ensure_ascii=False)
        
        if 'fields_config' in config and isinstance(config['fields_config'], list):
            group.fields_config = json.dumps(config['fields_config'], ensure_ascii=False)
        
        # Import auto_replies (optional: user can choose to import or not)
        if 'auto_replies' in config and isinstance(config['auto_replies'], list):
            for ar_data in config['auto_replies']:
                if not isinstance(ar_data, dict):
                    continue
                # Check if auto reply with same keyword exists
                existing = AutoReply.query.filter_by(
                    group_id=gid,
                    trigger_keyword=ar_data.get('trigger_keyword', '')
                ).first()
                if not existing:
                    ar = AutoReply(
                        group_id=gid,
                        trigger_keyword=ar_data.get('trigger_keyword', ''),
                        media_type=ar_data.get('media_type', 'text'),
                        media_url=ar_data.get('media_url'),
                        content=ar_data.get('content'),
                        links=json.dumps(ar_data.get('links', []), ensure_ascii=False),
                        delete_after=ar_data.get('delete_after', 0),
                        remark=ar_data.get('remark'),
                        is_active=ar_data.get('is_active', True)
                    )
                    db.session.add(ar)
        
        # Import points_rules (optional)
        if 'points_rules' in config and isinstance(config['points_rules'], list):
            for pr_data in config['points_rules']:
                if not isinstance(pr_data, dict):
                    continue
                # Check if rule with same name exists
                existing = PointsRule.query.filter_by(
                    group_id=gid,
                    rule_name=pr_data.get('rule_name', '')
                ).first()
                if not existing:
                    pr = PointsRule(
                        group_id=gid,
                        rule_name=pr_data.get('rule_name', ''),
                        rule_type=pr_data.get('rule_type', 'custom'),
                        points_amount=pr_data.get('points_amount', 1),
                        is_active=pr_data.get('is_active', True)
                    )
                    db.session.add(pr)
        
        # Import member_levels (optional)
        if 'member_levels' in config and isinstance(config['member_levels'], list):
            for ml_data in config['member_levels']:
                if not isinstance(ml_data, dict):
                    continue
                # Check if level with same name exists
                existing = MemberLevel.query.filter_by(
                    group_id=gid,
                    level_name=ml_data.get('level_name', '')
                ).first()
                if not existing:
                    ml = MemberLevel(
                        group_id=gid,
                        level_name=ml_data.get('level_name', ''),
                        required_points=ml_data.get('required_points', 0),
                        permissions=json.dumps(ml_data.get('permissions', {}), ensure_ascii=False),
                        badge_emoji=ml_data.get('badge_emoji')
                    )
                    db.session.add(ml)
        
        db.session.commit()
        
        return jsonify({'success': True, 'message': '配置导入成功'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@core_bp.route('/health')
def health_check():
    """健康检查端点 - 需要认证或仅返回基本信息"""
    # Check if authenticated for detailed information
    is_authenticated = session.get('logged_in', False)
    
    try:
        # Check database connection
        db.session.execute(db.text('SELECT 1'))
        db_status = 'ok'
    except Exception as e:
        db_status = 'error'
    
    # Check bot status
    bot_status = 'running' if global_ptb_app else 'not_started'
    
    # Basic health status (always available)
    result = {
        'status': 'healthy' if db_status == 'ok' and bot_status == 'running' else 'degraded',
        'timestamp': datetime.now().isoformat()
    }
    
    # Detailed information (only when authenticated)
    if is_authenticated:
        try:
            last_stat = MessageStatistics.query.order_by(
                MessageStatistics.updated_at.desc()
            ).first()
            last_message_time = last_stat.updated_at.isoformat() if last_stat else None
        except Exception:
            last_message_time = None
        
        result.update({
            'database': db_status,
            'bot': bot_status,
            'last_message_time': last_message_time
        })
    
    return jsonify(result)


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
    
    # Handle expiration_date - directly set from datetime picker
    # Note: datetime-local input returns browser local time, which we treat as Beijing time
    # since the UI is in Chinese and the database stores naive datetimes in Beijing time
    expiration_date_str = d.get('expiration_date')
    
    if expiration_date_str:
        try:
            # Parse datetime-local format: YYYY-MM-DDTHH:MM (treated as Beijing time)
            new_expiration = datetime.strptime(expiration_date_str, '%Y-%m-%dT%H:%M')
            u.expiration_date = new_expiration
            
            # If expiration is extended to future and user was banned, unban them
            now = get_beijing_now()
            if new_expiration > now and u.is_banned:
                u.is_banned = False
                unban_user_in_group(gid, u.tg_id)
        except ValueError as e:
            return jsonify({'status':'error','msg':f'日期格式错误: {e}'})
    else:
        # Clear expiration (set to permanent)
        u.expiration_date = None
        # If user was banned due to expiration, unban them when set to permanent
        if u.is_banned:
            u.is_banned = False
            unban_user_in_group(gid, u.tg_id)

    db.session.commit()
    return jsonify({'status':'ok'})

@core_bp.route('/api/delete_user', methods=['POST'])
def api_delete_user():
    if not session.get('logged_in'): return jsonify({'status':'error'})
    d = request.json
    if not d or 'id' not in d:
        return jsonify({'status':'error', 'msg':'Missing required parameter: id'})
    GroupUser.query.filter_by(id=d['id']).delete()
    db.session.commit()
    return jsonify({'status':'ok'})

@core_bp.route('/api/get_user_info')
def api_get_user_info():
    """获取用户详细信息"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'msg': 'Auth required'})
    
    group_id = safe_int(request.args.get('group_id'), 0)
    user_id = safe_int(request.args.get('user_id'), 0)
    
    if not group_id or not user_id:
        return jsonify({'status': 'error', 'msg': 'Missing parameters'})
    
    user = GroupUser.query.filter_by(id=user_id, group_id=group_id).first()
    if not user:
        return jsonify({'status': 'error', 'msg': 'User not found'})
    
    points = UserPoints.query.filter_by(group_id=group_id, user_id=user.tg_id).first()
    
    # Parse profile data
    try:
        profile_data = json.loads(user.profile_data) if user.profile_data else {}
    except:
        profile_data = {}
    
    return jsonify({
        'status': 'ok',
        'user': {
            'tg_id': user.tg_id,
            'username': profile_data.get('username'),
            'first_name': profile_data.get('first_name') or profile_data.get('name'),
            'last_name': profile_data.get('last_name'),
            'is_banned': user.is_banned,
            'online': user.online,
            'points': points.points_balance if points else 0,
            'created_at': user.created_at.isoformat() if user.created_at else None,
            'last_activity': user.last_activity.isoformat() if user.last_activity else None,
            'expiration_date': user.expiration_date.isoformat() if user.expiration_date else None,
            'checkin_time': user.checkin_time.isoformat() if user.checkin_time else None,
            'profile_data': profile_data
        }
    })

@core_bp.route('/api/bulk_import_users', methods=['POST'])
def api_bulk_import_users():
    """Bulk import users from XLSX data"""
    if not session.get('logged_in'): return jsonify({'status':'error', 'msg': 'Not logged in'})
    
    d = request.json
    group_id = d.get('group_id')
    
    # Check if it's XLSX import (base64 encoded) or old text format
    xlsx_content = d.get('xlsx_content')
    users_data = d.get('users', [])
    add_days = safe_int(d.get('add_days', 0), 0)
    
    # Validate add_days parameter (reasonable bounds: 0-3650 days / 10 years)
    if add_days < 0 or add_days > 3650:
        return jsonify({'status':'error', 'msg':'有效天数必须在 0-3650 之间'})
    
    if not group_id:
        return jsonify({'status':'error', 'msg':'缺少必要参数'})
    
    group = BotGroup.query.get(group_id)
    if not group:
        return jsonify({'status':'error', 'msg':'群组不存在'})
    
    fields = get_group_fields(group)
    
    # Parse XLSX if provided
    if xlsx_content:
        try:
            import base64
            xlsx_bytes = base64.b64decode(xlsx_content)
            wb = load_workbook(BytesIO(xlsx_bytes))
            ws = wb.active
            
            users_data = []
            # Skip header row
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[0]:
                    continue
                
                tg_id = str(row[0]).strip()
                if not tg_id or not tg_id.isdigit():
                    continue
                
                profile = {}
                for idx, field in enumerate(fields):
                    col_idx = idx + 1
                    if col_idx < len(row):
                        value = row[col_idx]
                        profile[field['key']] = str(value) if value else ''
                
                users_data.append({
                    'tg_id': tg_id,
                    'profile': profile
                })
        except Exception as e:
            return jsonify({'status':'error', 'msg': f'解析 XLSX 文件失败: {str(e)}'})
    
    if not users_data:
        return jsonify({'status':'error', 'msg':'没有有效的用户数据'})
    
    success_count = 0
    skipped_count = 0
    
    try:
        for user_data in users_data:
            tg_id = user_data.get('tg_id')
            profile = user_data.get('profile', {})
            
            if not tg_id:
                continue
            
            # Check if user already exists
            existing_user = GroupUser.query.filter_by(group_id=group_id, tg_id=tg_id).first()
            if existing_user:
                skipped_count += 1
                continue
            
            # Create new user
            expiration_date = None
            if add_days > 0:
                expiration_date = get_beijing_now() + timedelta(days=add_days)
            
            new_user = GroupUser(
                group_id=group_id,
                tg_id=tg_id,
                profile_data=json.dumps(profile, ensure_ascii=False),
                expiration_date=expiration_date,
                is_banned=False,
                online=False
            )
            db.session.add(new_user)
            success_count += 1
        
        db.session.commit()
        
        return jsonify({
            'status': 'ok',
            'success_count': success_count,
            'skipped_count': skipped_count
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error in bulk_import_users: {e}")
        return jsonify({'status':'error', 'msg': str(e)})

        return jsonify({'status':'error', 'msg': f'导入失败: {str(e)}'})

@core_bp.route('/api/export_users', methods=['POST'])
def api_export_users():
    """Export users to XLSX format"""
    if not session.get('logged_in'): return jsonify({'status':'error', 'msg': 'Not logged in'})
    
    d = request.json
    group_id = d.get('group_id')
    
    if not group_id:
        return jsonify({'status':'error', 'msg':'缺少必要参数'})
    
    group = BotGroup.query.get(group_id)
    if not group:
        return jsonify({'status':'error', 'msg':'群组不存在'})
    
    fields = get_group_fields(group)
    users = GroupUser.query.filter_by(group_id=group_id).order_by(GroupUser.id.desc()).all()
    
    # Create XLSX workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "认证用户"
    
    # Style for header
    header_fill = PatternFill(start_color="667EEA", end_color="667EEA", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    header_alignment = Alignment(horizontal="center", vertical="center")
    
    # Header row
    header = ['TG_ID']
    for field in fields:
        header.append(field['label'])
    header.extend(['状态', '过期时间', '禁言'])
    
    for col_num, column_title in enumerate(header, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.value = column_title
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
    
    # Data rows
    for row_num, user in enumerate(users, 2):
        try:
            profile = json.loads(user.profile_data) if user.profile_data else {}
        except:
            profile = {}
        
        ws.cell(row=row_num, column=1, value=str(user.tg_id))
        
        for col_num, field in enumerate(fields, 2):
            value = profile.get(field['key'], '')
            ws.cell(row=row_num, column=col_num, value=str(value))
        
        # Status
        col_num = len(fields) + 2
        if user.is_banned:
            status = '封禁'
        elif user.expiration_date:
            status = '正常'
        else:
            status = '永久'
        ws.cell(row=row_num, column=col_num, value=status)
        
        # Expiration date
        exp_date = user.expiration_date.strftime('%Y-%m-%d %H:%M:%S') if user.expiration_date else ''
        ws.cell(row=row_num, column=col_num + 1, value=exp_date)
        
        # Banned
        ws.cell(row=row_num, column=col_num + 2, value='是' if user.is_banned else '否')
    
    # Auto-adjust column widths
    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[column_letter].width = adjusted_width
    
    # Save to BytesIO
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    
    # Convert to base64 for JSON response
    import base64
    xlsx_base64 = base64.b64encode(output.read()).decode('utf-8')
    
    return jsonify({
        'status': 'ok',
        'xlsx_content': xlsx_base64,
        'filename': f'users_{group_id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    })


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
        d = request.json
        if not d or 'id' not in d:
            return jsonify({'status':'error', 'msg':'Missing required parameter: id'})
        user = GroupUser.query.get(d['id'])
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
    
    # 检查是否包含 /start 关键词 - /start 应使用专门的 /start 消息功能
    # 只阻止精确的 /start 命令，不影响其他包含 start 的关键词
    keywords = [k.strip() for k in trigger_keyword.split(',') if k.strip()]
    if '/start' in keywords:
        return jsonify({'status':'error','msg':'/start 命令请使用「/start 消息」功能配置，不能在自动回复中设置'})
    
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

@core_bp.route('/api/export_auto_replies/<int:group_id>', methods=['GET'])
def api_export_auto_replies(group_id):
    """导出自动回复规则为XLSX"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    
    try:
        auto_replies = AutoReply.query.filter_by(group_id=group_id).all()
        
        # Create XLSX workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "自动回复"
        
        # Style for header
        header_fill = PatternFill(start_color="667EEA", end_color="667EEA", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # Header row
        headers = ['触发关键词', '消息类型', '多媒体链接', '消息内容', '链接按钮', '自动删除(秒)', '备注', '状态']
        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num)
            cell.value = header
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
        
        # Data rows
        for row_num, ar in enumerate(auto_replies, 2):
            ws.cell(row=row_num, column=1, value=ar.trigger_keyword)
            ws.cell(row=row_num, column=2, value=ar.media_type or 'text')
            ws.cell(row=row_num, column=3, value=ar.media_url or '')
            ws.cell(row=row_num, column=4, value=ar.content or '')
            ws.cell(row=row_num, column=5, value=ar.links or '[]')
            ws.cell(row=row_num, column=6, value=ar.delete_after or 0)
            ws.cell(row=row_num, column=7, value=ar.remark or '')
            ws.cell(row=row_num, column=8, value='启用' if ar.is_active else '暂停')
        
        # Auto-adjust column widths
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[column_letter].width = adjusted_width
        
        # Save to BytesIO
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        
        # Convert to base64 for JSON response
        import base64
        xlsx_base64 = base64.b64encode(output.read()).decode('utf-8')
        
        return jsonify({
            'status': 'ok',
            'xlsx_content': xlsx_base64,
            'filename': f'auto_replies_{group_id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx',
            'count': len(auto_replies)
        })
    except Exception as e:
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/import_auto_replies', methods=['POST'])
def api_import_auto_replies():
    """导入自动回复规则从XLSX"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    
    try:
        d = request.json
        if not d or 'group_id' not in d:
            return jsonify({'status':'error','msg':'Missing required fields'})
        
        group_id = d['group_id']
        xlsx_content = d.get('xlsx_content')
        
        # Verify group exists
        group = BotGroup.query.get(group_id)
        if not group:
            return jsonify({'status':'error','msg':'Group not found'})
        
        if not xlsx_content:
            return jsonify({'status':'error','msg':'No data provided'})
        
        # Parse XLSX
        import base64
        xlsx_bytes = base64.b64decode(xlsx_content)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb.active
        
        imported_count = 0
        skipped_count = 0
        
        # Skip header row
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[0]:
                continue
            
            try:
                # Create new auto reply
                item = AutoReply(group_id=group_id)
                item.trigger_keyword = row[0] if row[0] else ''
                item.media_type = row[1] if len(row) > 1 and row[1] else 'text'
                item.media_url = row[2] if len(row) > 2 else None
                item.content = row[3] if len(row) > 3 else None
                item.links = row[4] if len(row) > 4 and row[4] else '[]'
                item.delete_after = int(row[5]) if len(row) > 5 and row[5] else 0
                item.remark = row[6] if len(row) > 6 else None
                item.is_active = (row[7] == '启用') if len(row) > 7 and row[7] else True
                
                db.session.add(item)
                imported_count += 1
            except Exception as e:
                logging.error(f"Error importing auto reply: {e}")
                skipped_count += 1
                continue
        
        db.session.commit()
        return jsonify({
            'status':'ok',
            'imported': imported_count,
            'skipped': skipped_count
        })
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

@core_bp.route('/api/export_scheduled_messages/<int:group_id>', methods=['GET'])
def api_export_scheduled_messages(group_id):
    """导出定时消息为XLSX"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    
    try:
        scheduled_messages = ScheduledMessage.query.filter_by(group_id=group_id).all()
        
        # Create XLSX workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "定时消息"
        
        # Style for header
        header_fill = PatternFill(start_color="F093FB", end_color="F093FB", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        # Header row
        headers = ['消息类型', '多媒体链接', '消息内容', '链接按钮', '间隔(分钟)', '删除上一条', '开始时间', '停止时间', '备注', '状态']
        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num)
            cell.value = header
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
        
        # Data rows
        for row_num, sm in enumerate(scheduled_messages, 2):
            ws.cell(row=row_num, column=1, value=sm.media_type or 'text')
            ws.cell(row=row_num, column=2, value=sm.media_url or '')
            ws.cell(row=row_num, column=3, value=sm.content or '')
            ws.cell(row=row_num, column=4, value=sm.links or '[]')
            ws.cell(row=row_num, column=5, value=sm.repeat_interval or 0)
            ws.cell(row=row_num, column=6, value='是' if sm.delete_previous else '否')
            ws.cell(row=row_num, column=7, value=sm.start_time.strftime('%Y-%m-%d %H:%M:%S') if sm.start_time else '')
            ws.cell(row=row_num, column=8, value=sm.stop_time.strftime('%Y-%m-%d %H:%M:%S') if sm.stop_time else '')
            ws.cell(row=row_num, column=9, value=sm.remark or '')
            ws.cell(row=row_num, column=10, value='启用' if sm.is_active else '暂停')
        
        # Auto-adjust column widths
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[column_letter].width = adjusted_width
        
        # Save to BytesIO
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        
        # Convert to base64 for JSON response
        import base64
        xlsx_base64 = base64.b64encode(output.read()).decode('utf-8')
        
        return jsonify({
            'status': 'ok',
            'xlsx_content': xlsx_base64,
            'filename': f'scheduled_messages_{group_id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx',
            'count': len(scheduled_messages)
        })
    except Exception as e:
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/import_scheduled_messages', methods=['POST'])
def api_import_scheduled_messages():
    """导入定时消息从XLSX"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    
    try:
        d = request.json
        if not d or 'group_id' not in d:
            return jsonify({'status':'error','msg':'Missing required fields'})
        
        group_id = d['group_id']
        xlsx_content = d.get('xlsx_content')
        
        # Verify group exists
        group = BotGroup.query.get(group_id)
        if not group:
            return jsonify({'status':'error','msg':'Group not found'})
        
        if not xlsx_content:
            return jsonify({'status':'error','msg':'No data provided'})
        
        # Parse XLSX
        import base64
        xlsx_bytes = base64.b64decode(xlsx_content)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb.active
        
        imported_count = 0
        skipped_count = 0
        
        # Skip header row
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row:
                continue
            
            try:
                # Create new scheduled message
                item = ScheduledMessage(group_id=group_id)
                item.media_type = row[0] if row[0] else 'text'
                item.media_url = row[1] if len(row) > 1 else None
                item.content = row[2] if len(row) > 2 else None
                item.links = row[3] if len(row) > 3 and row[3] else '[]'
                
                # Handle repeat_interval - only convert to int if it's a valid number
                item.repeat_interval = 0  # Default value
                if len(row) > 4 and row[4]:
                    try:
                        item.repeat_interval = int(row[4])
                    except (ValueError, TypeError):
                        pass  # Keep default value of 0
                    
                item.delete_previous = (row[5] == '是') if len(row) > 5 and row[5] else False
                
                # Parse dates
                if len(row) > 6 and row[6]:
                    try:
                        if isinstance(row[6], str):
                            item.start_time = datetime.strptime(row[6], '%Y-%m-%d %H:%M:%S')
                        else:
                            item.start_time = row[6]
                    except:
                        item.start_time = None
                
                if len(row) > 7 and row[7]:
                    try:
                        if isinstance(row[7], str):
                            item.stop_time = datetime.strptime(row[7], '%Y-%m-%d %H:%M:%S')
                        else:
                            item.stop_time = row[7]
                    except:
                        item.stop_time = None
                
                item.remark = row[8] if len(row) > 8 else None
                item.is_active = (row[9] == '启用') if len(row) > 9 and row[9] else True
                
                db.session.add(item)
                imported_count += 1
            except Exception as e:
                logging.error(f"Error importing scheduled message: {e}")
                skipped_count += 1
                continue
        
        db.session.commit()
        return jsonify({
            'status':'ok',
            'imported': imported_count,
            'skipped': skipped_count
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

# --- Start Message API Routes ---
@core_bp.route('/api/save_start_message', methods=['POST'])
def api_save_start_message():
    """保存自定义 /start 消息"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d: return jsonify({'status':'error','msg':'Missing request body'})
    
    group_id = d.get('group_id')
    if not group_id: return jsonify({'status':'error','msg':'Missing group_id'})
    
    message_type = d.get('message_type', 'user')
    if message_type not in ['user', 'admin']: return jsonify({'status':'error','msg':'Invalid message_type'})
    
    try:
        item_id = d.get('id')
        if item_id:
            # 编辑现有消息
            item = StartMessage.query.get(item_id)
            if not item: return jsonify({'status':'error','msg':'Message not found'})
            if item.group_id != group_id: return jsonify({'status':'error','msg':'Permission denied'})
        else:
            # 新增消息
            item = StartMessage(group_id=group_id)
            db.session.add(item)
        
        item.message_type = message_type
        item.media_type = d.get('media_type', 'text')
        item.media_url = d.get('media_url', '').strip() or None
        item.content = d.get('content', '').strip() or None
        item.links = d.get('links', '[]')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/toggle_start_message', methods=['POST'])
def api_toggle_start_message():
    """切换自定义 /start 消息状态"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        item = StartMessage.query.get(d['id'])
        if not item: return jsonify({'status':'error','msg':'Message not found'})
        item.is_active = not item.is_active
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_start_message', methods=['POST'])
def api_delete_start_message():
    """删除自定义 /start 消息"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        item = StartMessage.query.get(d['id'])
        if not item: return jsonify({'status':'error','msg':'Message not found'})
        db.session.delete(item)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_entry_exit_settings', methods=['POST'])
def api_save_entry_exit_settings():
    """保存进退群设置"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        settings = GroupEntryExitSettings.query.filter_by(group_id=d['group_id']).first()
        if not settings:
            settings = GroupEntryExitSettings(group_id=d['group_id'])
            db.session.add(settings)
        
        settings.entry_verification_enabled = d.get('entry_verification_enabled', False)
        settings.verification_question = d.get('verification_question')
        settings.verification_answer = d.get('verification_answer')
        settings.verification_timeout = d.get('verification_timeout', 60)
        settings.welcome_enabled = d.get('welcome_enabled', False)
        settings.welcome_message = d.get('welcome_message')
        settings.welcome_media_type = d.get('welcome_media_type', 'text')
        settings.welcome_media_url = d.get('welcome_media_url')
        settings.exit_ban_enabled = d.get('exit_ban_enabled', False)
        settings.exit_ban_duration = d.get('exit_ban_duration', 0)
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_spam_protection', methods=['POST'])
def api_save_spam_protection():
    """保存垃圾防护设置"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        settings = SpamProtection.query.filter_by(group_id=d['group_id']).first()
        if not settings:
            settings = SpamProtection(group_id=d['group_id'])
            db.session.add(settings)
        
        settings.enabled = d.get('enabled', False)
        settings.max_messages_per_minute = d.get('max_messages_per_minute', 10)
        settings.block_links = d.get('block_links', False)
        settings.block_forwards = d.get('block_forwards', False)
        settings.block_stickers = d.get('block_stickers', False)
        settings.punishment_type = d.get('punishment_type', 'mute')
        settings.punishment_duration = d.get('punishment_duration', 60)
        settings.whitelist_users = json.dumps(d.get('whitelist_users', []))
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_timed_group_control', methods=['POST'])
def api_save_timed_group_control():
    """保存定时开关群设置"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        settings = TimedGroupControl.query.filter_by(group_id=d['group_id']).first()
        if not settings:
            settings = TimedGroupControl(group_id=d['group_id'])
            db.session.add(settings)
        
        settings.enabled = d.get('enabled', False)
        # Parse time strings to time objects
        if d.get('open_time'):
            settings.open_time = datetime.strptime(d['open_time'], '%H:%M').time()
        if d.get('close_time'):
            settings.close_time = datetime.strptime(d['close_time'], '%H:%M').time()
        settings.timezone = d.get('timezone', 'Asia/Shanghai')
        settings.close_message = d.get('close_message')
        settings.open_message = d.get('open_message')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_other_settings', methods=['POST'])
def api_save_other_settings():
    """保存其他设置"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        settings = OtherSettings.query.filter_by(group_id=d['group_id']).first()
        if not settings:
            settings = OtherSettings(group_id=d['group_id'])
            db.session.add(settings)
        
        settings.auto_delete_join_msg = d.get('auto_delete_join_msg', False)
        settings.auto_delete_leave_msg = d.get('auto_delete_leave_msg', False)
        settings.auto_delete_promote_msg = d.get('auto_delete_promote_msg', False)
        settings.auto_delete_pin_msg = d.get('auto_delete_pin_msg', False)
        settings.cancel_channel_pin = d.get('cancel_channel_pin', False)
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_invitation_activity', methods=['POST'])
def api_save_invitation_activity():
    """保存邀请活动设置"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        settings = InvitationActivity.query.filter_by(group_id=d['group_id']).first()
        if not settings:
            settings = InvitationActivity(group_id=d['group_id'])
            db.session.add(settings)
        
        settings.enabled = d.get('enabled', False)
        settings.reward_points = d.get('reward_points', 10)
        settings.minimum_invites = d.get('minimum_invites', 1)
        if d.get('activity_start'):
            try:
                # Handle datetime-local input format (YYYY-MM-DDTHH:MM)
                activity_start = d['activity_start']
                if 'Z' in activity_start:
                    activity_start = activity_start.replace('Z', '+00:00')
                settings.activity_start = datetime.fromisoformat(activity_start)
            except (ValueError, TypeError):
                pass
        if d.get('activity_end'):
            try:
                activity_end = d['activity_end']
                if 'Z' in activity_end:
                    activity_end = activity_end.replace('Z', '+00:00')
                settings.activity_end = datetime.fromisoformat(activity_end)
            except (ValueError, TypeError):
                pass
        settings.description = d.get('description')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_forced_channel_subscription', methods=['POST'])
def api_save_forced_channel_subscription():
    """保存强制订阅频道设置"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        settings = ForcedChannelSubscription.query.filter_by(group_id=d['group_id']).first()
        if not settings:
            settings = ForcedChannelSubscription(group_id=d['group_id'])
            db.session.add(settings)
        
        settings.enabled = d.get('enabled', False)
        settings.channel_id = d.get('channel_id')
        settings.channel_username = d.get('channel_username')
        settings.check_interval = d.get('check_interval', 3600)
        settings.unsubscribe_action = d.get('unsubscribe_action', 'kick')
        settings.verification_message = d.get('verification_message')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_points_rule', methods=['POST'])
def api_save_points_rule():
    """保存积分规则"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        if d.get('id'):
            rule = PointsRule.query.get(d['id'])
            if not rule: return jsonify({'status':'error','msg':'Rule not found'})
        else:
            rule = PointsRule(group_id=d['group_id'])
            db.session.add(rule)
        
        rule.rule_name = d.get('rule_name', '')
        rule.rule_type = d.get('rule_type', 'message')
        rule.points_amount = d.get('points_amount', 1)
        rule.is_active = d.get('is_active', True)
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_points_rule', methods=['POST'])
def api_delete_points_rule():
    """删除积分规则"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        rule = PointsRule.query.get(d['id'])
        if not rule: return jsonify({'status':'error','msg':'Rule not found'})
        db.session.delete(rule)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_points_auto_reply', methods=['POST'])
def api_save_points_auto_reply():
    """保存积分自动回复"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        if d.get('id'):
            reply = PointsAutoReply.query.get(d['id'])
            if not reply: return jsonify({'status':'error','msg':'Reply not found'})
        else:
            reply = PointsAutoReply(group_id=d['group_id'])
            db.session.add(reply)
        
        reply.trigger_keyword = d.get('trigger_keyword', '')
        reply.points_cost = d.get('points_cost', 0)
        reply.content = d.get('content')
        reply.media_type = d.get('media_type', 'text')
        reply.media_url = d.get('media_url')
        reply.is_active = d.get('is_active', True)
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_points_auto_reply', methods=['POST'])
def api_delete_points_auto_reply():
    """删除积分自动回复"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        reply = PointsAutoReply.query.get(d['id'])
        if not reply: return jsonify({'status':'error','msg':'Reply not found'})
        db.session.delete(reply)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_points_auction', methods=['POST'])
def api_save_points_auction():
    """保存积分竞拍"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        if d.get('id'):
            auction = PointsAuction.query.get(d['id'])
            if not auction: return jsonify({'status':'error','msg':'Auction not found'})
        else:
            auction = PointsAuction(group_id=d['group_id'])
            db.session.add(auction)
        
        auction.item_name = d.get('item_name', '')
        auction.item_description = d.get('item_description')
        auction.starting_price = d.get('starting_price', 100)
        if d.get('auction_start'):
            try:
                auction_start = d['auction_start']
                if 'Z' in auction_start:
                    auction_start = auction_start.replace('Z', '+00:00')
                auction.auction_start = datetime.fromisoformat(auction_start)
            except (ValueError, TypeError):
                pass
        if d.get('auction_end'):
            try:
                auction_end = d['auction_end']
                if 'Z' in auction_end:
                    auction_end = auction_end.replace('Z', '+00:00')
                auction.auction_end = datetime.fromisoformat(auction_end)
            except (ValueError, TypeError):
                pass
        auction.status = d.get('status', 'pending')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_points_auction', methods=['POST'])
def api_delete_points_auction():
    """删除积分竞拍"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        auction = PointsAuction.query.get(d['id'])
        if not auction: return jsonify({'status':'error','msg':'Auction not found'})
        db.session.delete(auction)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_group_lottery', methods=['POST'])
def api_save_group_lottery():
    """保存群抽奖"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        if d.get('id'):
            lottery = GroupLottery.query.get(d['id'])
            if not lottery: return jsonify({'status':'error','msg':'Lottery not found'})
        else:
            lottery = GroupLottery(group_id=d['group_id'])
            db.session.add(lottery)
        
        lottery.lottery_name = d.get('lottery_name', '')
        lottery.lottery_type = d.get('lottery_type', 'message_count')
        lottery.prize_description = d.get('prize_description')
        lottery.min_messages = d.get('min_messages', 10)
        lottery.top_n_winners = d.get('top_n_winners', 3)
        if d.get('start_time'):
            try:
                start_time = d['start_time']
                if 'Z' in start_time:
                    start_time = start_time.replace('Z', '+00:00')
                lottery.start_time = datetime.fromisoformat(start_time)
            except (ValueError, TypeError):
                pass
        if d.get('end_time'):
            try:
                end_time = d['end_time']
                if 'Z' in end_time:
                    end_time = end_time.replace('Z', '+00:00')
                lottery.end_time = datetime.fromisoformat(end_time)
            except (ValueError, TypeError):
                pass
        lottery.status = d.get('status', 'pending')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_group_lottery', methods=['POST'])
def api_delete_group_lottery():
    """删除群抽奖"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        lottery = GroupLottery.query.get(d['id'])
        if not lottery: return jsonify({'status':'error','msg':'Lottery not found'})
        db.session.delete(lottery)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_member_level', methods=['POST'])
def api_save_member_level():
    """保存成员等级"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        if d.get('id'):
            level = MemberLevel.query.get(d['id'])
            if not level: return jsonify({'status':'error','msg':'Level not found'})
        else:
            level = MemberLevel(group_id=d['group_id'])
            db.session.add(level)
        
        level.level_name = d.get('level_name', '')
        level.required_points = d.get('required_points', 0)
        level.permissions = d.get('permissions', '{}')
        level.badge_emoji = d.get('badge_emoji')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_member_level', methods=['POST'])
def api_delete_member_level():
    """删除成员等级"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        level = MemberLevel.query.get(d['id'])
        if not level: return jsonify({'status':'error','msg':'Level not found'})
        db.session.delete(level)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_group_bottom_button', methods=['POST'])
def api_save_group_bottom_button():
    """保存群底部按钮"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        if d.get('id'):
            button = GroupBottomButton.query.get(d['id'])
            if not button: return jsonify({'status':'error','msg':'Button not found'})
        else:
            button = GroupBottomButton(group_id=d['group_id'])
            db.session.add(button)
        
        # Active UI fields
        button.button_text = d.get('button_text', '')
        button.input_field_placeholder = d.get('input_field_placeholder', '').strip() or None
        button.button_order = d.get('button_order', 0)
        button.row_position = d.get('row_position', 0)
        button.is_active = d.get('is_active', True)
        
        # Keep these fields for backward compatibility but don't expose in UI
        button.button_url = d.get('button_url')
        button.button_callback = d.get('button_callback')
        button.trigger_keyword = d.get('trigger_keyword', '').strip() or None
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_group_bottom_button', methods=['POST'])
def api_delete_group_bottom_button():
    """删除群底部按钮"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        button = GroupBottomButton.query.get(d['id'])
        if not button: return jsonify({'status':'error','msg':'Button not found'})
        db.session.delete(button)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/move_group_bottom_button', methods=['POST'])
def api_move_group_bottom_button():
    """移动群底部按钮顺序"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d or 'direction' not in d: return jsonify({'status':'error','msg':'Missing parameters'})
    
    try:
        button = GroupBottomButton.query.get(d['id'])
        if not button: return jsonify({'status':'error','msg':'Button not found'})
        
        direction = d['direction']
        current_order = button.button_order
        
        # Get all buttons for the same group, ordered by button_order
        all_buttons = GroupBottomButton.query.filter_by(group_id=button.group_id).order_by(GroupBottomButton.button_order).all()
        
        # Find current position
        current_index = next((i for i, b in enumerate(all_buttons) if b.id == button.id), None)
        if current_index is None:
            return jsonify({'status':'error','msg':'Button position not found'})
        
        # Determine swap target
        if direction == 'up' and current_index > 0:
            swap_button = all_buttons[current_index - 1]
        elif direction == 'down' and current_index < len(all_buttons) - 1:
            swap_button = all_buttons[current_index + 1]
        else:
            return jsonify({'status':'error','msg':'Cannot move in that direction'})
        
        # Swap orders
        button.button_order, swap_button.button_order = swap_button.button_order, button.button_order
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/push_group_bottom_buttons', methods=['POST'])
def api_push_group_bottom_buttons():
    """推送群底部按钮到群组 - 直接显示菜单键盘给所有群成员"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    try:
        group = BotGroup.query.get(d['group_id'])
        if not group: return jsonify({'status':'error','msg':'Group not found'})
        
        # Get active buttons ordered by row_position, then button_order
        buttons = GroupBottomButton.query.filter_by(
            group_id=group.id,
            is_active=True
        ).order_by(GroupBottomButton.row_position, GroupBottomButton.button_order).all()
        
        if not buttons:
            return jsonify({'status':'error','msg':'没有可推送的按钮'})
        
        # Check if bot is ready
        if not global_ptb_app or not global_bot_loop:
            print(f"❌ Bot未就绪: global_ptb_app={bool(global_ptb_app)}, global_bot_loop={bool(global_bot_loop)}", flush=True)
            return jsonify({'status':'error','msg':'Bot未就绪，请稍后再试'})
        
        print(f"🔄 推送菜单键盘到群组 {group.chat_id}，共 {len(buttons)} 个按钮", flush=True)
        
        # Build the reply keyboard markup from buttons
        keyboard = []
        current_row = []
        current_row_num = buttons[0].row_position
        
        # Get the input field placeholder from the first button that has one
        input_placeholder = None
        for button in buttons:
            if button.input_field_placeholder:
                input_placeholder = button.input_field_placeholder
                break
        
        for button in buttons:
            # Start a new row if row_position changes
            if button.row_position != current_row_num:
                if current_row:
                    keyboard.append(current_row)
                current_row = []
                current_row_num = button.row_position
            
            # Reply keyboard buttons don't support URLs, just text
            current_row.append(KeyboardButton(button.button_text))
        
        # Add the last row
        if current_row:
            keyboard.append(current_row)
        
        reply_markup = ReplyKeyboardMarkup(
            keyboard, 
            resize_keyboard=True,
            one_time_keyboard=False,
            input_field_placeholder=input_placeholder
        ) if keyboard else None
        
        # Send message with menu keyboard to the group
        async def _send_menu_command():
            try:
                # Send a message with the menu keyboard attached
                msg = await global_ptb_app.bot.send_message(
                    chat_id=group.chat_id,
                    text="📋 群组菜单已更新！\n\n👇 请使用下方按钮菜单：",
                    reply_markup=reply_markup
                )
                print(f"✅ 菜单键盘推送成功，消息ID: {msg.message_id}", flush=True)
                return True
            except Exception as e:
                import traceback
                print(f"❌ 推送菜单键盘时发生错误: {e}", flush=True)
                print(''.join(traceback.format_exception(type(e), e, e.__traceback__)), flush=True)
                return False
        
        # Run async function in bot's event loop
        try:
            future = asyncio.run_coroutine_threadsafe(_send_menu_command(), global_bot_loop)
            success = future.result(timeout=10)
            
            if success:
                return jsonify({'status':'ok','msg':'菜单已成功推送到群组，群成员现在可以看到底部按钮菜单'})
            else:
                return jsonify({'status':'error','msg':'推送失败，请检查bot权限和群组ID是否正确'})
        except asyncio.TimeoutError:
            print("❌ 推送菜单超时", flush=True)
            return jsonify({'status':'error','msg':'推送超时，请稍后再试'})
        except Exception as e:
            import traceback
            print(f"❌ 异步调用失败: {e}", flush=True)
            print(''.join(traceback.format_exception(type(e), e, e.__traceback__)), flush=True)
            return jsonify({'status':'error','msg':f'推送失败: {str(e)}'})
            
    except Exception as e:
        import traceback
        print(f"❌ API调用失败: {e}", flush=True)
        print(''.join(traceback.format_exception(type(e), e, e.__traceback__)), flush=True)
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/save_sync_group_messages', methods=['POST'])
def api_save_sync_group_messages():
    """保存同步群消息设置"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'group_id' not in d: return jsonify({'status':'error','msg':'Missing group_id'})
    
    # Validate target_group_id is provided
    target_group_id = d.get('target_group_id', '').strip()
    if not target_group_id:
        return jsonify({'status':'error','msg':'目标群组ID不能为空'})
    
    try:
        settings = SyncGroupMessages.query.filter_by(source_group_id=d['group_id']).first()
        if not settings:
            # Explicitly set all fields when creating new record to ensure consistent behavior
            settings = SyncGroupMessages(
                source_group_id=d['group_id'],
                target_group_id=target_group_id,
                enabled=d.get('enabled', False),
                sync_media=d.get('sync_media', True),
                sync_forwards=d.get('sync_forwards', True),
                filter_keywords=d.get('filter_keywords', '[]')
            )
            db.session.add(settings)
        else:
            settings.target_group_id = target_group_id
            settings.enabled = d.get('enabled', False)
            settings.sync_media = d.get('sync_media', True)
            settings.sync_forwards = d.get('sync_forwards', True)
            settings.filter_keywords = d.get('filter_keywords', '[]')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_bot_clone', methods=['POST'])
def api_save_bot_clone():
    """保存机器人克隆"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d: return jsonify({'status':'error','msg':'Missing request body'})
    
    clone_name = d.get('clone_name', '').strip()
    bot_token = d.get('bot_token', '').strip()
    
    if not clone_name or not bot_token:
        return jsonify({'status':'error','msg':'克隆名称和Bot Token不能为空'})
    
    try:
        clone_id = d.get('id')
        if clone_id:
            # Edit existing clone
            clone = BotClone.query.get(clone_id)
            if not clone: return jsonify({'status':'error','msg':'Clone not found'})
        else:
            # Create new clone
            clone = BotClone()
            db.session.add(clone)
        
        clone.clone_name = clone_name
        clone.bot_token = bot_token
        clone.is_active = d.get('is_active', True)
        clone.description = d.get('description', '').strip() or None
        clone.webhook_url = d.get('webhook_url', '').strip() or None
        
        # Handle owner_user_id
        owner_user_id = d.get('owner_user_id', '').strip()
        if owner_user_id:
            try:
                clone.owner_user_id = int(owner_user_id)
            except (ValueError, TypeError):
                clone.owner_user_id = None
        else:
            clone.owner_user_id = None
        
        # Handle admin_user_ids - convert comma-separated string to JSON array
        admin_user_ids_str = d.get('admin_user_ids', '').strip()
        if admin_user_ids_str:
            try:
                # Split by comma and convert to integers (supports negative IDs)
                admin_ids = []
                for uid in admin_user_ids_str.split(','):
                    uid = uid.strip()
                    if uid:  # Skip empty strings
                        try:
                            admin_ids.append(int(uid))
                        except ValueError:
                            pass  # Skip invalid values
                clone.admin_user_ids = json.dumps(admin_ids)
            except (ValueError, TypeError):
                clone.admin_user_ids = '[]'
        else:
            clone.admin_user_ids = '[]'
        
        # Parse expiration date
        expiration_date_str = d.get('expiration_date')
        if expiration_date_str:
            try:
                # HTML datetime-local format: YYYY-MM-DDTHH:MM (no timezone)
                # Parse as Beijing time
                clone.expiration_date = datetime.strptime(expiration_date_str, '%Y-%m-%dT%H:%M')
            except (ValueError, TypeError):
                clone.expiration_date = None
        else:
            clone.expiration_date = None
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/delete_bot_clone', methods=['POST'])
def api_delete_bot_clone():
    """删除机器人克隆"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        clone = BotClone.query.get(d['id'])
        if not clone: return jsonify({'status':'error','msg':'Clone not found'})
        db.session.delete(clone)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})

@core_bp.route('/api/toggle_bot_clone', methods=['POST'])
def api_toggle_bot_clone():
    """切换机器人克隆状态"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        clone = BotClone.query.get(d['id'])
        if not clone: return jsonify({'status':'error','msg':'Clone not found'})
        clone.is_active = not clone.is_active
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_inactive_user_settings', methods=['POST'])
def api_save_inactive_user_settings():
    """保存不活跃用户设置"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d: return jsonify({'status':'error','msg':'Missing request body'})
    
    try:
        gid = safe_int(d.get('group_id'))
        group = BotGroup.query.get(gid)
        if not group: return jsonify({'status':'error','msg':'Group not found'})
        
        settings = InactiveUserSettings.query.filter_by(group_id=gid).first()
        if not settings:
            settings = InactiveUserSettings(group_id=gid)
            db.session.add(settings)
        
        settings.enabled = d.get('enabled', False)
        settings.inactivity_days = safe_int(d.get('inactivity_days', 30))
        settings.action_type = d.get('action_type', 'kick')
        settings.check_interval = safe_int(d.get('check_interval', 86400))
        settings.warning_enabled = d.get('warning_enabled', False)
        settings.warning_days = safe_int(d.get('warning_days', 7))
        settings.warning_message = d.get('warning_message', '')
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_keyword_filter', methods=['POST'])
def api_save_keyword_filter():
    """保存关键词过滤"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d: return jsonify({'status':'error','msg':'Missing request body'})
    
    try:
        gid = safe_int(d.get('group_id'))
        group = BotGroup.query.get(gid)
        if not group: return jsonify({'status':'error','msg':'Group not found'})
        
        filter_id = safe_int(d.get('id', 0))
        if filter_id:
            kf = KeywordFilter.query.get(filter_id)
            if not kf: return jsonify({'status':'error','msg':'Filter not found'})
        else:
            kf = KeywordFilter(group_id=gid)
            db.session.add(kf)
        
        kf.keyword = d.get('keyword', '')
        kf.filter_type = d.get('filter_type', 'blacklist')
        kf.match_type = d.get('match_type', 'contains')
        kf.action = d.get('action', 'delete')
        kf.is_active = d.get('is_active', True)
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/delete_keyword_filter', methods=['POST'])
def api_delete_keyword_filter():
    """删除关键词过滤"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        kf = KeywordFilter.query.get(d['id'])
        if not kf: return jsonify({'status':'error','msg':'Filter not found'})
        db.session.delete(kf)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_group_vote', methods=['POST'])
def api_save_group_vote():
    """保存群投票"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d: return jsonify({'status':'error','msg':'Missing request body'})
    
    try:
        gid = safe_int(d.get('group_id'))
        group = BotGroup.query.get(gid)
        if not group: return jsonify({'status':'error','msg':'Group not found'})
        
        vote_id = safe_int(d.get('id', 0))
        if vote_id:
            vote = GroupVote.query.get(vote_id)
            if not vote: return jsonify({'status':'error','msg':'Vote not found'})
        else:
            vote = GroupVote(group_id=gid)
            db.session.add(vote)
        
        vote.title = d.get('title', '')
        vote.description = d.get('description', '')
        vote.options = json.dumps(d.get('options', []), ensure_ascii=False)
        vote.vote_type = d.get('vote_type', 'single')
        vote.max_choices = safe_int(d.get('max_choices', 1))
        vote.is_anonymous = d.get('is_anonymous', False)
        vote.allow_revote = d.get('allow_revote', True)
        
        # Parse dates
        if d.get('start_time'):
            try:
                vote.start_time = datetime.fromisoformat(d['start_time'].replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                pass
        if d.get('end_time'):
            try:
                vote.end_time = datetime.fromisoformat(d['end_time'].replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                pass
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/delete_group_vote', methods=['POST'])
def api_delete_group_vote():
    """删除群投票"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        vote = GroupVote.query.get(d['id'])
        if not vote: return jsonify({'status':'error','msg':'Vote not found'})
        db.session.delete(vote)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/save_quiz_game', methods=['POST'])
def api_save_quiz_game():
    """保存问答游戏"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d: return jsonify({'status':'error','msg':'Missing request body'})
    
    try:
        gid = safe_int(d.get('group_id'))
        group = BotGroup.query.get(gid)
        if not group: return jsonify({'status':'error','msg':'Group not found'})
        
        quiz_id = safe_int(d.get('id', 0))
        if quiz_id:
            quiz = QuizGame.query.get(quiz_id)
            if not quiz: return jsonify({'status':'error','msg':'Quiz not found'})
        else:
            quiz = QuizGame(group_id=gid)
            db.session.add(quiz)
        
        quiz.question = d.get('question', '')
        quiz.answers = json.dumps(d.get('answers', []), ensure_ascii=False)
        quiz.correct_answer_index = safe_int(d.get('correct_answer_index', 0))
        quiz.explanation = d.get('explanation', '')
        quiz.points_reward = safe_int(d.get('points_reward', 10))
        quiz.time_limit = safe_int(d.get('time_limit', 60))
        quiz.difficulty = d.get('difficulty', 'medium')
        quiz.category = d.get('category', '')
        quiz.is_active = d.get('is_active', True)
        
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/api/delete_quiz_game', methods=['POST'])
def api_delete_quiz_game():
    """删除问答游戏"""
    if not session.get('logged_in'): return jsonify({'status':'error','msg':'Auth required'})
    d = request.json
    if not d or 'id' not in d: return jsonify({'status':'error','msg':'Missing id'})
    
    try:
        quiz = QuizGame.query.get(d['id'])
        if not quiz: return jsonify({'status':'error','msg':'Quiz not found'})
        db.session.delete(quiz)
        db.session.commit()
        return jsonify({'status':'ok'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status':'error','msg':str(e)})


@core_bp.route('/magic_login')
def magic_login():
    token = request.args.get('token')
    secret_key = os.getenv('SECRET_KEY')
    if not secret_key:
        print("❌ CRITICAL: SECRET_KEY environment variable is not set!")
        return "Configuration error: SECRET_KEY not set", 500
    try:
        data = jwt.decode(token, secret_key, algorithms=['HS256'])
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
        
        # Check if already verified - redirect directly if already logged in via cookie
        if auth_session.is_verified:
            session['logged_in'] = True
            session.permanent = True  # Make session persistent
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
            # Set session as logged in with persistent cookie
            session['logged_in'] = True
            session.permanent = True  # Make session persistent (uses PERMANENT_SESSION_LIFETIME)
            return jsonify({
                'status': 'verified',
                'redirect_url': '/core/select_group'
            })
        
        return jsonify({'status': 'pending'})
    else:
        return jsonify({'status': 'verified', 'redirect_url': '/core/select_group'})


@core_bp.route('/api/get_member_detail')
def api_get_member_detail():
    """获取成员详细信息（支持认证和未认证用户）"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'msg': 'Auth required'})
    
    group_id = safe_int(request.args.get('group_id'), 0)
    user_id = safe_int(request.args.get('user_id'), 0)
    
    if not group_id or not user_id:
        return jsonify({'status': 'error', 'msg': 'Missing parameters'})
    
    # 获取积分信息
    user_points = UserPoints.query.filter_by(group_id=group_id, user_id=user_id).first()
    
    # 获取认证用户信息（如果存在）
    verified_user = GroupUser.query.filter_by(group_id=group_id, tg_id=user_id).first()
    
    # 获取积分日志
    logs = PointsLog.query.filter_by(
        group_id=group_id, user_id=user_id
    ).order_by(PointsLog.created_at.desc()).limit(10).all()
    
    result = {
        'user_id': user_id,
        'points': user_points.points_balance if user_points else 0,
        'is_verified': verified_user is not None,
        'recent_logs': [{
            'points_change': log.points_change,
            'reason': log.reason,
            'created_at': log.created_at.isoformat() if log.created_at else None
        } for log in logs]
    }
    
    if verified_user:
        try:
            profile_data = json.loads(verified_user.profile_data) if verified_user.profile_data else {}
        except:
            profile_data = {}
        
        result.update({
            'name': profile_data.get('name') or profile_data.get('first_name'),
            'username': profile_data.get('username'),
            'expiration_date': verified_user.expiration_date.isoformat() if verified_user.expiration_date else None,
            'is_banned': verified_user.is_banned,
            'checkin_time': verified_user.checkin_time.isoformat() if verified_user.checkin_time else None
        })
    
    return jsonify({'status': 'ok', 'user': result})


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
                
                # Extract scalar values BEFORE leaving the app context to avoid detached instance errors
                # This is crucial because accessing attributes outside the session context can fail
                # Note: We extract data WITHOUT marking as banned yet - we'll update DB after API success
                users_data = []
                for user, group, ban_msg in users_to_ban:
                    users_data.append({
                        'user_tg_id': user.tg_id,
                        'group_chat_id': group.chat_id,
                        'group_id': group.id,
                        'group_user_id': user.id,  # Store user.id to update database later
                        'group_title': group.title,
                        'ban_msg': ban_msg
                    })
                print(f"📦 [定时任务] Extracted data for {len(users_data)} users to ban", flush=True)
                
                # Return the extracted data for async processing
                return users_data
                            
            except Exception as e:
                print(f"Error in check_expired_users sync part: {e}")
                db.session.rollback()
                return None
    
    # Run sync DB operations in executor
    users_to_ban = await asyncio.get_running_loop().run_in_executor(None, _sync_check)
    
    if not users_to_ban:
        return
    
    # Log the users_to_ban list to confirm we have users to process
    print(f"🚀 [定时任务] Starting async ban operations for {len(users_to_ban)} users", flush=True)
    for idx, user_data in enumerate(users_to_ban):
        print(f"   User {idx+1}: tg_id={user_data['user_tg_id']}, group={user_data['group_title']} (chat_id={user_data['group_chat_id']})", flush=True)
    
    # Helper function to mark user as banned in database after successful API call
    def _mark_user_banned_in_db(group_user_id):
        """Mark user as banned in database - called after successful API mute"""
        with global_flask_app.app_context():
            try:
                group_user = GroupUser.query.get(group_user_id)
                if group_user:
                    group_user.is_banned = True
                    db.session.commit()
                    return True
            except Exception as e:
                print(f"❌ [定时任务] Failed to mark user {group_user_id} as banned in DB: {e}", flush=True)
                db.session.rollback()
                return False
        return False
    
    # Now perform all async Telegram operations with rate limiting
    async def ban_user_async(user_data):
        """Mute an expired user and send notification"""
        user_tg_id = user_data['user_tg_id']
        group_chat_id = user_data['group_chat_id']
        group_id = user_data['group_id']
        group_user_id = user_data['group_user_id']
        group_title = user_data['group_title']
        ban_msg = user_data['ban_msg']
        
        print(f"🔧 [定时任务] ban_user_async CALLED for user {user_tg_id} in group {group_title}", flush=True)
        try:
            # Convert chat_id to integer for Telegram API
            chat_id_int = convert_chat_id_to_int(group_chat_id, group_id, group_title)
            if chat_id_int is None:
                print(f"❌ [定时任务] Failed to convert chat_id for group {group_id} ({group_title}), chat_id={group_chat_id}", flush=True)
                return
            
            # Mute the user in the group with comprehensive restrictions
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id_int,
                    user_id=user_tg_id,
                    permissions=get_muted_permissions()
                )
                print(f"✅ [定时任务] Successfully called restrict_chat_member API - Muted user {user_tg_id} in group {group_title} (chat_id={chat_id_int})", flush=True)
                
                # ✅ NEW: Only mark as banned in DB after successful API call
                success = await asyncio.get_running_loop().run_in_executor(None, _mark_user_banned_in_db, group_user_id)
                if success:
                    print(f"✅ [定时任务] Marked user {user_tg_id} as banned in database after successful API call", flush=True)
                else:
                    print(f"⚠️  [定时任务] API succeeded but failed to update database for user {user_tg_id}", flush=True)
                    
            except Exception as restrict_error:
                # 详细记录 restrict_chat_member API 调用失败的错误
                print(f"❌ [定时任务] restrict_chat_member API failed for user {user_tg_id} in group {group_title} (chat_id={chat_id_int})", flush=True)
                print(f"   Error type: {type(restrict_error).__name__}", flush=True)
                print(f"   Error details: {str(restrict_error)}", flush=True)
                print(f"   Traceback: {traceback.format_exc()}", flush=True)
                print(f"   ⚠️  Database NOT updated - user will be retried in next check", flush=True)
                # Don't re-raise to avoid duplicate logging in outer exception handler
                return
            
            # Try to send notification to user privately
            try:
                # Sanitize HTML before sending to Telegram
                sanitized_msg = sanitize_html_for_telegram(ban_msg)
                await context.bot.send_message(
                    chat_id=user_tg_id,
                    text=sanitized_msg,
                    parse_mode='HTML'
                )
                print(f"✅ [定时任务] Sent ban notification to user {user_tg_id}", flush=True)
            except Exception as e:
                # If private message fails, we don't send to group to avoid spam
                print(f"⚠️  [定时任务] Failed to send ban notification to user {user_tg_id}: {e}", flush=True)
                
        except Exception as e:
            # 捕获所有其他异常，确保详细记录
            print(f"❌ [定时任务] Unexpected error in ban_user_async for user {user_tg_id} in group {group_chat_id} (type: {type(group_chat_id).__name__})", flush=True)
            print(f"   Error: {type(e).__name__}: {str(e)}", flush=True)
            print(f"   Full traceback: {traceback.format_exc()}", flush=True)
    
    # Use semaphore to limit concurrent operations and avoid Telegram API rate limits
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BANS)
    
    async def ban_with_limit(user_data):
        user_tg_id = user_data['user_tg_id']
        print(f"🔄 [定时任务] ban_with_limit wrapper CALLED for user {user_tg_id}", flush=True)
        async with semaphore:
            await ban_user_async(user_data)
    
    # Run ban operations with rate limiting
    print(f"📋 [定时任务] About to call asyncio.gather for {len(users_to_ban)} ban operations", flush=True)
    results = await asyncio.gather(*[ban_with_limit(user_data) for user_data in users_to_ban], return_exceptions=True)
    print(f"✅ [定时任务] asyncio.gather completed, results count: {len(results)}", flush=True)
    
    # Log any exceptions that occurred
    for idx, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"❌ [定时任务] Exception in ban operation {idx}: {type(result).__name__}: {str(result)}", flush=True)

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
                    
                    # 检查模块是否启用
                    conf = get_group_conf(msg.group)
                    if not conf.get('scheduled_msg_open', True):
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
                buttons = build_inline_keyboard_from_links(links)
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
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
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

# 🆕 New Feature Handlers

async def handle_new_chat_member(update: Update, context):
    """Handle new members joining the group - Entry verification, welcome messages, etc."""
    if not global_flask_app or not update.message or not update.message.new_chat_members:
        return
    
    try:
        chat = update.effective_chat
        if chat.type not in ['group', 'supergroup']:
            return
        
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group or not group.is_active:
                return
            
            settings = GroupEntryExitSettings.query.filter_by(group_id=group.id).first()
            if not settings:
                return
            
            for new_member in update.message.new_chat_members:
                if new_member.is_bot:
                    continue
                
                # Entry verification
                if settings.entry_verification_enabled and settings.verification_question:
                    try:
                        # Send verification question
                        question_text = f"👋 欢迎 {new_member.first_name}!\n\n"
                        question_text += f"🔐 请回答验证问题:\n{settings.verification_question}\n\n"
                        question_text += f"⏱ 超时时间: {settings.verification_timeout}秒"
                        
                        await context.bot.send_message(
                            chat_id=chat.id,
                            text=question_text
                        )
                        # Note: Full verification logic would require storing pending verifications
                        # and checking responses - simplified here for initial implementation
                    except Exception as e:
                        print(f"Error sending verification question: {e}")
                
                # Welcome message
                if settings.welcome_enabled and settings.welcome_message:
                    try:
                        welcome_text = settings.welcome_message.replace('{username}', new_member.first_name)
                        welcome_text = sanitize_html_for_telegram(welcome_text)
                        
                        if settings.welcome_media_type == 'image' and settings.welcome_media_url:
                            await context.bot.send_photo(
                                chat_id=chat.id,
                                photo=settings.welcome_media_url,
                                caption=welcome_text,
                                parse_mode='HTML'
                            )
                        elif settings.welcome_media_type == 'video' and settings.welcome_media_url:
                            await context.bot.send_video(
                                chat_id=chat.id,
                                video=settings.welcome_media_url,
                                caption=welcome_text,
                                parse_mode='HTML'
                            )
                        elif welcome_text:
                            await context.bot.send_message(
                                chat_id=chat.id,
                                text=welcome_text,
                                parse_mode='HTML'
                            )
                    except Exception as e:
                        print(f"Error sending welcome message: {e}")
                
                # Track invitation for points system
                if update.message.from_user and update.message.from_user.id != new_member.id:
                    inviter_id = update.message.from_user.id
                    invitation_activity = InvitationActivity.query.filter_by(group_id=group.id, enabled=True).first()
                    
                    if invitation_activity:
                        # Award points to inviter
                        user_points = UserPoints.query.filter_by(
                            group_id=group.id,
                            user_id=inviter_id
                        ).first()
                        
                        if not user_points:
                            user_points = UserPoints(
                                group_id=group.id,
                                user_id=inviter_id,
                                points_balance=0
                            )
                            db.session.add(user_points)
                        
                        user_points.points_balance += invitation_activity.reward_points
                        
                        # Log the points transaction
                        points_log = PointsLog(
                            group_id=group.id,
                            user_id=inviter_id,
                            points_change=invitation_activity.reward_points,
                            reason=f"邀请新成员: {new_member.first_name}",
                            balance_after=user_points.points_balance
                        )
                        db.session.add(points_log)
                        db.session.commit()
                        
    except Exception as e:
        print(f"Error in handle_new_chat_member: {e}")

async def handle_left_chat_member(update: Update, context):
    """Handle members leaving the group - Exit ban functionality"""
    if not global_flask_app or not update.message or not update.message.left_chat_member:
        return
    
    try:
        chat = update.effective_chat
        if chat.type not in ['group', 'supergroup']:
            return
        
        left_member = update.message.left_chat_member
        if left_member.is_bot:
            return
        
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group or not group.is_active:
                return
            
            settings = GroupEntryExitSettings.query.filter_by(group_id=group.id).first()
            if not settings or not settings.exit_ban_enabled:
                return
            
            # Ban user who left
            try:
                if settings.exit_ban_duration == 0:
                    # Permanent ban
                    await context.bot.ban_chat_member(chat.id, left_member.id)
                else:
                    # Temporary ban
                    until_date = datetime.now() + timedelta(minutes=settings.exit_ban_duration)
                    await context.bot.ban_chat_member(
                        chat.id,
                        left_member.id,
                        until_date=until_date
                    )
                print(f"Banned user {left_member.id} for leaving group {chat.id}")
            except Exception as e:
                print(f"Error banning user who left: {e}")
    
    except Exception as e:
        print(f"Error in handle_left_chat_member: {e}")

async def check_spam_protection(update: Update, context):
    """Monitor messages for spam protection rules"""
    if not global_flask_app or not update.effective_message:
        return False
    
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        
        if not chat or chat.type not in ['group', 'supergroup']:
            return False
        
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group or not group.is_active:
                return False
            
            protection = SpamProtection.query.filter_by(group_id=group.id, enabled=True).first()
            if not protection:
                return False
            
            # Check whitelist
            try:
                whitelist = json.loads(protection.whitelist_users or '[]')
                if user.id in whitelist:
                    return False
            except json.JSONDecodeError as e:
                print(f"Error parsing whitelist JSON: {e}")
                # Continue with spam check if whitelist is invalid
            
            # Check for blocked content
            should_punish = False
            
            # Block links
            if protection.block_links and msg.text:
                if 'http://' in msg.text or 'https://' in msg.text or 'www.' in msg.text:
                    should_punish = True
            
            # Block forwards
            if protection.block_forwards and msg.forward_date:
                should_punish = True
            
            # Block stickers
            if protection.block_stickers and msg.sticker:
                should_punish = True
            
            if should_punish:
                # Delete the message
                try:
                    await msg.delete()
                except Exception as e:
                    print(f"Failed to delete spam message: {e}")
                
                # Apply punishment
                if protection.punishment_type == 'mute':
                    until_date = datetime.now() + timedelta(minutes=protection.punishment_duration)
                    await context.bot.restrict_chat_member(
                        chat_id=chat.id,
                        user_id=user.id,
                        permissions=get_muted_permissions(),
                        until_date=until_date
                    )
                elif protection.punishment_type == 'kick':
                    await context.bot.ban_chat_member(chat.id, user.id)
                    await context.bot.unban_chat_member(chat.id, user.id)
                elif protection.punishment_type == 'ban':
                    until_date = datetime.now() + timedelta(minutes=protection.punishment_duration)
                    await context.bot.ban_chat_member(chat.id, user.id, until_date=until_date)
                
                return True
            
            return False
            
    except Exception as e:
        print(f"Error in check_spam_protection: {e}")
        return False

async def check_timed_group_control(context):
    """Background task to check and apply timed group controls"""
    if not global_flask_app:
        return
    
    try:
        def _get_controls():
            with global_flask_app.app_context():
                return TimedGroupControl.query.filter_by(enabled=True).all()
        
        controls = await asyncio.get_running_loop().run_in_executor(None, _get_controls)
        
        for control in controls:
            try:
                # Get current time in the specified timezone
                tz = pytz.timezone(control.timezone)
                now = datetime.now(tz)
                current_time = now.time()
                
                # Check if we should open or close the group
                if control.open_time and control.close_time:
                    # Determine if group should be open or closed
                    should_be_open = False
                    
                    if control.open_time < control.close_time:
                        # Normal case: open at 9am, close at 5pm
                        should_be_open = control.open_time <= current_time < control.close_time
                    else:
                        # Overnight case: open at 9pm, close at 6am
                        should_be_open = current_time >= control.open_time or current_time < control.close_time
                    
                    # Get group to check current state
                    with global_flask_app.app_context():
                        group = BotGroup.query.get(control.group_id)
                        if not group:
                            continue
                        
                        chat_id = int(group.chat_id)
                        
                        # Track state in group config to avoid repeated messages
                        conf = get_group_conf(group)
                        last_state = conf.get('_timed_control_state', None)
                        
                        # Only act if state has changed
                        if last_state != should_be_open:
                            try:
                                if should_be_open:
                                    # Open the group - allow all members to send messages
                                    # Note: This requires bot to have appropriate admin rights
                                    # For supergroups, we can't change permissions for all users at once
                                    # Instead, we send the open message
                                    if control.open_message:
                                        open_text = sanitize_html_for_telegram(control.open_message)
                                        await context.bot.send_message(
                                            chat_id=chat_id,
                                            text=open_text,
                                            parse_mode='HTML'
                                        )
                                else:
                                    # Close the group - send close message
                                    if control.close_message:
                                        close_text = sanitize_html_for_telegram(control.close_message)
                                        await context.bot.send_message(
                                            chat_id=chat_id,
                                            text=close_text,
                                            parse_mode='HTML'
                                        )
                                
                                # Update state
                                conf['_timed_control_state'] = should_be_open
                                group.config = json.dumps(conf, ensure_ascii=False)
                                db.session.commit()
                                
                            except Exception as e:
                                print(f"Error applying timed control for group {chat_id}: {e}")
                        
            except Exception as e:
                print(f"Error processing timed control for group {control.group_id}: {e}")
                
    except Exception as e:
        print(f"Error in check_timed_group_control: {e}")

async def track_user_name_change(update: Update, context):
    """Track when users change their names"""
    if not global_flask_app or not update.message:
        return
    
    try:
        chat = update.effective_chat
        user = update.effective_user
        
        if not chat or chat.type not in ['group', 'supergroup']:
            return
        
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return
            
            # Get the last known name for this user
            last_change = UserNameChange.query.filter_by(
                group_id=group.id,
                user_id=user.id
            ).order_by(UserNameChange.changed_at.desc()).first()
            
            current_name = user.first_name
            if user.last_name:
                current_name += f" {user.last_name}"
            
            # Check if name has changed
            if last_change:
                if last_change.new_name != current_name:
                    # Name has changed
                    name_change = UserNameChange(
                        group_id=group.id,
                        user_id=user.id,
                        old_name=last_change.new_name,
                        new_name=current_name,
                        changed_at=get_beijing_now()
                    )
                    db.session.add(name_change)
                    db.session.commit()
                    
                    # 🆕 Send alert message in group
                    try:
                        # Sanitize names for HTML safety
                        old_name_safe = sanitize_html_for_telegram(last_change.new_name)
                        new_name_safe = sanitize_html_for_telegram(current_name)
                        alert_message = (
                            f"📝 用户改名提醒\n\n"
                            f"用户ID: <code>{user.id}</code>\n"
                            f"旧昵称: {old_name_safe}\n"
                            f"新昵称: {new_name_safe}\n"
                            f"时间: {get_beijing_now().strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        await context.bot.send_message(
                            chat_id=chat.id,
                            text=alert_message,
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        print(f"Error sending name change alert: {e}")
            else:
                # First time seeing this user, record initial name
                name_change = UserNameChange(
                    group_id=group.id,
                    user_id=user.id,
                    old_name=None,
                    new_name=current_name,
                    changed_at=get_beijing_now()
                )
                db.session.add(name_change)
                db.session.commit()
                
    except Exception as e:
        print(f"Error in track_user_name_change: {e}")

async def handle_sync_group_messages(update: Update, context):
    """Sync messages from source group to target groups - Enhanced to sync all message types"""
    if not global_flask_app or not update.effective_message:
        return
    
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        
        if not chat or chat.type not in ['group', 'supergroup']:
            return
        
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return
            
            # Find sync settings where this group is the source
            sync_settings = SyncGroupMessages.query.filter_by(
                source_group_id=group.id,
                enabled=True
            ).all()
            
            for sync_setting in sync_settings:
                try:
                    # Check keyword filters for text messages
                    if msg.text and sync_setting.filter_keywords:
                        try:
                            keywords = json.loads(sync_setting.filter_keywords)
                            if any(kw in msg.text for kw in keywords):
                                continue  # Skip this message due to keyword filter
                        except json.JSONDecodeError as e:
                            print(f"Error parsing filter keywords JSON: {e}")
                            # Continue with sync if filter is invalid
                    
                    # Skip forwards if not enabled
                    if msg.forward_date and not sync_setting.sync_forwards:
                        continue
                    
                    # Sync the message to target group
                    # target_group_id is stored as string chat_id, not database id
                    target_chat_id = sync_setting.target_group_id
                    
                    # Prepare sender info (include bot messages)
                    if user:
                        sender_name = user.first_name
                        if user.last_name:
                            sender_name += f" {user.last_name}"
                        sender_prefix = f"[{sender_name}] "
                    else:
                        sender_prefix = "[机器人] "
                    
                    sent_msg = None
                    message_type = 'unknown'
                    
                    # Handle different message types
                    if msg.text:
                        message_type = 'text'
                        synced_text = f"{sender_prefix}{msg.text}"
                        sent_msg = await context.bot.send_message(
                            chat_id=target_chat_id,
                            text=synced_text
                        )
                    elif msg.photo and sync_setting.sync_media:
                        message_type = 'photo'
                        caption = f"{sender_prefix}{msg.caption or ''}"
                        sent_msg = await context.bot.send_photo(
                            chat_id=target_chat_id,
                            photo=msg.photo[-1].file_id,
                            caption=caption
                        )
                    elif msg.video and sync_setting.sync_media:
                        message_type = 'video'
                        caption = f"{sender_prefix}{msg.caption or ''}"
                        sent_msg = await context.bot.send_video(
                            chat_id=target_chat_id,
                            video=msg.video.file_id,
                            caption=caption
                        )
                    elif msg.document and sync_setting.sync_media:
                        message_type = 'document'
                        caption = f"{sender_prefix}{msg.caption or ''}"
                        sent_msg = await context.bot.send_document(
                            chat_id=target_chat_id,
                            document=msg.document.file_id,
                            caption=caption
                        )
                    elif msg.audio and sync_setting.sync_media:
                        message_type = 'audio'
                        caption = f"{sender_prefix}{msg.caption or ''}"
                        sent_msg = await context.bot.send_audio(
                            chat_id=target_chat_id,
                            audio=msg.audio.file_id,
                            caption=caption
                        )
                    elif msg.voice and sync_setting.sync_media:
                        message_type = 'voice'
                        caption = f"{sender_prefix}{msg.caption or ''}"
                        sent_msg = await context.bot.send_voice(
                            chat_id=target_chat_id,
                            voice=msg.voice.file_id,
                            caption=caption
                        )
                    elif msg.video_note and sync_setting.sync_media:
                        message_type = 'video_note'
                        sent_msg = await context.bot.send_video_note(
                            chat_id=target_chat_id,
                            video_note=msg.video_note.file_id
                        )
                    elif msg.sticker and sync_setting.sync_media:
                        message_type = 'sticker'
                        sent_msg = await context.bot.send_sticker(
                            chat_id=target_chat_id,
                            sticker=msg.sticker.file_id
                        )
                    elif msg.animation and sync_setting.sync_media:
                        message_type = 'animation'
                        caption = f"{sender_prefix}{msg.caption or ''}"
                        sent_msg = await context.bot.send_animation(
                            chat_id=target_chat_id,
                            animation=msg.animation.file_id,
                            caption=caption
                        )
                    elif msg.poll:
                        message_type = 'poll'
                        # Forward poll as-is (can't modify poll sender)
                        sent_msg = await context.bot.forward_message(
                            chat_id=target_chat_id,
                            from_chat_id=chat.id,
                            message_id=msg.message_id
                        )
                    elif msg.location:
                        message_type = 'location'
                        # Send location with sender prefix as a separate message
                        await context.bot.send_message(
                            chat_id=target_chat_id,
                            text=sender_prefix
                        )
                        sent_msg = await context.bot.send_location(
                            chat_id=target_chat_id,
                            latitude=msg.location.latitude,
                            longitude=msg.location.longitude
                        )
                    elif msg.contact:
                        message_type = 'contact'
                        # Send contact with sender prefix as a separate message
                        await context.bot.send_message(
                            chat_id=target_chat_id,
                            text=sender_prefix
                        )
                        sent_msg = await context.bot.send_contact(
                            chat_id=target_chat_id,
                            phone_number=msg.contact.phone_number,
                            first_name=msg.contact.first_name,
                            last_name=msg.contact.last_name
                        )
                    elif msg.venue:
                        message_type = 'venue'
                        # Send venue with sender prefix as a separate message
                        await context.bot.send_message(
                            chat_id=target_chat_id,
                            text=sender_prefix
                        )
                        sent_msg = await context.bot.send_venue(
                            chat_id=target_chat_id,
                            latitude=msg.venue.location.latitude,
                            longitude=msg.venue.location.longitude,
                            title=msg.venue.title,
                            address=msg.venue.address
                        )
                    else:
                        # Unknown or unsupported message type, skip
                        continue
                    
                    # Log the sync if message was sent
                    if sent_msg:
                        log_entry = SyncMessageLog(
                            source_group_id=group.id,
                            target_group_id=target_chat_id,
                            source_message_id=msg.message_id,
                            target_message_id=sent_msg.message_id,
                            user_id=user.id if user else None,
                            username=user.username if user else None,
                            message_type=message_type,
                            content_preview=msg.text[:100] if msg.text else (msg.caption[:100] if msg.caption else None),
                            status='success',
                            synced_at=get_beijing_now()
                        )
                        db.session.add(log_entry)
                        db.session.commit()
                    
                except Exception as e:
                    print(f"Error syncing message to {sync_setting.target_group_id}: {e}")
                    # Log the failure
                    log_entry = SyncMessageLog(
                        source_group_id=group.id,
                        target_group_id=sync_setting.target_group_id,
                        source_message_id=msg.message_id,
                        user_id=user.id if user else None,
                        username=user.username if user else None,
                        status='failed',
                        error_message=str(e),
                        synced_at=get_beijing_now()
                    )
                    db.session.add(log_entry)
                    db.session.commit()
                    
    except Exception as e:
        print(f"Error in handle_sync_group_messages: {e}")

async def handle_auto_delete_messages(update: Update, context):
    """Auto-delete system messages based on settings"""
    if not global_flask_app or not update.message:
        return
    
    try:
        msg = update.message
        chat = update.effective_chat
        
        if not chat or chat.type not in ['group', 'supergroup']:
            return
        
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return
            
            settings = OtherSettings.query.filter_by(group_id=group.id).first()
            if not settings:
                return
            
            should_delete = False
            
            # Check for join messages
            if settings.auto_delete_join_msg and msg.new_chat_members:
                should_delete = True
            
            # Check for leave messages
            if settings.auto_delete_leave_msg and msg.left_chat_member:
                should_delete = True
            
            # Check for pin messages
            if settings.auto_delete_pin_msg and msg.pinned_message:
                should_delete = True
            
            if should_delete:
                try:
                    await msg.delete()
                except Exception as e:
                    print(f"Error deleting message: {e}")
                    
    except Exception as e:
        print(f"Error in handle_auto_delete_messages: {e}")

async def check_channel_subscriptions(context):
    """Background task to check forced channel subscriptions"""
    if not global_flask_app:
        return
    
    try:
        def _get_subscription_settings():
            with global_flask_app.app_context():
                return ForcedChannelSubscription.query.filter_by(enabled=True).all()
        
        settings_list = await asyncio.get_running_loop().run_in_executor(None, _get_subscription_settings)
        
        for settings in settings_list:
            try:
                if not settings.channel_id:
                    continue
                
                with global_flask_app.app_context():
                    group = BotGroup.query.get(settings.group_id)
                    if not group:
                        continue
                    
                    chat_id = int(group.chat_id)
                    
                    # Get all group members (this is simplified - actual implementation 
                    # would need to track active members)
                    # Use configurable batch size to respect API rate limits
                    SUBSCRIPTION_CHECK_BATCH_SIZE = 10  # Check 10 users per run
                    group_users = GroupUser.query.filter_by(group_id=group.id).limit(SUBSCRIPTION_CHECK_BATCH_SIZE).all()
                    
                    for group_user in group_users:
                        try:
                            # Check if user is subscribed to the channel
                            member = await context.bot.get_chat_member(
                                settings.channel_id,
                                group_user.tg_id
                            )
                            
                            # If not subscribed (left or kicked), apply action
                            if member.status in ['left', 'kicked']:
                                if settings.unsubscribe_action == 'kick':
                                    await context.bot.ban_chat_member(chat_id, group_user.tg_id)
                                    await context.bot.unban_chat_member(chat_id, group_user.tg_id)
                                elif settings.unsubscribe_action == 'ban':
                                    await context.bot.ban_chat_member(chat_id, group_user.tg_id)
                                elif settings.unsubscribe_action == 'mute':
                                    try:
                                        print(f"🔄 [频道订阅检测] 准备禁言未订阅用户 {group_user.tg_id} in group {settings.group_id} (chat_id={chat_id})", flush=True)
                                        await context.bot.restrict_chat_member(
                                            chat_id=chat_id,
                                            user_id=group_user.tg_id,
                                            permissions=get_muted_permissions()
                                        )
                                        print(f"✅ [频道订阅检测] Successfully called restrict_chat_member API - Muted unsubscribed user {group_user.tg_id} in group {settings.group_id} (chat_id={chat_id})", flush=True)
                                    except Exception as restrict_error:
                                        print(f"❌ [频道订阅检测] restrict_chat_member API failed for user {group_user.tg_id} in group {settings.group_id} (chat_id={chat_id})", flush=True)
                                        print(f"   Error type: {type(restrict_error).__name__}", flush=True)
                                        print(f"   Error details: {str(restrict_error)}", flush=True)
                                        print(f"   Traceback: {traceback.format_exc()}", flush=True)
                        except Exception as e:
                            # User may not be in channel or bot doesn't have access
                            print(f"Error checking subscription for user {group_user.tg_id}: {e}")
                            
            except Exception as e:
                print(f"Error processing channel subscription for group {settings.group_id}: {e}")
                
    except Exception as e:
        print(f"Error in check_channel_subscriptions: {e}")

async def update_member_levels(context):
    """Background task to update member levels based on points"""
    if not global_flask_app:
        return
    
    try:
        def _update_levels():
            with global_flask_app.app_context():
                # Get all users with points
                user_points_list = UserPoints.query.all()
                
                updated_count = 0
                for user_points in user_points_list:
                    # Find the highest level this user qualifies for
                    levels = MemberLevel.query.filter_by(
                        group_id=user_points.group_id
                    ).order_by(MemberLevel.required_points.desc()).all()
                    
                    # Determine the appropriate level for this user
                    new_level = None
                    for level in levels:
                        if user_points.points_balance >= level.required_points:
                            new_level = level
                            break
                    
                    # Only update if the level has changed
                    new_level_id = new_level.id if new_level else None
                    if user_points.current_level_id != new_level_id:
                        user_points.current_level_id = new_level_id
                        updated_count += 1
                
                db.session.commit()
                return updated_count
        
        count = await asyncio.get_running_loop().run_in_executor(None, _update_levels)
        if count > 0:
            print(f"✅ Updated member levels for {count} users")
        
    except Exception as e:
        print(f"❌ Error in update_member_levels: {e}")

async def run_lottery_draws(context):
    """Background task to run lottery draws when time is up"""
    if not global_flask_app:
        return
    
    try:
        def _get_active_lotteries():
            with global_flask_app.app_context():
                now = get_beijing_now()
                # Find lotteries that have ended but not yet drawn
                return GroupLottery.query.filter(
                    GroupLottery.status == 'active',
                    GroupLottery.end_time <= now
                ).all()
        
        lotteries = await asyncio.get_running_loop().run_in_executor(None, _get_active_lotteries)
        
        for lottery in lotteries:
            try:
                with global_flask_app.app_context():
                    group = BotGroup.query.get(lottery.group_id)
                    if not group:
                        continue
                    
                    chat_id = int(group.chat_id)
                    winners = []
                    
                    if lottery.lottery_type == 'message_count':
                        # 🆕 Use actual message tracking data
                        # Get participants who sent at least one message
                        # Limit to top MAX_LOTTERY_MESSAGE_COUNT_RECORDS participants for performance in very large groups
                        message_counts = LotteryMessageCount.query.filter_by(
                            lottery_id=lottery.id
                        ).filter(LotteryMessageCount.message_count > 0).order_by(
                            LotteryMessageCount.message_count.desc()
                        ).limit(MAX_LOTTERY_MESSAGE_COUNT_RECORDS).all()
                        
                        if message_counts:
                            # Create weighted random selection based on message counts
                            # Users with more messages have higher chance to win
                            participants = [(mc.user_id, mc.message_count) for mc in message_counts]
                            
                            # Weighted random selection
                            total_messages = sum(count for _, count in participants)
                            if total_messages > 0:
                                rand_val = random.uniform(0, total_messages)
                                cumulative = 0
                                for user_id, count in participants:
                                    cumulative += count
                                    if cumulative >= rand_val:
                                        winners = [user_id]
                                        break
                        else:
                            # Fallback: if no one sent messages, pick from group users (limited sample)
                            eligible_users = GroupUser.query.filter_by(group_id=group.id).limit(MAX_LOTTERY_PARTICIPANTS).all()
                            if eligible_users:
                                winner = random.choice(eligible_users)
                                winners = [winner.tg_id]
                    
                    elif lottery.lottery_type == 'message_rank':
                        # 🆕 Use actual message tracking data - top N senders
                        # Optimized: use limit() to avoid loading all records
                        top_senders = LotteryMessageCount.query.filter_by(
                            lottery_id=lottery.id
                        ).order_by(LotteryMessageCount.message_count.desc()).limit(
                            min(lottery.top_n_winners or 1, MAX_AUCTION_WINNERS)  # Cap at MAX_AUCTION_WINNERS for performance
                        ).all()
                        
                        winners = [mc.user_id for mc in top_senders]
                        
                        if not winners:
                            # Fallback: if no tracking data, use group users (limited sample)
                            top_users = GroupUser.query.filter_by(group_id=group.id).limit(
                                min(lottery.top_n_winners or 1, MAX_AUCTION_WINNERS)
                            ).all()
                            winners = [u.tg_id for u in top_users]
                    
                    # Update lottery with winners
                    lottery.winner_ids = json.dumps(winners)
                    lottery.status = 'ended'
                    db.session.commit()
                    
                    # Announce winners
                    if winners:
                        winner_mentions = [f"<a href='tg://user?id={uid}'>用户{uid}</a>" for uid in winners]
                        message = f"🎉 <b>抽奖结束！</b>\n\n"
                        message += f"活动：{lottery.lottery_name}\n"
                        message += f"获奖者：{', '.join(winner_mentions)}\n"
                        if lottery.prize_description:
                            message += f"奖品：{lottery.prize_description}\n"
                        
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=message,
                            parse_mode='HTML'
                        )
                        
            except Exception as e:
                print(f"Error running lottery {lottery.id}: {e}")
                
    except Exception as e:
        print(f"Error in run_lottery_draws: {e}")


async def check_inactive_users(context):
    """检查并处理不活跃用户 - Background task"""
    if not global_flask_app: return
    try:
        def _check_inactive():
            """Sync part: query database and collect users for moderation actions"""
            with global_flask_app.app_context():
                try:
                    # Get all groups with inactive user settings enabled
                    settings_list = InactiveUserSettings.query.filter_by(enabled=True).all()
                    
                    users_to_mute = []
                    
                    for settings in settings_list:
                        try:
                            group = BotGroup.query.get(settings.group_id)
                            if not group or not group.is_active:
                                continue
                            
                            # Convert chat_id to integer for Telegram API
                            chat_id_int = convert_chat_id_to_int(group.chat_id, group.id)
                            if chat_id_int is None:
                                continue
                            
                            # Calculate threshold date
                            threshold_date = datetime.now() - timedelta(days=settings.inactivity_days)
                            
                            # Find inactive users
                            inactive_users = GroupUser.query.filter(
                                GroupUser.group_id == settings.group_id,
                                GroupUser.last_activity < threshold_date,
                                GroupUser.is_banned == False
                            ).limit(EXPIRED_USERS_BATCH_SIZE).all()  # Process in batches
                            
                            for user in inactive_users:
                                # Take action based on settings
                                if settings.action_type == 'kick':
                                    users_to_mute.append({
                                        'action': 'kick',
                                        'chat_id': chat_id_int,
                                        'user_id': user.tg_id,
                                        'group_id': settings.group_id,
                                        'user_db_id': user.id  # Store DB ID for later update
                                    })
                                elif settings.action_type == 'ban':
                                    users_to_mute.append({
                                        'action': 'ban',
                                        'chat_id': chat_id_int,
                                        'user_id': user.tg_id,
                                        'group_id': settings.group_id,
                                        'user_db_id': user.id  # Store DB ID for later update
                                    })
                                elif settings.action_type == 'mute':
                                    users_to_mute.append({
                                        'action': 'mute',
                                        'chat_id': chat_id_int,
                                        'user_id': user.tg_id,
                                        'group_id': settings.group_id,
                                        'user_db_id': user.id  # Store DB ID for later update
                                    })
                                    
                        except Exception as e:
                            print(f"Error processing group {settings.group_id}: {e}")
                            continue
                    
                    return users_to_mute
                    
                except Exception as e:
                    print(f"Error in _check_inactive: {e}")
                    return []
        
        # Execute sync part in executor
        users_to_mute = await asyncio.get_running_loop().run_in_executor(None, _check_inactive)
        
        # Now handle async operations
        if users_to_mute:
            for item in users_to_mute:
                try:
                    if item['action'] == 'kick':
                        await context.bot.ban_chat_member(item['chat_id'], item['user_id'])
                        await context.bot.unban_chat_member(item['chat_id'], item['user_id'])
                    elif item['action'] == 'ban':
                        await context.bot.ban_chat_member(item['chat_id'], item['user_id'])
                        # Update database only after successful API call
                        def _update_banned_status():
                            with global_flask_app.app_context():
                                try:
                                    user = GroupUser.query.get(item['user_db_id'])
                                    if user:
                                        user.is_banned = True
                                        db.session.commit()
                                except Exception as e:
                                    print(f"Error updating is_banned for user {item['user_id']}: {e}")
                                    db.session.rollback()
                        await asyncio.get_running_loop().run_in_executor(None, _update_banned_status)
                    elif item['action'] == 'mute':
                        try:
                            print(f"🔄 [不活跃用户检测] 准备禁言不活跃用户 {item['user_id']} in group {item['group_id']} (chat_id={item['chat_id']})", flush=True)
                            await context.bot.restrict_chat_member(
                                chat_id=item['chat_id'],
                                user_id=item['user_id'],
                                permissions=get_muted_permissions()
                            )
                            print(f"✅ [不活跃用户检测] Successfully called restrict_chat_member API - Muted inactive user {item['user_id']} in group {item['group_id']} (chat_id={item['chat_id']})", flush=True)
                        except Exception as restrict_error:
                            print(f"❌ [不活跃用户检测] restrict_chat_member API failed for user {item['user_id']} in group {item['group_id']} (chat_id={item['chat_id']})", flush=True)
                            print(f"   Error type: {type(restrict_error).__name__}", flush=True)
                            print(f"   Error details: {str(restrict_error)}", flush=True)
                            print(f"   Traceback: {traceback.format_exc()}", flush=True)
                except Exception as e:
                    print(f"Error executing action for user {item['user_id']}: {e}")
                    continue
                    
    except Exception as e:
        print(f"Error in check_inactive_users: {e}")


async def track_message_statistics(update: Update, context):
    """跟踪消息统计 - Called from on_message"""
    if not global_flask_app: return
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        
        if not user or chat.type not in ['group', 'supergroup']:
            return
        
        def _track_stats():
            with global_flask_app.app_context():
                group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if not group:
                    return
                
                today = datetime.now().date()
                
                # Use upsert-like pattern to avoid race conditions
                stats = MessageStatistics.query.filter_by(
                    group_id=group.id,
                    user_id=user.id,
                    date=today
                ).with_for_update().first()
                
                if not stats:
                    stats = MessageStatistics(
                        group_id=group.id,
                        user_id=user.id,
                        date=today,
                        message_count=0,
                        text_count=0,
                        photo_count=0,
                        video_count=0,
                        sticker_count=0,
                        document_count=0,
                        voice_count=0
                    )
                    db.session.add(stats)
                    try:
                        db.session.flush()
                    except:
                        # Another thread created it, re-fetch
                        db.session.rollback()
                        stats = MessageStatistics.query.filter_by(
                            group_id=group.id,
                            user_id=user.id,
                            date=today
                        ).first()
                        if not stats:
                            return
                
                # Update counts
                stats.message_count += 1
                
                if msg.text:
                    stats.text_count += 1
                elif msg.photo:
                    stats.photo_count += 1
                elif msg.video:
                    stats.video_count += 1
                elif msg.sticker:
                    stats.sticker_count += 1
                elif msg.document:
                    stats.document_count += 1
                elif msg.voice:
                    stats.voice_count += 1
                
                db.session.commit()
        
        await asyncio.get_running_loop().run_in_executor(None, _track_stats)
    except Exception as e:
        print(f"Error tracking message statistics: {e}")


async def check_keyword_filter(update: Update, context):
    """检查关键词过滤 - Called from on_message, returns True if message should be deleted"""
    if not global_flask_app: return False
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        
        if not msg.text or chat.type not in ['group', 'supergroup']:
            return False
        
        def _check_filters():
            with global_flask_app.app_context():
                group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if not group:
                    return None
                
                # Get active keyword filters
                filters_list = KeywordFilter.query.filter_by(
                    group_id=group.id,
                    is_active=True
                ).all()
                
                for kf in filters_list:
                    matched = False
                    
                    if kf.match_type == 'exact':
                        matched = msg.text.strip().lower() == kf.keyword.lower()
                    elif kf.match_type == 'contains':
                        matched = kf.keyword.lower() in msg.text.lower()
                    elif kf.match_type == 'regex':
                        try:
                            matched = re.search(kf.keyword, msg.text, re.IGNORECASE) is not None
                        except re.error:
                            # Invalid regex pattern, skip this filter
                            pass
                    
                    # Blacklist: if matched, take action
                    if matched and kf.filter_type == 'blacklist':
                        return kf
                    
                return None
        
        matched_filter = await asyncio.get_running_loop().run_in_executor(None, _check_filters)
        
        if matched_filter:
            # Take action based on filter settings
            if matched_filter.action == 'delete':
                await msg.delete()
                return True
            elif matched_filter.action == 'warn':
                await msg.reply_text(f"⚠️ 警告：消息包含禁止关键词")
            elif matched_filter.action == 'mute':
                try:
                    print(f"🔄 [内容过滤] 准备禁言触发关键词的用户 {user.id} in group {chat.id}", flush=True)
                    await context.bot.restrict_chat_member(
                        chat_id=chat.id,
                        user_id=user.id,
                        permissions=get_muted_permissions(),
                        until_date=datetime.now() + timedelta(minutes=10)
                    )
                    print(f"✅ [内容过滤] Successfully called restrict_chat_member API - Muted user {user.id} for 10 minutes in group {chat.id}", flush=True)
                except Exception as restrict_error:
                    print(f"❌ [内容过滤] restrict_chat_member API failed for user {user.id} in group {chat.id}", flush=True)
                    print(f"   Error type: {type(restrict_error).__name__}", flush=True)
                    print(f"   Error details: {str(restrict_error)}", flush=True)
                    print(f"   Traceback: {traceback.format_exc()}", flush=True)
                await msg.delete()
                return True
            elif matched_filter.action == 'kick':
                await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
                await context.bot.unban_chat_member(chat_id=chat.id, user_id=user.id)
                await msg.delete()
                return True
            elif matched_filter.action == 'ban':
                await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
                await msg.delete()
                return True
        
        return False
    except Exception as e:
        print(f"Error in check_keyword_filter: {e}")
        return False


async def update_user_activity(update: Update, context):
    """更新用户最后活动时间 - Called from on_message"""
    if not global_flask_app: return
    try:
        chat = update.effective_chat
        user = update.effective_user
        
        if not user or chat.type not in ['group', 'supergroup']:
            return
        
        def _update_activity():
            with global_flask_app.app_context():
                group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if not group:
                    return
                
                group_user = GroupUser.query.filter_by(
                    group_id=group.id,
                    tg_id=user.id
                ).first()
                
                if group_user:
                    group_user.last_activity = datetime.now()
                    db.session.commit()
        
        await asyncio.get_running_loop().run_in_executor(None, _update_activity)
    except Exception as e:
        print(f"Error updating user activity: {e}")


async def check_and_mute_expired_user(update: Update, context):
    """检查并立即禁言过期用户 - Called from on_message
    
    当认证用户在群内发消息时，即时检查其认证状态。
    如果已过期且未被禁言，则立即自动禁言。
    """
    if not global_flask_app:
        return
    
    try:
        chat = update.effective_chat
        user = update.effective_user
        
        # 只处理群组消息
        if not user or chat.type not in ['group', 'supergroup']:
            return
        
        # 排除机器人自身
        if user.is_bot:
            return
        
        # 检查是否为管理员，管理员不受限制
        is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
        if is_admin:
            return
        
        # 同步检查数据库并准备禁言操作
        def _check_expiration():
            with global_flask_app.app_context():
                try:
                    # 获取群组信息
                    group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                    if not group or not group.is_active:
                        return None
                    
                    # 查找用户记录
                    group_user = GroupUser.query.filter_by(
                        group_id=group.id,
                        tg_id=user.id
                    ).first()
                    
                    # 如果用户没有记录，或者没有设置过期时间，则不处理
                    if not group_user or not group_user.expiration_date:
                        return None
                    
                    # 如果用户已经被禁言，仍然尝试调用API（补救机制）
                    # 因为之前的API调用可能失败了，但数据库已标记为banned
                    # 这样可以确保用户真正被禁言
                    
                    # 检查是否过期
                    now = get_beijing_now()
                    if group_user.expiration_date >= now:
                        # 未过期，无需处理
                        return None
                    
                    # 用户已过期，需要立即禁言
                    # ✅ NEW: 不再先更新数据库，而是在API成功后更新
                    
                    # 获取配置信息用于通知消息
                    conf = get_group_conf(group)
                    ban_msg = conf.get('msg_expired_ban', '⛔️ <b>您的认证已过期，已被暂时禁言。请联系管理员续费。</b>')
                    
                    # Extract scalar values to avoid detached instance issues
                    return {
                        'group_chat_id': group.chat_id,
                        'group_id': group.id,
                        'group_user_id': group_user.id,  # Store group_user.id to update database later
                        'group_title': group.title,
                        'user_id': user.id,
                        'ban_msg': ban_msg
                    }
                    
                except Exception as e:
                    print(f"Error in _check_expiration: {e}")
                    db.session.rollback()
                    return None
        
        # 在线程池中执行数据库操作
        result = await asyncio.get_running_loop().run_in_executor(None, _check_expiration)
        
        # 如果不需要禁言，直接返回
        if not result:
            return
        
        print(f"🚀 [群消息触发] User {user.id} in group {chat.id} needs to be muted (expired)", flush=True)
        
        # Helper function to mark user as banned in database after successful API call
        def _mark_user_banned_in_db(group_user_id):
            """Mark user as banned in database - called after successful API mute"""
            with global_flask_app.app_context():
                try:
                    group_user = GroupUser.query.get(group_user_id)
                    if group_user:
                        group_user.is_banned = True
                        db.session.commit()
                        return True
                except Exception as e:
                    print(f"❌ [群消息触发] Failed to mark user {group_user_id} as banned in DB: {e}", flush=True)
                    db.session.rollback()
                    return False
            return False
        
        # 执行禁言操作
        try:
            group_chat_id = result['group_chat_id']
            group_id = result['group_id']
            group_user_id = result['group_user_id']
            group_title = result['group_title']
            user_id = result['user_id']
            ban_msg = result['ban_msg']
            
            # 转换 chat_id 为整数
            chat_id_int = convert_chat_id_to_int(group_chat_id, group_id, group_title)
            if chat_id_int is None:
                print(f"❌ [群消息触发] Failed to convert chat_id for group {group_id} ({group_title}), chat_id={group_chat_id}", flush=True)
                return
            
            print(f"🔧 [群消息触发] About to call restrict_chat_member for user {user_id} in group {group_title}", flush=True)
            
            # 禁言用户
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id_int,
                    user_id=user_id,
                    permissions=get_muted_permissions()
                )
                print(f"✅ [群消息触发] Successfully called restrict_chat_member API - Instantly muted expired user {user_id} in group {group_title} (chat_id={chat_id_int})", flush=True)
                
                # ✅ NEW: Only mark as banned in DB after successful API call
                success = await asyncio.get_running_loop().run_in_executor(None, _mark_user_banned_in_db, group_user_id)
                if success:
                    print(f"✅ [群消息触发] Marked user {user_id} as banned in database after successful API call", flush=True)
                else:
                    print(f"⚠️  [群消息触发] API succeeded but failed to update database for user {user_id}", flush=True)
                    
            except Exception as restrict_error:
                # 详细记录 restrict_chat_member API 调用失败的错误
                print(f"❌ [群消息触发] restrict_chat_member API failed for user {user_id} in group {group_title} (chat_id={chat_id_int})", flush=True)
                print(f"   Error type: {type(restrict_error).__name__}", flush=True)
                print(f"   Error details: {str(restrict_error)}", flush=True)
                print(f"   Traceback: {traceback.format_exc()}", flush=True)
                print(f"   ⚠️  Database NOT updated - user will be retried on next message or scheduled check", flush=True)
                # Don't re-raise to avoid duplicate logging in outer exception handler
                return
            
            # 尝试发送私信通知（不阻塞主流程）
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=ban_msg,
                    parse_mode='HTML'
                )
                print(f"✅ [群消息触发] Sent ban notification to user {user_id}", flush=True)
            except Exception as e:
                # 私信失败不影响主流程（用户可能未与机器人对话）
                print(f"⚠️  [群消息触发] Could not send DM to user {user_id}: {e}", flush=True)
                
        except Exception as e:
            print(f"❌ [群消息触发] Unexpected error muting expired user {user_id}", flush=True)
            print(f"   Error: {type(e).__name__}: {str(e)}", flush=True)
            print(f"   Full traceback: {traceback.format_exc()}", flush=True)
            
    except Exception as e:
        print(f"Error in check_and_mute_expired_user: {e}")


async def cmd_vote(update: Update, context):
    """创建投票命令 /vote 标题|选项1|选项2|..."""
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能创建投票")
        return
    
    # Parse vote content
    if not context.args:
        await update.message.reply_text(
            "📊 <b>创建投票</b>\n\n"
            "格式：/vote 标题|选项1|选项2|选项3...\n\n"
            "示例：/vote 今天吃什么|火锅|烧烤|快餐|自助餐",
            parse_mode='HTML'
        )
        return
    
    content = ' '.join(context.args)
    parts = content.split('|')
    
    if len(parts) < 3:
        await update.message.reply_text("❌ 至少需要标题和两个选项")
        return
    
    title = parts[0].strip()
    options = [opt.strip() for opt in parts[1:] if opt.strip()]
    
    if len(options) < 2:
        await update.message.reply_text("❌ 至少需要两个选项")
        return
    
    if len(options) > 10:
        await update.message.reply_text("❌ 选项最多10个")
        return
    
    if not global_flask_app:
        return
    
    def _create_vote():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return None, "群组不存在"
            
            # Create vote
            vote = GroupVote(
                group_id=group.id,
                title=title,
                options=json.dumps(options, ensure_ascii=False),
                vote_type='single',
                max_choices=1,
                is_anonymous=False,
                allow_revote=True,
                start_time=get_beijing_now(),
                end_time=get_beijing_now() + timedelta(days=7),  # Default 7 days
                status='active',
                created_by=user.id
            )
            db.session.add(vote)
            db.session.commit()
            return vote.id, None
    
    vote_id, error = await asyncio.get_running_loop().run_in_executor(None, _create_vote)
    
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    
    # Create inline keyboard with vote options
    buttons = []
    for idx, option in enumerate(options):
        buttons.append([InlineKeyboardButton(
            f"{option}",
            callback_data=f"vote_{vote_id}_{idx}"
        )])
    
    # Add result button
    buttons.append([InlineKeyboardButton(
        "📊 查看结果",
        callback_data=f"vote_result_{vote_id}"
    )])
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    message = await update.message.reply_text(
        f"📊 <b>投票：{title}</b>\n\n"
        f"👆 点击下方按钮投票\n"
        f"⏰ 截止时间：7天后\n"
        f"✅ 允许改投",
        reply_markup=keyboard,
        parse_mode='HTML'
    )
    
    # Update vote with message_id
    def _update_message_id():
        with global_flask_app.app_context():
            vote = GroupVote.query.get(vote_id)
            if vote:
                vote.message_id = message.message_id
                db.session.commit()
    
    await asyncio.get_running_loop().run_in_executor(None, _update_message_id)


async def cmd_quiz(update: Update, context):
    """启动问答游戏命令 /quiz"""
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    if not global_flask_app:
        return
    
    def _get_random_quiz():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return None
            
            # Get a random active quiz
            quiz = QuizGame.query.filter_by(
                group_id=group.id,
                is_active=True
            ).order_by(db.func.random()).first()
            
            if quiz:
                return {
                    'id': quiz.id,
                    'question': quiz.question,
                    'answers': json.loads(quiz.answers),
                    'time_limit': quiz.time_limit,
                    'points_reward': quiz.points_reward
                }
            return None
    
    quiz_data = await asyncio.get_running_loop().run_in_executor(None, _get_random_quiz)
    
    if not quiz_data:
        await update.message.reply_text("❌ 暂无可用的问答题目")
        return
    
    # Create quiz session and send question
    buttons = []
    for idx, answer in enumerate(quiz_data['answers']):
        buttons.append([InlineKeyboardButton(
            f"{chr(65+idx)}. {answer}",
            callback_data=f"quiz_answer_{quiz_data['id']}_{idx}"
        )])
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    message = await update.message.reply_text(
        f"🎯 <b>问答题</b>\n\n"
        f"{quiz_data['question']}\n\n"
        f"⏱ 时限：{quiz_data['time_limit']}秒\n"
        f"🎁 奖励：{quiz_data['points_reward']}积分",
        reply_markup=keyboard,
        parse_mode='HTML'
    )
    
    # Create quiz session in database
    def _create_session():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if group:
                session = QuizSession(
                    group_id=group.id,
                    quiz_id=quiz_data['id'],
                    message_id=message.message_id,
                    start_time=datetime.now(),
                    status='active'
                )
                db.session.add(session)
                db.session.commit()
    
    await asyncio.get_running_loop().run_in_executor(None, _create_session)


async def cmd_redpacket(update: Update, context):
    """发红包命令 /redpacket 总积分 数量 [祝福语]"""
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # 解析参数
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text(
                "🧧 <b>发红包</b>\n\n"
                "格式：/redpacket 总积分 红包数量 [祝福语]\n\n"
                "示例：\n"
                "/redpacket 100 10 新年快乐\n"
                "/redpacket 200 5",
                parse_mode='HTML'
            )
            return
        
        total_points = int(args[0])
        packet_count = int(args[1])
        message = ' '.join(args[2:]) if len(args) > 2 else "恭喜发财 🧧"
        
        if total_points < packet_count:
            await update.message.reply_text("❌ 总积分不能少于红包数量")
            return
        
        if packet_count < 1 or packet_count > 50:
            await update.message.reply_text("❌ 红包数量需要在1-50之间")
            return
        
        if total_points < 1:
            await update.message.reply_text("❌ 总积分必须大于0")
            return
        
    except ValueError:
        await update.message.reply_text("❌ 参数格式错误，请输入数字")
        return
    
    if not global_flask_app:
        return
    
    def _create_redpacket():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return None, "群组不存在"
            
            # 检查用户积分
            user_points = UserPoints.query.filter_by(
                group_id=group.id,
                user_id=user.id
            ).first()
            
            if not user_points or user_points.points_balance < total_points:
                current_balance = user_points.points_balance if user_points else 0
                return None, f"积分不足！需要 {total_points} 积分，当前 {current_balance} 积分"
            
            # 扣除积分
            user_points.points_balance -= total_points
            
            # 创建红包
            packet = RedPacket(
                group_id=group.id,
                creator_id=user.id,
                packet_type='random',
                total_points=total_points,
                packet_count=packet_count,
                remaining_count=packet_count,
                remaining_points=total_points,
                message=message,
                expire_time=get_beijing_now() + timedelta(hours=24),
                status='active'
            )
            db.session.add(packet)
            
            # 记录积分日志
            log = PointsLog(
                group_id=group.id,
                user_id=user.id,
                points_change=-total_points,
                reason="发红包",
                balance_after=user_points.points_balance
            )
            db.session.add(log)
            
            db.session.commit()
            return packet.id, None
    
    packet_id, error = await asyncio.get_running_loop().run_in_executor(None, _create_redpacket)
    
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    
    # 发送红包消息
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🧧 领取红包", callback_data=f"redpacket_claim_{packet_id}")
    ]])
    
    await update.message.reply_text(
        f"🧧 <b>红包来啦！</b>\n\n"
        f"💬 {message}\n"
        f"💰 共 {total_points} 积分\n"
        f"🎁 {packet_count} 个红包\n"
        f"⏰ 24小时内有效\n\n"
        f"👆 点击按钮领取",
        reply_markup=keyboard,
        parse_mode='HTML'
    )


async def cmd_rank(update: Update, context):
    """积分排行榜命令 /rank [数量]"""
    chat = update.effective_chat
    
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Parse limit (default 10, max 50)
    limit = 10
    if context.args:
        try:
            limit = int(context.args[0])
            if limit < 1 or limit > 50:
                limit = 10
        except ValueError:
            pass
    
    if not global_flask_app:
        return
    
    def _get_rankings():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return None, "群组不存在"
            
            # Get top users by points
            rankings = db.session.query(
                UserPoints,
                GroupUser
            ).join(
                GroupUser,
                db.and_(
                    UserPoints.group_id == GroupUser.group_id,
                    UserPoints.user_id == GroupUser.tg_id
                )
            ).filter(
                UserPoints.group_id == group.id,
                UserPoints.points_balance > 0
            ).order_by(
                UserPoints.points_balance.desc()
            ).limit(limit).all()
            
            results = []
            for user_points, group_user in rankings:
                # Get user's level badge
                level_badge = ""
                if user_points.current_level:
                    level = MemberLevel.query.get(user_points.current_level_id)
                    if level and level.badge_emoji:
                        level_badge = level.badge_emoji
                
                # Get user profile data
                try:
                    profile = json.loads(group_user.profile_data) if group_user.profile_data else {}
                    name = profile.get('name', f'User_{user_points.user_id}')
                except Exception:
                    name = f'User_{user_points.user_id}'
                
                results.append({
                    'name': name,
                    'points': user_points.points_balance,
                    'badge': level_badge,
                    'user_id': user_points.user_id
                })
            
            return results, None
    
    results, error = await asyncio.get_running_loop().run_in_executor(None, _get_rankings)
    
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    
    if not results:
        await update.message.reply_text("📊 暂无积分排行数据")
        return
    
    # Build ranking message
    msg = f"🏆 <b>积分排行榜 TOP {len(results)}</b>\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for idx, user_data in enumerate(results):
        rank_icon = medals[idx] if idx < 3 else f"{idx + 1}."
        badge = user_data['badge'] + " " if user_data['badge'] else ""
        msg += f"{rank_icon} {badge}{user_data['name']} - {user_data['points']} 积分\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')


async def cmd_active(update: Update, context):
    """活跃排行榜命令 /active [period]"""
    chat = update.effective_chat
    
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Parse period (week/month, default week)
    period = 'week'
    if context.args and context.args[0].lower() in ['week', 'month']:
        period = context.args[0].lower()
    
    if not global_flask_app:
        return
    
    def _get_active_rankings():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return None, "群组不存在"
            
            # Calculate date range
            now = get_beijing_now()
            if period == 'week':
                start_date = (now - timedelta(days=7)).date()
                period_label = "本周"
            else:
                start_date = (now - timedelta(days=30)).date()
                period_label = "本月"
            
            # Get message statistics
            rankings = db.session.query(
                MessageStatistics.user_id,
                db.func.sum(MessageStatistics.message_count).label('total_messages'),
                GroupUser
            ).join(
                GroupUser,
                db.and_(
                    MessageStatistics.group_id == GroupUser.group_id,
                    MessageStatistics.user_id == GroupUser.tg_id
                )
            ).filter(
                MessageStatistics.group_id == group.id,
                MessageStatistics.date >= start_date
            ).group_by(
                MessageStatistics.user_id,
                GroupUser.id
            ).order_by(
                db.text('total_messages DESC')
            ).limit(20).all()
            
            results = []
            for user_id, total_messages, group_user in rankings:
                # Get user profile data
                try:
                    profile = json.loads(group_user.profile_data) if group_user.profile_data else {}
                    name = profile.get('name', f'User_{user_id}')
                except Exception:
                    name = f'User_{user_id}'
                
                results.append({
                    'name': name,
                    'messages': int(total_messages),
                    'user_id': user_id
                })
            
            return {'results': results, 'period_label': period_label}, None
    
    data, error = await asyncio.get_running_loop().run_in_executor(None, _get_active_rankings)
    
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    
    if not data['results']:
        await update.message.reply_text(f"📊 暂无{data['period_label']}活跃数据")
        return
    
    # Build ranking message
    msg = f"📈 <b>{data['period_label']}活跃排行榜 TOP {len(data['results'])}</b>\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for idx, user_data in enumerate(data['results']):
        rank_icon = medals[idx] if idx < 3 else f"{idx + 1}."
        msg += f"{rank_icon} {user_data['name']} - {user_data['messages']} 条消息\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')



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
    
    # 🆕 New member/leave handlers (must come before general message handler)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_member))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_left_chat_member))
    
    # 🆕 Auto-delete system messages (must come early to delete before processing)
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | 
        filters.StatusUpdate.LEFT_CHAT_MEMBER | 
        filters.StatusUpdate.PINNED_MESSAGE,
        handle_auto_delete_messages
    ))
    
    # 🆕 Handle channel messages for pin control
    app.add_handler(MessageHandler(filters.SenderChat.CHANNEL, handle_channel_pin))
    
    # General message handler (processes text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    
    # Callback query handler
    app.add_handler(CallbackQueryHandler(pagination_callback)) 
    
    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    
    # Group management commands (群管理机器人功能)
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("unpin", cmd_unpin))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("userinfo", cmd_userinfo))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("buttons", cmd_menu))  # alias for menu
    
    # 🆕 Points auction commands (积分竞拍命令)
    app.add_handler(CommandHandler("bid", cmd_bid))
    app.add_handler(CommandHandler("auction", cmd_auction))
    
    # 🆕 Interactive features commands (互动功能命令)
    app.add_handler(CommandHandler("vote", cmd_vote))
    app.add_handler(CommandHandler("quiz", cmd_quiz))
    app.add_handler(CommandHandler("redpacket", cmd_redpacket))
    app.add_handler(CommandHandler("lottery_draw", cmd_lottery_draw))
    app.add_handler(CommandHandler("lottery_history", cmd_lottery_history))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("top", cmd_rank))  # alias for rank
    app.add_handler(CommandHandler("active", cmd_active))
    
    # Periodic jobs
    app.job_queue.run_repeating(check_expired_users, interval=EXPIRATION_CHECK_INTERVAL, first=10)
    app.job_queue.run_repeating(check_scheduled_messages, interval=SCHEDULED_MESSAGE_CHECK_INTERVAL, first=15)
    app.job_queue.run_repeating(check_timed_group_control, interval=60, first=20)  # 🆕 Check every minute
    app.job_queue.run_repeating(check_channel_subscriptions, interval=3600, first=30)  # 🆕 Check every hour
    app.job_queue.run_repeating(update_member_levels, interval=1800, first=40)  # 🆕 Update every 30 minutes
    app.job_queue.run_repeating(run_lottery_draws, interval=300, first=50)  # 🆕 Check every 5 minutes
    app.job_queue.run_repeating(check_inactive_users, interval=86400, first=60)  # 🆕 Check inactive users daily
    app.job_queue.run_repeating(check_auction_expiration, interval=300, first=70)  # 🆕 Check auction expiration every 5 minutes
    app.job_queue.run_repeating(check_redpacket_expiration, interval=300, first=80)  # 🆕 Check red packet expiration every 5 minutes
    
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

# 🤖 Group Management Commands (群管理机器人功能)

async def cmd_kick(update: Update, context):
    """踢出群成员命令 /kick"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要踢出的用户消息")
        return
    
    target_user = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(chat.id, target_user.id)
        await context.bot.unban_chat_member(chat.id, target_user.id)
        await update.message.reply_text(f"✅ 已将 {target_user.first_name} 踢出群组")
        
        # Log admin action
        def _log():
            with global_flask_app.app_context():
                group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if group:
                    log_admin_action(
                        group.id,
                        user.id,
                        user.first_name + (f" {user.last_name}" if user.last_name else ""),
                        'kick',
                        target_user.id,
                        target_user.first_name + (f" {target_user.last_name}" if target_user.last_name else "")
                    )
        
        if global_flask_app:
            await asyncio.get_running_loop().run_in_executor(None, _log)
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {str(e)}")

async def cmd_ban(update: Update, context):
    """封禁群成员命令 /ban"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要封禁的用户消息")
        return
    
    target_user = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(chat.id, target_user.id)
        await update.message.reply_text(f"✅ 已将 {target_user.first_name} 封禁")
        
        # Log admin action
        def _log():
            with global_flask_app.app_context():
                group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if group:
                    log_admin_action(
                        group.id,
                        user.id,
                        user.first_name + (f" {user.last_name}" if user.last_name else ""),
                        'ban',
                        target_user.id,
                        target_user.first_name + (f" {target_user.last_name}" if target_user.last_name else "")
                    )
        
        if global_flask_app:
            await asyncio.get_running_loop().run_in_executor(None, _log)
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {str(e)}")

async def cmd_unban(update: Update, context):
    """解封群成员命令 /unban"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要解封的用户消息")
        return
    
    target_user = update.message.reply_to_message.from_user
    try:
        await context.bot.unban_chat_member(chat.id, target_user.id)
        await update.message.reply_text(f"✅ 已将 {target_user.first_name} 解封")
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {str(e)}")

async def cmd_mute(update: Update, context):
    """禁言群成员命令 /mute [时间(分钟)]"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要禁言的用户消息")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    # Parse duration (default: 60 minutes)
    duration = 60
    if context.args:
        try:
            duration = int(context.args[0])
            if duration <= 0:
                duration = 60
        except:
            duration = 60
    
    try:
        # Restrict user from sending messages
        permissions = get_muted_permissions()
        until_date = datetime.now() + timedelta(minutes=duration)
        await context.bot.restrict_chat_member(chat.id, target_user.id, permissions, until_date=until_date)
        await update.message.reply_text(f"✅ 已将 {target_user.first_name} 禁言 {duration} 分钟")
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {str(e)}")

async def cmd_unmute(update: Update, context):
    """解除禁言命令 /unmute"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要解除禁言的用户消息")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    try:
        # Restore default permissions
        permissions = get_unrestricted_permissions()
        await context.bot.restrict_chat_member(chat.id, target_user.id, permissions)
        await update.message.reply_text(f"✅ 已解除 {target_user.first_name} 的禁言")
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {str(e)}")

async def cmd_pin(update: Update, context):
    """置顶消息命令 /pin"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要置顶的消息")
        return
    
    try:
        await context.bot.pin_chat_message(chat.id, update.message.reply_to_message.message_id)
        await update.message.reply_text("✅ 消息已置顶")
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {str(e)}")

async def cmd_unpin(update: Update, context):
    """取消置顶消息命令 /unpin"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    try:
        if update.message.reply_to_message:
            # Unpin specific message
            await context.bot.unpin_chat_message(chat.id, update.message.reply_to_message.message_id)
        else:
            # Unpin all messages
            await context.bot.unpin_all_chat_messages(chat.id)
        await update.message.reply_text("✅ 已取消置顶")
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {str(e)}")

async def cmd_warn(update: Update, context):
    """警告用户命令 /warn [原因]"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ 请回复要警告的用户消息")
        return
    
    target_user = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "违反群规"
    
    warning_text = f"⚠️ 警告\n\n用户: {target_user.first_name}\n原因: {reason}\n\n请遵守群规，避免再次违规！"
    await update.message.reply_text(warning_text)


async def cmd_userinfo(update: Update, context):
    """查询用户详细信息命令 /userinfo"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能使用此命令")
        return
    
    # Check if replying to a message or forwarded message
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = getattr(update.message, 'forward_from', None)
    
    if not target_user:
        await update.message.reply_text("❌ 请回复或转发用户的消息来查看详情")
        return
    
    # Get user info from database
    def _get_user_info():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return None, None, None
            
            group_user = GroupUser.query.filter_by(
                group_id=group.id,
                tg_id=target_user.id
            ).first()
            
            # Get user points if available
            user_points = None
            total_earned = 0
            total_spent = 0
            try:
                user_points = UserPoints.query.filter_by(
                    group_id=group.id,
                    user_id=target_user.id
                ).first()
                
                # Calculate total earned and spent using database aggregation
                from sqlalchemy import func, case
                result = db.session.query(
                    func.sum(case((PointsLog.points_change > 0, PointsLog.points_change), else_=0)).label('earned'),
                    func.sum(case((PointsLog.points_change < 0, func.abs(PointsLog.points_change)), else_=0)).label('spent')
                ).filter(
                    PointsLog.group_id == group.id,
                    PointsLog.user_id == target_user.id
                ).first()
                
                if result:
                    total_earned = result.earned or 0
                    total_spent = result.spent or 0
            except Exception as e:
                print(f"Error getting user points: {e}")
            
            return group_user, user_points, group, total_earned, total_spent
    
    group_user, user_points, group, total_earned, total_spent = await asyncio.get_running_loop().run_in_executor(None, _get_user_info)
    
    # Build user info message
    info_lines = ["👤 用户详细信息\n"]
    info_lines.append(f"━━━━━━━━━━━━━━━━")
    info_lines.append(f"📛 用户名: {target_user.first_name or '-'}")
    if target_user.last_name:
        info_lines.append(f"   姓氏: {target_user.last_name}")
    if target_user.username:
        info_lines.append(f"🔗 Username: @{target_user.username}")
    info_lines.append(f"🆔 用户ID: <code>{target_user.id}</code>")
    info_lines.append(f"🤖 机器人: {'是' if target_user.is_bot else '否'}")
    
    if group_user:
        info_lines.append(f"\n📊 群组信息")
        info_lines.append(f"━━━━━━━━━━━━━━━━")
        
        # Parse profile data
        try:
            profile_data = json.loads(group_user.profile_data or '{}')
            if profile_data:
                for key, value in profile_data.items():
                    info_lines.append(f"   {key}: {value}")
        except:
            pass
        
        if group_user.expiration_date:
            info_lines.append(f"⏰ 到期时间: {group_user.expiration_date.strftime('%Y-%m-%d %H:%M')}")
        
        info_lines.append(f"🚫 封禁状态: {'已封禁' if group_user.is_banned else '正常'}")
        
        if group_user.checkin_time:
            info_lines.append(f"✅ 最后签到: {group_user.checkin_time.strftime('%Y-%m-%d %H:%M')}")
        
        info_lines.append(f"🟢 在线状态: {'在线' if group_user.online else '离线'}")
    
    if user_points:
        info_lines.append(f"\n💰 积分信息")
        info_lines.append(f"━━━━━━━━━━━━━━━━")
        info_lines.append(f"💎 当前积分: {user_points.points_balance}")
        if total_earned > 0 or total_spent > 0:
            info_lines.append(f"📈 总获得: {total_earned}")
            info_lines.append(f"📉 总消耗: {total_spent}")
        
        # 🆕 Show member level badge
        try:
            def _get_member_level():
                with global_flask_app.app_context():
                    levels = MemberLevel.query.filter_by(
                        group_id=group.id
                    ).order_by(MemberLevel.required_points.desc()).all()
                    
                    current_level = None
                    for level in levels:
                        if user_points.points_balance >= level.required_points:
                            current_level = level
                            break
                    return current_level
            
            member_level = await asyncio.get_running_loop().run_in_executor(None, _get_member_level)
            if member_level:
                badge = member_level.badge_emoji or "🎖️"
                info_lines.append(f"{badge} 会员等级: {member_level.level_name}")
        except Exception as e:
            print(f"Error getting member level: {e}")
    
    # Get member info from Telegram
    try:
        member = await context.bot.get_chat_member(chat.id, target_user.id)
        info_lines.append(f"\n👥 群组权限")
        info_lines.append(f"━━━━━━━━━━━━━━━━")
        info_lines.append(f"📌 身份: {member.status}")
        if member.status == 'creator':
            info_lines.append(f"   (群主)")
        elif member.status == 'administrator':
            info_lines.append(f"   (管理员)")
        elif member.status == 'member':
            info_lines.append(f"   (普通成员)")
        elif member.status == 'restricted':
            info_lines.append(f"   (受限用户)")
        elif member.status == 'left':
            info_lines.append(f"   (已退出)")
        elif member.status == 'kicked':
            info_lines.append(f"   (已被移除)")
    except Exception as e:
        print(f"Error getting member info: {e}")
    
    info_text = "\n".join(info_lines)
    await update.message.reply_html(info_text)

async def cmd_menu(update: Update, context):
    """显示群底部按钮菜单命令 /menu 或 /buttons - 使用菜单键盘"""
    chat = update.effective_chat
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Get bottom buttons as reply keyboard (menu keyboard)
    bottom_buttons_markup = await display_bottom_buttons(chat.id, context, use_reply_keyboard=True)
    
    if not bottom_buttons_markup:
        await update.message.reply_text("ℹ️ 该群组暂未设置底部按钮")
        return
    
    # Send message with menu keyboard
    await update.message.reply_text(
        "📋 群组菜单（点击下方按钮使用功能）：",
        reply_markup=bottom_buttons_markup
    )

async def cmd_bid(update: Update, context):
    """积分竞拍出价命令 /bid <auction_id> <amount>"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Parse arguments
    if len(context.args) < 2:
        await update.message.reply_text("❌ 用法: /bid <竞拍ID> <出价金额>\n例如: /bid 1 100")
        return
    
    try:
        auction_id = int(context.args[0])
        bid_amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ 无效的参数，请输入数字")
        return
    
    if bid_amount <= 0:
        await update.message.reply_text("❌ 出价金额必须大于0")
        return
    
    def _process_bid():
        with global_flask_app.app_context():
            try:
                # Use pessimistic locking to prevent race conditions
                group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if not group:
                    return "error", "群组未找到"
                
                # Get auction with row-level lock
                auction = PointsAuction.query.filter_by(
                    id=auction_id,
                    group_id=group.id
                ).with_for_update().first()
                
                if not auction:
                    return "error", "竞拍不存在"
                
                if auction.status != 'active':
                    return "error", "竞拍未激活或已结束"
                
                # Check if auction has ended
                if auction.auction_end and get_beijing_now() > auction.auction_end:
                    auction.status = 'ended'
                    db.session.commit()
                    return "error", "竞拍已结束"
                
                # Check if bid is higher than current
                if bid_amount <= auction.current_bid:
                    return "error", f"出价必须高于当前价格 {auction.current_bid} 积分"
                
                # Get user points with lock
                user_points = UserPoints.query.filter_by(
                    group_id=group.id,
                    user_id=user.id
                ).with_for_update().first()
                
                if not user_points or user_points.points_balance < bid_amount:
                    current_balance = user_points.points_balance if user_points else 0
                    return "error", f"积分不足。当前积分: {current_balance}，需要: {bid_amount}"
                
                # Refund previous bidder if exists
                if auction.current_bidder_id and auction.current_bidder_id != user.id:
                    prev_bidder_points = UserPoints.query.filter_by(
                        group_id=group.id,
                        user_id=auction.current_bidder_id
                    ).with_for_update().first()
                    
                    if prev_bidder_points:
                        prev_bidder_points.points_balance += auction.current_bid
                        
                        # Log the refund
                        points_log = PointsLog(
                            group_id=group.id,
                            user_id=auction.current_bidder_id,
                            points_change=auction.current_bid,
                            reason=f"竞拍退款: {auction.item_name}",
                            balance_after=prev_bidder_points.points_balance
                        )
                        db.session.add(points_log)
                    else:
                        # Previous bidder points record doesn't exist - log error but continue
                        print(f"Warning: Previous bidder {auction.current_bidder_id} has no points record for refund")
                
                # Deduct points from current bidder
                user_points.points_balance -= bid_amount
                
                # Log the bid
                points_log = PointsLog(
                    group_id=group.id,
                    user_id=user.id,
                    points_change=-bid_amount,
                    reason=f"竞拍出价: {auction.item_name}",
                    balance_after=user_points.points_balance
                )
                db.session.add(points_log)
                
                # Update auction
                auction.current_bid = bid_amount
                auction.current_bidder_id = user.id
                
                db.session.commit()
                
                return "success", auction.item_name, bid_amount, user_points.points_balance
            
            except Exception as e:
                db.session.rollback()
                print(f"Error processing bid: {e}")
                return "error", "出价处理失败，请重试"
    
    result = await asyncio.get_running_loop().run_in_executor(None, _process_bid)
    
    if result[0] == "error":
        await update.message.reply_text(f"❌ {result[1]}")
    else:
        _, item_name, bid_amount, remaining_balance = result
        await update.message.reply_text(
            f"✅ 出价成功！\n\n"
            f"📦 物品: {item_name}\n"
            f"💰 出价: {bid_amount} 积分\n"
            f"💎 剩余积分: {remaining_balance}"
        )

async def cmd_auction(update: Update, context):
    """查看当前活跃的竞拍 /auction"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    def _get_auctions():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return []
            
            auctions = PointsAuction.query.filter_by(
                group_id=group.id,
                status='active'
            ).order_by(PointsAuction.auction_end).all()
            
            return auctions
    
    auctions = await asyncio.get_running_loop().run_in_executor(None, _get_auctions)
    
    if not auctions:
        await update.message.reply_text("📭 当前没有进行中的竞拍")
        return
    
    message_lines = ["🏆 当前竞拍列表\n"]
    message_lines.append("━━━━━━━━━━━━━━━━")
    
    for auction in auctions:
        message_lines.append(f"\n📦 ID: {auction.id}")
        message_lines.append(f"   物品: {auction.item_name}")
        if auction.item_description:
            message_lines.append(f"   描述: {auction.item_description}")
        message_lines.append(f"   起拍价: {auction.starting_price} 积分")
        message_lines.append(f"   当前价: {auction.current_bid} 积分")
        if auction.current_bidder_id:
            message_lines.append(f"   领先者: 用户 {auction.current_bidder_id}")
        if auction.auction_end:
            message_lines.append(f"   结束时间: {auction.auction_end.strftime('%Y-%m-%d %H:%M')}")
        message_lines.append(f"\n   💡 出价: /bid {auction.id} <金额>")
    
    await update.message.reply_text("\n".join(message_lines))

async def handle_channel_pin(update: Update, context):
    """处理频道消息自动置顶 - 取消置顶"""
    if not global_flask_app or not update.message:
        return
    
    try:
        msg = update.message
        chat = update.effective_chat
        
        if not chat or chat.type not in ['group', 'supergroup']:
            return
        
        # Check if this is a channel post forwarded to the group
        if not msg.sender_chat or msg.sender_chat.type != 'channel':
            return
        
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return
            
            settings = OtherSettings.query.filter_by(group_id=group.id).first()
            if not settings or not settings.cancel_channel_pin:
                return
            
            # Wait a moment for Telegram to auto-pin the message
            await asyncio.sleep(1)
            
            try:
                # Get pinned messages in the chat
                # If this channel message was just pinned, unpin it
                await context.bot.unpin_chat_message(
                    chat_id=chat.id,
                    message_id=msg.message_id
                )
                print(f"Unpinned channel message {msg.message_id} in chat {chat.id}")
            except Exception as e:
                print(f"Error unpinning channel message: {e}")
                    
    except Exception as e:
        print(f"Error in handle_channel_pin: {e}")

async def cmd_lottery_draw(update: Update, context):
    """手动开奖命令 /lottery_draw <lottery_id>"""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # Check if user is admin
    is_admin = await is_user_admin_in_group(context.bot, chat.id, user.id)
    if not is_admin:
        await update.message.reply_text("❌ 只有管理员才能手动开奖")
        return
    
    # Parse lottery_id
    if len(context.args) < 1:
        await update.message.reply_text("❌ 用法: /lottery_draw <抽奖ID>\n例如: /lottery_draw 1")
        return
    
    try:
        lottery_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ 无效的抽奖ID，请输入数字")
        return
    
    if not global_flask_app:
        return
    
    def _draw_lottery():
        with global_flask_app.app_context():
            try:
                group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                if not group:
                    return "error", "群组未找到"
                
                lottery = GroupLottery.query.filter_by(
                    id=lottery_id,
                    group_id=group.id
                ).first()
                
                if not lottery:
                    return "error", "抽奖不存在"
                
                if lottery.status == 'ended':
                    return "error", "抽奖已经结束"
                
                if lottery.status != 'active':
                    return "error", "抽奖未激活"
                
                # Perform the draw
                winners = []
                
                if lottery.lottery_type == 'message_count':
                    # Weighted random based on message count
                    message_counts = LotteryMessageCount.query.filter_by(
                        lottery_id=lottery.id
                    ).filter(LotteryMessageCount.message_count > 0).order_by(
                        LotteryMessageCount.message_count.desc()
                    ).limit(MAX_LOTTERY_MESSAGE_COUNT_RECORDS).all()
                    
                    if message_counts:
                        participants = [(mc.user_id, mc.message_count) for mc in message_counts]
                        total_messages = sum(count for _, count in participants)
                        
                        if total_messages > 0:
                            rand_val = random.uniform(0, total_messages)
                            cumulative = 0
                            for uid, count in participants:
                                cumulative += count
                                if cumulative >= rand_val:
                                    winners = [uid]
                                    break
                    else:
                        # Fallback to random group user
                        eligible_users = GroupUser.query.filter_by(group_id=group.id).limit(MAX_LOTTERY_PARTICIPANTS).all()
                        if eligible_users:
                            winner = random.choice(eligible_users)
                            winners = [winner.tg_id]
                
                elif lottery.lottery_type == 'message_rank':
                    # Top N senders
                    top_senders = LotteryMessageCount.query.filter_by(
                        lottery_id=lottery.id
                    ).order_by(LotteryMessageCount.message_count.desc()).limit(
                        min(lottery.top_n_winners or 1, MAX_AUCTION_WINNERS)
                    ).all()
                    
                    winners = [mc.user_id for mc in top_senders]
                    
                    if not winners:
                        # Fallback
                        top_users = GroupUser.query.filter_by(group_id=group.id).limit(
                            min(lottery.top_n_winners or 1, MAX_AUCTION_WINNERS)
                        ).all()
                        winners = [u.tg_id for u in top_users]
                
                # Update lottery
                lottery.winner_ids = json.dumps(winners)
                lottery.status = 'ended'
                db.session.commit()
                
                return "success", winners, lottery.lottery_name, lottery.prize_description
            
            except Exception as e:
                db.session.rollback()
                print(f"Error drawing lottery: {e}")
                traceback.print_exc()
                return "error", f"开奖失败: {str(e)}"
    
    result = await asyncio.get_running_loop().run_in_executor(None, _draw_lottery)
    
    if result[0] == "error":
        await update.message.reply_text(f"❌ {result[1]}")
    else:
        _, winners, lottery_name, prize_desc = result
        
        if winners:
            winner_mentions = [f"<a href='tg://user?id={uid}'>用户{uid}</a>" for uid in winners]
            message = f"🎉 <b>抽奖结束！</b>\n\n"
            message += f"活动：{lottery_name}\n"
            message += f"获奖者：{', '.join(winner_mentions)}\n"
            if prize_desc:
                message += f"奖品：{prize_desc}\n"
            
            await update.message.reply_text(message, parse_mode='HTML')
        else:
            await update.message.reply_text("⚠️ 开奖完成，但未找到符合条件的获奖者")


async def cmd_lottery_history(update: Update, context):
    """查看抽奖历史命令 /lottery_history"""
    chat = update.effective_chat
    
    # Only work in groups
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    if not global_flask_app:
        return
    
    def _get_lottery_history():
        with global_flask_app.app_context():
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return []
            
            # Get recent ended lotteries
            lotteries = GroupLottery.query.filter_by(
                group_id=group.id,
                status='ended'
            ).order_by(GroupLottery.end_time.desc()).limit(10).all()
            
            return lotteries
    
    lotteries = await asyncio.get_running_loop().run_in_executor(None, _get_lottery_history)
    
    if not lotteries:
        await update.message.reply_text("📭 暂无抽奖历史记录")
        return
    
    message_lines = ["🏆 抽奖历史记录\n"]
    message_lines.append("━━━━━━━━━━━━━━━━")
    
    for lottery in lotteries:
        message_lines.append(f"\n📌 {lottery.lottery_name}")
        message_lines.append(f"   类型: {'消息数量抽奖' if lottery.lottery_type == 'message_count' else '消息排名抽奖'}")
        if lottery.prize_description:
            message_lines.append(f"   奖品: {lottery.prize_description}")
        if lottery.end_time:
            message_lines.append(f"   结束时间: {lottery.end_time.strftime('%Y-%m-%d %H:%M')}")
        
        # Parse winners
        try:
            winner_ids = json.loads(lottery.winner_ids or '[]')
            if winner_ids:
                winner_mentions = [f"<a href='tg://user?id={uid}'>用户{uid}</a>" for uid in winner_ids[:5]]
                message_lines.append(f"   获奖者: {', '.join(winner_mentions)}")
                if len(winner_ids) > 5:
                    message_lines.append(f"   （共{len(winner_ids)}位获奖者）")
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"Error parsing winner_ids for lottery history: {e}")
    
    await update.message.reply_text("\n".join(message_lines), parse_mode='HTML')


async def check_auction_expiration(context):
    """Background task to check and notify auction expiration"""
    if not global_flask_app:
        return
    
    try:
        def _get_expired_auctions():
            with global_flask_app.app_context():
                now = get_beijing_now()
                # Find active auctions that have expired
                return PointsAuction.query.filter(
                    PointsAuction.status == 'active',
                    PointsAuction.auction_end <= now
                ).all()
        
        auctions = await asyncio.get_running_loop().run_in_executor(None, _get_expired_auctions)
        
        for auction in auctions:
            try:
                with global_flask_app.app_context():
                    group = BotGroup.query.get(auction.group_id)
                    if not group:
                        continue
                    
                    chat_id = int(group.chat_id)
                    
                    # Update auction status
                    auction.status = 'ended'
                    db.session.commit()
                    
                    # Notify winner
                    if auction.current_bidder_id and auction.current_bid > 0:
                        message = (
                            f"🎉 <b>竞拍结束！</b>\n\n"
                            f"📦 物品: {auction.item_name}\n"
                            f"💰 成交价: {auction.current_bid} 积分\n"
                            f"👤 获胜者: <a href='tg://user?id={auction.current_bidder_id}'>用户{auction.current_bidder_id}</a>\n\n"
                        )
                        if auction.item_description:
                            message += f"📝 描述: {auction.item_description}\n"
                        message += "请获胜者联系管理员领取物品！"
                    else:
                        message = (
                            f"📭 <b>竞拍结束</b>\n\n"
                            f"📦 物品: {auction.item_name}\n"
                            f"⚠️ 无人出价，流拍"
                        )
                    
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode='HTML'
                    )
                    
            except Exception as e:
                print(f"Error notifying auction end {auction.id}: {e}")
                traceback.print_exc()
                
    except Exception as e:
        print(f"Error in check_auction_expiration: {e}")
        traceback.print_exc()


async def check_redpacket_expiration(context):
    """Background task to check and expire red packets"""
    if not global_flask_app:
        return
    
    try:
        def _get_expired_packets():
            with global_flask_app.app_context():
                now = get_beijing_now()
                # Find active red packets that have expired
                return RedPacket.query.filter(
                    RedPacket.status == 'active',
                    RedPacket.expire_time <= now
                ).all()
        
        packets = await asyncio.get_running_loop().run_in_executor(None, _get_expired_packets)
        
        for packet in packets:
            try:
                with global_flask_app.app_context():
                    # Refund remaining points to creator
                    if packet.remaining_points > 0:
                        group = BotGroup.query.get(packet.group_id)
                        if group:
                            user_points = UserPoints.query.filter_by(
                                group_id=group.id,
                                user_id=packet.creator_id
                            ).first()
                            
                            if not user_points:
                                user_points = UserPoints(
                                    group_id=group.id,
                                    user_id=packet.creator_id,
                                    points_balance=0
                                )
                                db.session.add(user_points)
                            
                            user_points.points_balance += packet.remaining_points
                            
                            # Log the refund
                            log = PointsLog(
                                group_id=group.id,
                                user_id=packet.creator_id,
                                points_change=packet.remaining_points,
                                reason="红包过期退款",
                                balance_after=user_points.points_balance
                            )
                            db.session.add(log)
                    
                    # Update packet status
                    packet.status = 'expired'
                    db.session.commit()
                    
            except Exception as e:
                db.session.rollback()
                print(f"Error expiring red packet {packet.id}: {e}")
                traceback.print_exc()
                
    except Exception as e:
        print(f"Error in check_redpacket_expiration: {e}")
        traceback.print_exc()


async def quiz_answer_callback(update: Update, context):
    """处理问答答案选择"""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    
    # 解析回调数据: quiz_answer_{quiz_id}_{answer_idx}
    try:
        _, _, quiz_id, answer_idx = query.data.split('_')
        quiz_id = int(quiz_id)
        answer_idx = int(answer_idx)
    except:
        await query.answer("❌ 无效的选择")
        return
    
    if not global_flask_app:
        return
    
    def _process_answer():
        with global_flask_app.app_context():
            # 1. 获取问题和会话
            quiz = QuizGame.query.get(quiz_id)
            if not quiz:
                return None, "问题不存在"
            
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group:
                return None, "群组不存在"
            
            session = QuizSession.query.filter_by(
                group_id=group.id,
                quiz_id=quiz_id,
                status='active'
            ).first()
            
            if not session:
                return None, "问答已结束"
            
            # 2. 检查是否已回答
            existing = QuizAnswer.query.filter_by(
                session_id=session.id,
                user_id=user.id
            ).first()
            
            if existing:
                return None, "您已经回答过了"
            
            # 3. 检查是否超时
            if get_beijing_now() > session.start_time + timedelta(seconds=quiz.time_limit):
                session.status = 'ended'
                db.session.commit()
                return None, "回答超时"
            
            # 4. 验证答案
            is_correct = (answer_idx == quiz.correct_answer_index)
            points = quiz.points_reward if is_correct else 0
            
            # 5. 记录答案
            answer = QuizAnswer(
                session_id=session.id,
                user_id=user.id,
                answer_index=answer_idx,
                is_correct=is_correct,
                points_awarded=points
            )
            db.session.add(answer)
            
            # 6. 更新用户积分
            if is_correct and points > 0:
                user_points = UserPoints.query.filter_by(
                    group_id=group.id,
                    user_id=user.id
                ).first()
                
                if not user_points:
                    user_points = UserPoints(
                        group_id=group.id,
                        user_id=user.id,
                        points_balance=0
                    )
                    db.session.add(user_points)
                
                user_points.points_balance += points
                
                # 记录积分日志
                log = PointsLog(
                    group_id=group.id,
                    user_id=user.id,
                    points_change=points,
                    reason=f"答对问答题",
                    balance_after=user_points.points_balance
                )
                db.session.add(log)
            
            db.session.commit()
            
            return {
                'is_correct': is_correct,
                'points': points,
                'explanation': quiz.explanation
            }, None
    
    result, error = await asyncio.get_running_loop().run_in_executor(None, _process_answer)
    
    if error:
        await query.answer(f"❌ {error}")
        return
    
    if result['is_correct']:
        msg = f"✅ 回答正确！\n🎁 获得 {result['points']} 积分"
    else:
        msg = "❌ 回答错误"
    
    if result.get('explanation'):
        msg += f"\n\n💡 {result['explanation']}"
    
    await query.answer(msg, show_alert=True)

async def redpacket_claim_callback(update: Update, context):
    """处理红包领取"""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    
    try:
        packet_id = int(query.data.split('_')[2])
    except:
        await query.answer("❌ 无效的红包")
        return
    
    if not global_flask_app:
        return
    
    def _claim_packet():
        with global_flask_app.app_context():
            import random
            
            # 获取红包
            packet = RedPacket.query.get(packet_id)
            if not packet:
                return None, "红包不存在"
            
            if packet.status != 'active':
                return None, "红包已过期"
            
            if packet.remaining_count <= 0:
                return None, "红包已被抢完"
            
            group = BotGroup.query.get(packet.group_id)
            if not group or str(chat.id) != group.chat_id:
                return None, "群组不匹配"
            
            # 检查是否已领取
            existing = RedPacketClaim.query.filter_by(
                packet_id=packet_id,
                user_id=user.id
            ).first()
            
            if existing:
                return None, "您已经领取过了"
            
            # 计算分配积分（随机或平均）
            if packet.packet_type == 'random':
                # 拼手气红包：最后一个人拿剩余的，其他人随机
                if packet.remaining_count == 1:
                    points = packet.remaining_points
                else:
                    # 随机范围：1 到 (剩余积分 / 剩余数量 * 2)
                    max_points = int(packet.remaining_points / packet.remaining_count * 2)
                    points = random.randint(1, max(1, max_points))
            else:
                # 普通红包：平均分配
                points = packet.remaining_points // packet.remaining_count
            
            # 更新红包状态
            packet.remaining_count -= 1
            packet.remaining_points -= points
            
            if packet.remaining_count == 0:
                packet.status = 'claimed'
            
            # 记录领取
            claim = RedPacketClaim(
                packet_id=packet_id,
                user_id=user.id,
                points_received=points
            )
            db.session.add(claim)
            
            # 更新用户积分
            user_points = UserPoints.query.filter_by(
                group_id=group.id,
                user_id=user.id
            ).first()
            
            if not user_points:
                user_points = UserPoints(
                    group_id=group.id,
                    user_id=user.id,
                    points_balance=0
                )
                db.session.add(user_points)
            
            user_points.points_balance += points
            
            # 记录积分日志
            log = PointsLog(
                group_id=group.id,
                user_id=user.id,
                points_change=points,
                reason="领取红包",
                balance_after=user_points.points_balance
            )
            db.session.add(log)
            
            db.session.commit()
            
            return {
                'points': points,
                'remaining': packet.remaining_count
            }, None
    
    result, error = await asyncio.get_running_loop().run_in_executor(None, _claim_packet)
    
    if error:
        await query.answer(f"❌ {error}")
        return
    
    await query.answer(
        f"✅ 领取成功！获得 {result['points']} 积分\n"
        f"剩余 {result['remaining']} 个红包",
        show_alert=True
    )

async def vote_callback(update: Update, context):
    """处理投票回调"""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    
    # Parse callback data: vote_{vote_id}_{option_idx} or vote_result_{vote_id}
    try:
        parts = query.data.split('_')
        if parts[1] == 'result':
            # Show vote results
            vote_id = int(parts[2])
            return await show_vote_results(query, user, chat, vote_id)
        else:
            # Cast vote
            vote_id = int(parts[1])
            option_idx = int(parts[2])
            return await cast_vote(query, user, chat, vote_id, option_idx)
    except (IndexError, ValueError):
        await query.answer("❌ 无效的投票数据")
        return


async def cast_vote(query, user, chat, vote_id, option_idx):
    """处理用户投票"""
    if not global_flask_app:
        return
    
    def _process_vote():
        with global_flask_app.app_context():
            # Get vote
            vote = GroupVote.query.get(vote_id)
            if not vote:
                return None, "投票不存在"
            
            if vote.status != 'active':
                return None, "投票已结束"
            
            # Check if vote ended
            if vote.end_time and get_beijing_now() > vote.end_time:
                vote.status = 'ended'
                db.session.commit()
                return None, "投票已结束"
            
            # Get options
            options = json.loads(vote.options)
            if option_idx < 0 or option_idx >= len(options):
                return None, "无效的选项"
            
            # Check if user already voted
            existing = VoteRecord.query.filter_by(
                vote_id=vote_id,
                user_id=user.id
            ).first()
            
            if existing:
                if not vote.allow_revote:
                    return None, "您已经投过票了"
                # Update existing vote
                existing.choices = json.dumps([option_idx])
                existing.updated_at = get_beijing_now()
            else:
                # Create new vote record
                record = VoteRecord(
                    vote_id=vote_id,
                    user_id=user.id,
                    choices=json.dumps([option_idx])
                )
                db.session.add(record)
            
            db.session.commit()
            return {'option': options[option_idx], 'updated': existing is not None}, None
    
    result, error = await asyncio.get_running_loop().run_in_executor(None, _process_vote)
    
    if error:
        await query.answer(f"❌ {error}")
        return
    
    action = "已改投" if result['updated'] else "投票成功"
    await query.answer(f"✅ {action}：{result['option']}")


async def show_vote_results(query, user, chat, vote_id):
    """显示投票结果"""
    if not global_flask_app:
        return
    
    def _get_results():
        with global_flask_app.app_context():
            vote = GroupVote.query.get(vote_id)
            if not vote:
                return None, "投票不存在"
            
            options = json.loads(vote.options)
            
            # Count votes for each option
            vote_counts = [0] * len(options)
            total_votes = 0
            
            records = VoteRecord.query.filter_by(vote_id=vote_id).all()
            for record in records:
                choices = json.loads(record.choices)
                for choice in choices:
                    if 0 <= choice < len(options):
                        vote_counts[choice] += 1
                        total_votes += 1
            
            return {
                'title': vote.title,
                'options': options,
                'counts': vote_counts,
                'total': total_votes,
                'status': vote.status
            }, None
    
    result, error = await asyncio.get_running_loop().run_in_executor(None, _get_results)
    
    if error:
        await query.answer(f"❌ {error}")
        return
    
    # Build results message
    msg = f"📊 <b>{result['title']}</b>\n\n"
    msg += f"总投票数：{result['total']}\n\n"
    
    for idx, (option, count) in enumerate(zip(result['options'], result['counts'])):
        percentage = (count / result['total'] * 100) if result['total'] > 0 else 0
        bar_length = int(percentage / 5)  # 20 chars max
        bar = "█" * bar_length + "░" * (20 - bar_length)
        msg += f"{option}\n{bar} {count} 票 ({percentage:.1f}%)\n\n"
    
    if result['status'] == 'ended':
        msg += "⏰ 投票已结束"
    
    await query.answer()
    await query.message.reply_text(msg, parse_mode='HTML')

async def display_bottom_buttons(chat_id, context, use_reply_keyboard=False):
    """显示群底部按钮
    
    Args:
        chat_id: 群组ID
        context: 上下文
        use_reply_keyboard: 是否使用回复键盘（菜单键盘），默认False（使用内联键盘）
    
    Returns:
        ReplyKeyboardMarkup 或 InlineKeyboardMarkup
    """
    if not global_flask_app:
        return None
    
    try:
        def _get_buttons():
            with global_flask_app.app_context():
                group = BotGroup.query.filter_by(chat_id=str(chat_id)).first()
                if not group:
                    return []
                
                buttons = GroupBottomButton.query.filter_by(
                    group_id=group.id,
                    is_active=True
                ).order_by(GroupBottomButton.row_position, GroupBottomButton.button_order).all()
                
                return buttons
        
        buttons = await asyncio.get_running_loop().run_in_executor(None, _get_buttons)
        
        if not buttons:
            return None
        
        if use_reply_keyboard:
            # Build reply keyboard (menu keyboard) - only button text, no URLs
            keyboard = []
            current_row = []
            current_row_num = buttons[0].row_position  # Safe because we checked buttons is not empty
            
            # Get the input field placeholder from the first button that has one
            input_placeholder = None
            for button in buttons:
                if button.input_field_placeholder:
                    input_placeholder = button.input_field_placeholder
                    break
            
            for button in buttons:
                # Start a new row if row_position changes
                if button.row_position != current_row_num:
                    if current_row:
                        keyboard.append(current_row)
                    current_row = []
                    current_row_num = button.row_position
                
                # Reply keyboard buttons don't support URLs, just text
                current_row.append(KeyboardButton(button.button_text))
            
            # Add the last row
            if current_row:
                keyboard.append(current_row)
            
            if keyboard:
                return ReplyKeyboardMarkup(
                    keyboard, 
                    resize_keyboard=True,
                    one_time_keyboard=False,
                    input_field_placeholder=input_placeholder
                )
            return None
        else:
            # Build inline keyboard (original behavior)
            keyboard = []
            current_row = []
            current_row_num = buttons[0].row_position  # Safe because we checked buttons is not empty
            
            for button in buttons:
                # Start a new row if row_position changes
                if button.row_position != current_row_num:
                    if current_row:
                        keyboard.append(current_row)
                    current_row = []
                    current_row_num = button.row_position
                
                if button.button_url:
                    current_row.append(InlineKeyboardButton(
                        button.button_text,
                        url=button.button_url
                    ))
                elif button.button_callback:
                    current_row.append(InlineKeyboardButton(
                        button.button_text,
                        callback_data=button.button_callback
                    ))
            
            # Add the last row
            if current_row:
                keyboard.append(current_row)
            
            if keyboard:
                return InlineKeyboardMarkup(keyboard)
            
            return None
        
    except Exception as e:
        print(f"Error displaying bottom buttons: {e}")
        return None

async def cmd_start(update: Update, context):
    print(f"✅ /start 命令被触发，用户 ID: {update.effective_user.id}")
    user_id = update.effective_user.id
    chat = update.effective_chat
    admin_id = safe_int(os.getenv('ADMIN_ID', 0))
    
    print(f"📍 /start 调试: chat_type={chat.type}, chat_id={chat.id}, user_id={user_id}")
    
    # /start command should only work in private chat, not in groups
    if chat.type in ['group', 'supergroup']:
        print(f"⚠️ /start 调试: 在群组中使用 /start 命令，忽略")
        return
    
    # Handle private chat only
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
        # Get custom private start message and check for expired memberships
        def _get_private_start_msg_and_check_expiration():
            with global_flask_app.app_context():
                # Try to get configuration from the most recently updated group (bot-level setting)
                group = BotGroup.query.filter_by(is_active=True).order_by(BotGroup.updated_at.desc()).first()
                conf = get_group_conf(group) if group else DEFAULT_SYSTEM.copy()
                private_msg = conf.get('msg_private_start', DEFAULT_SYSTEM['msg_private_start'])
                
                # Check if this user has any expired memberships in groups
                now = get_beijing_now()
                expired_memberships = GroupUser.query.options(
                    joinedload(GroupUser.group)
                ).filter(
                    GroupUser.tg_id == user_id,
                    GroupUser.expiration_date.isnot(None),
                    GroupUser.expiration_date < now,
                    GroupUser.is_banned == False
                ).all()
                
                # Collect expired memberships to process
                users_to_ban = []
                for group_user in expired_memberships:
                    if group_user.group and group_user.group.is_active:
                        # Store group_user.id for later database update
                        users_to_ban.append({
                            'group_user_id': group_user.id,
                            'tg_id': group_user.tg_id,
                            'group_chat_id': group_user.group.chat_id,
                            'group_id': group_user.group.id,
                            'group_title': group_user.group.title
                        })
                
                # Return data WITHOUT marking as banned - will update after successful API call
                return private_msg, users_to_ban
        
        private_msg, users_to_ban = await asyncio.get_running_loop().run_in_executor(None, _get_private_start_msg_and_check_expiration)
        
        # Helper function to mark user as banned in database after successful API call
        def _mark_user_banned_in_db(group_user_id):
            """Mark user as banned in database - called after successful API mute"""
            with global_flask_app.app_context():
                try:
                    group_user = GroupUser.query.get(group_user_id)
                    if group_user:
                        group_user.is_banned = True
                        db.session.commit()
                        return True
                except Exception as e:
                    print(f"❌ [/start] Failed to mark user {group_user_id} as banned in DB: {e}", flush=True)
                    db.session.rollback()
                    return False
            return False
        
        # Mute expired users in their groups
        if users_to_ban:
            async def mute_expired_user(user_data):
                """Mute an expired user in a group"""
                try:
                    group_user_id = user_data['group_user_id']
                    tg_id = user_data['tg_id']
                    group_chat_id = user_data['group_chat_id']
                    group_id = user_data['group_id']
                    group_title = user_data['group_title']
                    
                    # Convert chat_id to integer for Telegram API
                    chat_id_int = convert_chat_id_to_int(group_chat_id, group_id, group_title)
                    if chat_id_int is None:
                        print(f"❌ [/start] Failed to convert chat_id for group {group_id} ({group_title}), chat_id={group_chat_id}", flush=True)
                        return
                    
                    try:
                        await context.bot.restrict_chat_member(
                            chat_id=chat_id_int,
                            user_id=tg_id,
                            permissions=get_muted_permissions()
                        )
                        print(f"✅ [/start] Successfully called restrict_chat_member API - Muted expired user {tg_id} in group {group_title} (chat_id={chat_id_int})", flush=True)
                        
                        # ✅ NEW: Only mark as banned in DB after successful API call
                        success = await asyncio.get_running_loop().run_in_executor(None, _mark_user_banned_in_db, group_user_id)
                        if success:
                            print(f"✅ [/start] Marked user {tg_id} as banned in database after successful API call", flush=True)
                        else:
                            print(f"⚠️  [/start] API succeeded but failed to update database for user {tg_id}", flush=True)
                            
                    except Exception as restrict_error:
                        # 详细记录 restrict_chat_member API 调用失败的错误
                        print(f"❌ [/start] restrict_chat_member API failed for user {tg_id} in group {group_title} (chat_id={chat_id_int})", flush=True)
                        print(f"   Error type: {type(restrict_error).__name__}", flush=True)
                        print(f"   Error details: {str(restrict_error)}", flush=True)
                        print(f"   Traceback: {traceback.format_exc()}", flush=True)
                        print(f"   ⚠️  Database NOT updated - user will be retried on next check", flush=True)
                        # Don't re-raise to avoid duplicate logging in outer exception handler
                        return
                except Exception as e:
                    print(f"❌ [/start] Unexpected error muting user {user_data['tg_id']} in group {user_data['group_chat_id']} (chat_id type: {type(user_data['group_chat_id']).__name__})", flush=True)
                    print(f"   Error: {type(e).__name__}: {str(e)}", flush=True)
                    print(f"   Full traceback: {traceback.format_exc()}", flush=True)
            
            # Use semaphore to limit concurrent operations
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_BANS)
            
            async def mute_with_limit(user_data):
                async with semaphore:
                    await mute_expired_user(user_data)
            
            # Mute users in all their expired groups concurrently
            await asyncio.gather(*[mute_with_limit(user_data) for user_data in users_to_ban], return_exceptions=True)
            
            # Notify the user about expired memberships
            expired_groups = [user_data['group_title'] for user_data in users_to_ban]
            notification_msg = f"⚠️ <b>注意</b>\n\n您在以下群组的认证已过期，已被暂时禁言：\n• " + "\n• ".join(expired_groups) + "\n\n请联系管理员续费。"
            # Sanitize the notification message before sending
            sanitized_notification = sanitize_html_for_telegram(notification_msg)
            await update.message.reply_html(sanitized_notification)
        
        await update.message.reply_html(private_msg)


async def on_my_chat_member(update: Update, context):
    """处理机器人被添加到群组事件，仅注册群组信息，不发送通知消息"""
    try:
        chat = update.effective_chat
        new_member = update.my_chat_member.new_chat_member
        old_member = update.my_chat_member.old_chat_member
        status = new_member.status
        old_status = old_member.status if old_member else None
        user = update.effective_user
        
        print(f"📍 on_my_chat_member: chat={chat.title}, type={chat.type}, new_status={status}, old_status={old_status}")
        
        if chat.type in ['group', 'supergroup']:
            # 处理机器人被添加到群组 (从 left/kicked 变为 member/administrator)
            if status in ['administrator', 'member'] and old_status in ['left', 'kicked', None]:
                # 使用全局 App Context
                with global_flask_app.app_context():
                    g = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                    if not g:
                        # 新群组，创建记录
                        g = BotGroup(chat_id=str(chat.id), title=chat.title, type=chat.type, is_active=True)
                        g.fields_config = json.dumps(DEFAULT_FIELDS, ensure_ascii=False)
                        db.session.add(g)
                        db.session.commit()
                        print(f"➕ 新群组注册: {chat.title} (chat_id: {chat.id})")
                    else:
                        # 已存在的群组，确保激活并更新标题
                        g.is_active = True
                        g.title = chat.title
                        g.type = chat.type
                        db.session.commit()
                        print(f"🔄 群组已重新激活: {chat.title} (chat_id: {chat.id})")
                    
                print(f"✅ 机器人已添加到群组 {chat.title}，群组已注册")
            
            # 处理机器人被移出群组 (从 member/administrator 变为 left/kicked)
            elif status in ['left', 'kicked'] and old_status in ['administrator', 'member']:
                with global_flask_app.app_context():
                    g = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
                    if g:
                        g.is_active = False
                        db.session.commit()
                        print(f"⛔️ 机器人已被移出群组 {chat.title}，群组已停用")
    except Exception as e:
        import traceback
        print(f"Error in on_my_chat_member: {e}")
        traceback.print_exc()

async def on_message(update: Update, context):
    if not global_flask_app: return
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not msg.text or not chat: return

        txt = msg.text.strip()
        
        # 🆕 Check spam protection first (may delete message and return early)
        if chat.type in ['group', 'supergroup']:
            spam_detected = await check_spam_protection(update, context)
            if spam_detected:
                return  # Message was deleted, stop processing
            
            # 🆕 Check keyword filter (may delete message and return early)
            keyword_filtered = await check_keyword_filter(update, context)
            if keyword_filtered:
                return  # Message was deleted, stop processing
            
            # 🆕 Check and mute expired users immediately
            await check_and_mute_expired_user(update, context)
            
            # 🆕 Update user last activity time
            await update_user_activity(update, context)
            
            # 🆕 Track message statistics
            await track_message_statistics(update, context)
            
            # 🆕 Track user name changes
            await track_user_name_change(update, context)
            
            # 🆕 Sync messages to other groups
            await handle_sync_group_messages(update, context)
        
        # Check if this is a verification code from admin in private chat
        if chat.type == 'private':
            # 🆕 Handle forwarded messages - show detailed user info
            # Use getattr to safely check for forward_from attribute (may not exist in non-forwarded messages)
            try:
                forwarded_user = getattr(msg, 'forward_from', None)
                if forwarded_user:
                    info_lines = ["🔍 转发消息详细信息\n"]
                    info_lines.append("━━━━━━━━━━━━━━━━")
                    info_lines.append(f"📛 用户名: {forwarded_user.first_name or '-'}")
                    if forwarded_user.last_name:
                        info_lines.append(f"   姓氏: {forwarded_user.last_name}")
                    if forwarded_user.username:
                        info_lines.append(f"🔗 Username: @{forwarded_user.username}")
                    info_lines.append(f"🆔 用户ID: <code>{forwarded_user.id}</code>")
                    info_lines.append(f"🤖 机器人: {'是' if forwarded_user.is_bot else '否'}")
                    
                    # Try to get user's group membership info
                    def _get_forwarded_user_info():
                        with global_flask_app.app_context():
                            # Find all groups where this user is a member
                            group_users = GroupUser.query.filter_by(tg_id=forwarded_user.id).all()
                            results = []
                            for gu in group_users:
                                group = BotGroup.query.get(gu.group_id)
                                if group:
                                    user_points = UserPoints.query.filter_by(
                                        group_id=group.id,
                                        user_id=forwarded_user.id
                                    ).first()
                                    
                                    results.append({
                                        'group_name': group.title,
                                        'banned': gu.is_banned,
                                        'expiration': gu.expiration_date,
                                        'points': user_points.points_balance if user_points else 0
                                    })
                            return results
                    
                    user_groups = await asyncio.get_running_loop().run_in_executor(None, _get_forwarded_user_info)
                    
                    if user_groups:
                        info_lines.append(f"\n📊 群组信息 ({len(user_groups)}个群)")
                        info_lines.append("━━━━━━━━━━━━━━━━")
                        for idx, info in enumerate(user_groups[:5], 1):  # Show max 5 groups
                            info_lines.append(f"\n{idx}. {info['group_name']}")
                            info_lines.append(f"   状态: {'🚫 已封禁' if info['banned'] else '✅ 正常'}")
                            if info['expiration']:
                                info_lines.append(f"   到期: {info['expiration'].strftime('%Y-%m-%d %H:%M')}")
                            info_lines.append(f"   积分: {info['points']}")
                        
                        if len(user_groups) > 5:
                            info_lines.append(f"\n... 及其他 {len(user_groups) - 5} 个群组")
                    
                    await msg.reply_text("\n".join(info_lines), parse_mode='HTML')
                    return
            except Exception as e:
                print(f"Error handling forwarded message: {e}")
                # Continue to verification code check even if forward message handling fails
            
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
                else:
                    # result is None - code didn't match any pending session
                    await msg.reply_html("❌ <b>验证码无效</b>\n\n请检查验证码是否正确，或重新发送 /start 获取新的验证码。")
                    return

        # 使用全局 App Context
        with global_flask_app.app_context():
            # 1. 自动点赞
            group = BotGroup.query.filter_by(chat_id=str(chat.id)).first()
            if not group or not group.is_active:
                return
            
            conf = get_group_conf(group)
            
            # 🆕 Award points for messages (if rule exists and user is registered)
            if chat.type in ['group', 'supergroup']:
                message_rule = PointsRule.query.filter_by(
                    group_id=group.id,
                    rule_type='message',
                    is_active=True
                ).first()
                
                if message_rule:
                    db_user = GroupUser.query.filter_by(group_id=group.id, tg_id=user.id).first()
                    if db_user:
                        user_points = UserPoints.query.filter_by(
                            group_id=group.id,
                            user_id=user.id
                        ).first()
                        
                        if not user_points:
                            user_points = UserPoints(
                                group_id=group.id,
                                user_id=user.id,
                                points_balance=0
                            )
                            db.session.add(user_points)
                        
                        user_points.points_balance += message_rule.points_amount
                        
                        # Log the points transaction
                        points_log = PointsLog(
                            group_id=group.id,
                            user_id=user.id,
                            points_change=message_rule.points_amount,
                            reason="发送消息",
                            balance_after=user_points.points_balance
                        )
                        db.session.add(points_log)
                        db.session.commit()
            
            # 🆕 Track messages for active lotteries (optimized for large groups)
            # ✅ Track ALL group members, not just verified users (unverified users can participate)
            if chat.type in ['group', 'supergroup']:
                # Track for both message_count and message_rank lotteries
                # Limit to 10 concurrent active lotteries to prevent performance issues in large groups
                active_lotteries = GroupLottery.query.filter_by(
                    group_id=group.id,
                    status='active'
                ).filter(GroupLottery.lottery_type.in_(['message_count', 'message_rank'])).limit(10).all()
                
                # Track for ALL users (verified and unverified)
                if active_lotteries:
                    now = get_beijing_now()
                    for lottery in active_lotteries:
                        # Only track if lottery is still within its time window
                        if lottery.start_time and lottery.end_time:
                            if lottery.start_time <= now <= lottery.end_time:
                                # Get or create message count record
                                msg_count = LotteryMessageCount.query.filter_by(
                                    lottery_id=lottery.id,
                                    user_id=user.id
                                ).first()
                                
                                if not msg_count:
                                    msg_count = LotteryMessageCount(
                                        lottery_id=lottery.id,
                                        group_id=group.id,
                                        user_id=user.id,
                                        message_count=0
                                    )
                                    db.session.add(msg_count)
                                
                                msg_count.message_count += 1
                                msg_count.updated_at = now
                    
                    # Batch commit all lottery tracking updates
                    try:
                        db.session.commit()
                    except Exception as e:
                        print(f"Error committing lottery tracking: {e}")
                        db.session.rollback()
            
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
                        # Always try to mute, even if already marked as banned (in case API failed before)
                        try:
                            print(f"🔄 [打卡功能] 准备禁言过期用户 {user.id} in group {chat.id}", flush=True)
                            # Mute with comprehensive restrictions
                            await context.bot.restrict_chat_member(
                                chat_id=chat.id,
                                user_id=user.id,
                                permissions=get_muted_permissions()
                            )
                            print(f"✅ [打卡功能] Successfully called restrict_chat_member API - Muted expired user {user.id} in group {chat.id}", flush=True)
                            
                            # ✅ NEW: Only mark as banned in DB after successful API call
                            if not db_user.is_banned:
                                db_user.is_banned = True
                                db.session.commit()
                                print(f"✅ [打卡功能] Marked user {user.id} as banned in database after successful API call", flush=True)
                                
                        except Exception as restrict_error:
                            print(f"❌ [打卡功能] restrict_chat_member API failed for user {user.id} in group {chat.id}", flush=True)
                            print(f"   Error type: {type(restrict_error).__name__}", flush=True)
                            print(f"   Error details: {str(restrict_error)}", flush=True)
                            print(f"   Traceback: {traceback.format_exc()}", flush=True)
                            print(f"   ⚠️  Database NOT updated - user will be retried on next check", flush=True)
                        
                        # 🆕 Send ephemeral message in group (visible only to that user)
                        msg_text = sanitize_html_for_telegram(conf.get('msg_expired_ban', '⛔️ 您的认证已过期，已被暂时禁言。请联系管理员续费。'))
                        try:
                            # Send a reply that will be auto-deleted
                            warning_msg = await msg.reply_html(msg_text)
                            # Auto-delete after 30 seconds
                            context.job_queue.run_once(
                                lambda c: c.job.data.delete(),
                                30,
                                data=warning_msg
                            )
                        except Exception as e:
                            print(f"Failed to send expiration notification: {e}")
                        return  # Stop processing, don't allow check-in
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
                            
                            # 🆕 Award points for check-in
                            checkin_rule = PointsRule.query.filter_by(
                                group_id=group.id,
                                rule_type='checkin',
                                is_active=True
                            ).first()
                            
                            if checkin_rule:
                                user_points = UserPoints.query.filter_by(
                                    group_id=group.id,
                                    user_id=user.id
                                ).first()
                                
                                if not user_points:
                                    user_points = UserPoints(
                                        group_id=group.id,
                                        user_id=user.id,
                                        points_balance=0
                                    )
                                    db.session.add(user_points)
                                
                                user_points.points_balance += checkin_rule.points_amount
                                
                                # Log the points transaction
                                points_log = PointsLog(
                                    group_id=group.id,
                                    user_id=user.id,
                                    points_change=checkin_rule.points_amount,
                                    reason="每日打卡",
                                    balance_after=user_points.points_balance
                                )
                                db.session.add(points_log)
                                db.session.commit()
                            
                            msg_text = sanitize_html_for_telegram(conf.get('msg_checkin_success', '打卡成功'))
                            r = await msg.reply_html(msg_text)
                            del_time = safe_int(conf.get('checkin_del_time'), 0)
                            if del_time > 0:
                                context.job_queue.run_once(lambda c: c.job.data.delete(), del_time, data=r)
                return

            # 3. 自动回复检查 (Check points-based first, then regular auto-reply)
            if conf.get('auto_reply_open', True):
                # 3.1 先检查积分自动回复 (Check Points-based Auto-Reply first)
                # 🆕 Support multiple keywords (comma-separated)
                points_reply = None
                all_points_replies = PointsAutoReply.query.filter_by(
                    group_id=group.id,
                    is_active=True
                ).all()
                
                for pr in all_points_replies:
                    # Filter out empty keywords after stripping
                    keywords = [k.strip() for k in pr.trigger_keyword.split(',') if k.strip()]
                    if txt in keywords:
                        points_reply = pr
                        break
                
                if points_reply and points_reply.points_cost > 0:
                    # Points-based content found - handle it
                    try:
                        # Check user points
                        user_points = UserPoints.query.filter_by(
                            group_id=group.id,
                            user_id=user.id
                        ).first()
                        
                        if not user_points or user_points.points_balance < points_reply.points_cost:
                            current_balance = user_points.points_balance if user_points else 0
                            await msg.reply_text(
                                f"❌ 积分不足！\n"
                                f"需要: {points_reply.points_cost} 积分\n"
                                f"当前: {current_balance} 积分"
                            )
                        else:
                            try:
                                # Deduct points and log in a transaction
                                user_points.points_balance -= points_reply.points_cost
                                
                                # Log the transaction
                                points_log = PointsLog(
                                    group_id=group.id,
                                    user_id=user.id,
                                    points_change=-points_reply.points_cost,
                                    reason=f"查看内容: {txt}",
                                    balance_after=user_points.points_balance
                                )
                                db.session.add(points_log)
                                db.session.commit()
                                
                                # Send the content only after successful commit
                                content = sanitize_html_for_telegram(points_reply.content or '')
                                
                                if points_reply.media_type == 'image' and points_reply.media_url:
                                    await msg.reply_photo(
                                        photo=points_reply.media_url,
                                        caption=content,
                                        parse_mode='HTML'
                                    )
                                elif points_reply.media_type == 'video' and points_reply.media_url:
                                    await msg.reply_video(
                                        video=points_reply.media_url,
                                        caption=content,
                                        parse_mode='HTML'
                                    )
                                elif content:
                                    await msg.reply_html(
                                        content,
                                        link_preview_options=LinkPreviewOptions(is_disabled=True)
                                    )
                                
                                # Notify about deduction
                                await msg.reply_text(
                                    f"💎 已扣除 {points_reply.points_cost} 积分\n"
                                    f"剩余: {user_points.points_balance} 积分"
                                )
                            except Exception as e:
                                # Rollback on any database error
                                db.session.rollback()
                                print(f"Points deduction error: {e}")
                                await msg.reply_text("❌ 积分扣除失败，请重试")
                    except Exception as e:
                        print(f"Points-based auto reply error: {e}")
                    # Don't check regular auto-reply if points-based was triggered
                else:
                    # 3.2 No points-based reply found, check regular auto-reply
                    # 🆕 Support multiple keywords (comma-separated)
                    auto_reply = None
                    all_auto_replies = AutoReply.query.filter_by(
                        group_id=group.id,
                        is_active=True
                    ).all()
                    
                    for ar in all_auto_replies:
                        # Filter out empty keywords after stripping
                        keywords = [k.strip() for k in ar.trigger_keyword.split(',') if k.strip()]
                        if txt in keywords:
                            auto_reply = ar
                            break
                    
                    if auto_reply:
                        try:
                            # 构建按钮
                            buttons = []
                            try:
                                links = json.loads(auto_reply.links or '[]')
                                buttons = build_inline_keyboard_from_links(links)
                            except:
                                pass
                            
                            # 🆕 Add bottom buttons from group settings
                            bottom_buttons_markup = await display_bottom_buttons(chat.id, context)
                            if bottom_buttons_markup:
                                # Merge bottom buttons with auto-reply buttons
                                buttons.extend(bottom_buttons_markup.inline_keyboard)
                            
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
                                    link_preview_options=LinkPreviewOptions(is_disabled=True)
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
                # Continue to check for query functionality
            
            # 3.3 检查群底按钮触发关键词
            try:
                # 查找有触发关键词的按钮
                buttons_with_keywords = GroupBottomButton.query.filter_by(
                    group_id=group.id,
                    is_active=True
                ).filter(GroupBottomButton.trigger_keyword.isnot(None)).all()
                
                for btn in buttons_with_keywords:
                    # Filter out empty keywords after stripping
                    keywords = [k.strip() for k in btn.trigger_keyword.split(',') if k.strip()]
                    if txt in keywords:
                        # Build inline keyboard with this button (only if it has URL or callback)
                        keyboard = []
                        if btn.button_url:
                            keyboard.append([InlineKeyboardButton(
                                btn.button_text,
                                url=btn.button_url
                            )])
                        elif btn.button_callback:
                            keyboard.append([InlineKeyboardButton(
                                btn.button_text,
                                callback_data=btn.button_callback
                            )])
                        
                        # Only send if button has valid URL or callback
                        if keyboard:
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            await msg.reply_text(
                                "📋 群组按钮：",
                                reply_markup=reply_markup
                            )
                            break  # Only show first matched button
            except Exception as e:
                print(f"Button keyword trigger error: {e}")
            
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
                    sent = await msg.reply_html(text_resp, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
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
    """统一的回调处理器 - 根据callback data路由到不同的处理函数"""
    query = update.callback_query
    
    if query.data == "noop": 
        return await query.answer()
    
    # 路由到不同的处理器
    if query.data.startswith('quiz_answer_'):
        return await quiz_answer_callback(update, context)
    elif query.data.startswith('redpacket_claim_'):
        return await redpacket_claim_callback(update, context)
    elif query.data.startswith('vote_'):
        return await vote_callback(update, context)
    
    # 默认处理分页查询
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
            await query.edit_message_text(text=text, parse_mode='HTML', reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception as e: 
        print(f"Page Error: {e}")
    await query.answer()
