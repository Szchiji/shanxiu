from . import db
from datetime import datetime
import json

class BotGroup(db.Model):
    __tablename__ = 'bot_groups'
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.String(50), unique=True, index=True)
    title = db.Column(db.String(255))
    type = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)
    config = db.Column(db.Text, default='{}')
    fields_config = db.Column(db.Text)
    last_query_msg_id = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

class GroupUser(db.Model):
    __tablename__ = 'group_users'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    tg_id = db.Column(db.BigInteger)
    profile_data = db.Column(db.Text, default='{}')
    expiration_date = db.Column(db.DateTime, nullable=True)  # Consider adding composite index: (expiration_date, is_banned)
    is_banned = db.Column(db.Boolean, default=False)
    checkin_time = db.Column(db.DateTime)
    online = db.Column(db.Boolean, default=False)
    __table_args__ = (db.UniqueConstraint('group_id', 'tg_id', name='_group_user_uc'),)
    
    # Relationship to BotGroup for efficient querying
    group = db.relationship('BotGroup', backref='users', lazy=True)

class AuthSession(db.Model):
    __tablename__ = 'auth_sessions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.BigInteger, index=True)
    session_token = db.Column(db.String(100), unique=True, index=True)
    verification_code = db.Column(db.String(10))
    is_verified = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    expires_at = db.Column(db.DateTime)

class AutoReply(db.Model):
    """自动回复规则"""
    __tablename__ = 'auto_replies'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    trigger_keyword = db.Column(db.String(255), nullable=False)  # 触发关键词
    media_type = db.Column(db.String(20), default='text')  # text, image, video
    media_url = db.Column(db.Text, nullable=True)  # 多媒体链接
    content = db.Column(db.Text, nullable=True)  # 富文本内容
    links = db.Column(db.Text, default='[]')  # JSON格式的链接数组
    delete_after = db.Column(db.Integer, default=0)  # 删除上一条消息的时间(秒)，0表示不删除
    remark = db.Column(db.Text, nullable=True)  # 备注
    is_active = db.Column(db.Boolean, default=True)  # 是否启用
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='auto_replies', lazy=True)


class ScheduledMessage(db.Model):
    """定时消息"""
    __tablename__ = 'scheduled_messages'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    media_type = db.Column(db.String(20), default='text')  # text, image, video
    media_url = db.Column(db.Text, nullable=True)  # 多媒体链接
    content = db.Column(db.Text, nullable=True)  # 富文本内容
    links = db.Column(db.Text, default='[]')  # JSON格式的链接数组
    repeat_interval = db.Column(db.Integer, default=0)  # 重复间隔(分钟)，0表示不重复
    delete_previous = db.Column(db.Boolean, default=False)  # 是否删除上一条
    last_message_id = db.Column(db.BigInteger, nullable=True)  # 上一条消息ID，用于删除
    start_time = db.Column(db.DateTime, nullable=True)  # 开始时间
    stop_time = db.Column(db.DateTime, nullable=True)  # 停止时间
    remark = db.Column(db.Text, nullable=True)  # 备注
    is_active = db.Column(db.Boolean, default=True)  # 是否启用
    last_sent_at = db.Column(db.DateTime, nullable=True)  # 上次发送时间
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='scheduled_messages', lazy=True)


class StartMessage(db.Model):
    """自定义 /start 消息"""
    __tablename__ = 'start_messages'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    message_type = db.Column(db.String(20), default='user')  # 'user' or 'admin'
    media_type = db.Column(db.String(20), default='text')  # text, image, video
    media_url = db.Column(db.Text, nullable=True)  # 多媒体链接
    content = db.Column(db.Text, nullable=True)  # 富文本内容
    links = db.Column(db.Text, default='[]')  # JSON格式的链接按钮数组
    is_active = db.Column(db.Boolean, default=True)  # 是否启用
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='start_messages', lazy=True)


DEFAULT_FIELDS = [
    {"key": "name", "label": "昵称", "type": "text"},
    {"key": "region", "label": "地区", "type": "select", "options": ["福田","南山"]},
]

DEFAULT_SYSTEM = {
    "checkin_open": True, "checkin_cmd": "打卡", 
    "query_open": True, "query_cmd": "查询", # 🆕 普通查询开关
    "query_filter_open": True,             # 🆕 筛选查询开关
    "checkin_del_time": 30, 
    "query_del_time": 60,
    "page_size": 10,
    "auto_like": True, "like_emoji": "❤️",
    "auto_reply_open": True,  # 自动回复开关
    "scheduled_msg_open": True,  # 定时消息开关
    "start_msg_open": True,  # /start 消息开关
    "push_channel_id": "",
    "msg_checkin_success": "✅ <b>打卡成功！</b>", 
    "msg_not_registered": "⚠️ <b>未认证用户</b>",
    "msg_repeat_checkin": "🔄 <b>今天已打卡</b>", 
    "msg_query_header": "🔍 <b>今日在线用户：</b>\n",
    "msg_filter_header": "🔍 <b>筛选结果：</b>\n",
    "msg_expired_ban": "⛔️ <b>您的认证已过期，已被暂时禁言。请联系管理员续费。</b>",
    "template": "{onlineEmoji} {昵称} | {地区}",
    "push_template": "<b>👤 名片推送</b>\n昵称：{昵称}\n<a href='tg://user?id={tg_id}'>联系我</a>",
    "custom_buttons": "[]" # 🆕 初始化为空数组
}
