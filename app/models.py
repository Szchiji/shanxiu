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
    last_activity = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)  # Track last message activity
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


class GroupEntryExitSettings(db.Model):
    """进退群设置"""
    __tablename__ = 'group_entry_exit_settings'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    # 进群验证
    entry_verification_enabled = db.Column(db.Boolean, default=False)
    verification_question = db.Column(db.Text, nullable=True)
    verification_answer = db.Column(db.Text, nullable=True)
    verification_timeout = db.Column(db.Integer, default=60)  # 验证超时时间(秒)
    # 进群欢迎
    welcome_enabled = db.Column(db.Boolean, default=False)
    welcome_message = db.Column(db.Text, nullable=True)
    welcome_media_type = db.Column(db.String(20), default='text')
    welcome_media_url = db.Column(db.Text, nullable=True)
    # 退群拉黑
    exit_ban_enabled = db.Column(db.Boolean, default=False)
    exit_ban_duration = db.Column(db.Integer, default=0)  # 0=永久
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='entry_exit_settings', lazy=True)


class SpamProtection(db.Model):
    """垃圾防护"""
    __tablename__ = 'spam_protection'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    enabled = db.Column(db.Boolean, default=False)
    # 防护规则
    max_messages_per_minute = db.Column(db.Integer, default=10)
    block_links = db.Column(db.Boolean, default=False)
    block_forwards = db.Column(db.Boolean, default=False)
    block_stickers = db.Column(db.Boolean, default=False)
    # 惩罚措施
    punishment_type = db.Column(db.String(20), default='mute')  # mute, kick, ban
    punishment_duration = db.Column(db.Integer, default=60)  # 分钟
    # 白名单
    whitelist_users = db.Column(db.Text, default='[]')  # JSON array of user IDs
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='spam_protection', lazy=True)


class TimedGroupControl(db.Model):
    """定时开关群"""
    __tablename__ = 'timed_group_control'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    enabled = db.Column(db.Boolean, default=False)
    open_time = db.Column(db.Time, nullable=True)  # 开群时间
    close_time = db.Column(db.Time, nullable=True)  # 关群时间
    timezone = db.Column(db.String(50), default='Asia/Shanghai')
    close_message = db.Column(db.Text, nullable=True)  # 关群提示消息
    open_message = db.Column(db.Text, nullable=True)  # 开群提示消息
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='timed_group_control', lazy=True)


class InvitationActivity(db.Model):
    """邀请活动"""
    __tablename__ = 'invitation_activity'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    enabled = db.Column(db.Boolean, default=False)
    reward_points = db.Column(db.Integer, default=10)  # 每邀请一人获得的积分
    minimum_invites = db.Column(db.Integer, default=1)  # 最少邀请人数
    activity_start = db.Column(db.DateTime, nullable=True)
    activity_end = db.Column(db.DateTime, nullable=True)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='invitation_activity', lazy=True)


class ForcedChannelSubscription(db.Model):
    """强制订阅频道"""
    __tablename__ = 'forced_channel_subscription'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    enabled = db.Column(db.Boolean, default=False)
    channel_id = db.Column(db.String(50), nullable=True)  # 必须订阅的频道ID
    channel_username = db.Column(db.String(255), nullable=True)  # 频道用户名
    check_interval = db.Column(db.Integer, default=3600)  # 检查间隔(秒)
    unsubscribe_action = db.Column(db.String(20), default='kick')  # kick, ban, mute
    verification_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='forced_channel_subscription', lazy=True)


class PointsRule(db.Model):
    """积分规则"""
    __tablename__ = 'points_rules'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    rule_name = db.Column(db.String(255), nullable=False)
    rule_type = db.Column(db.String(50), nullable=False)  # checkin, message, invite, etc.
    points_amount = db.Column(db.Integer, default=1)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='points_rules', lazy=True)


class PointsAutoReply(db.Model):
    """积分自动回复"""
    __tablename__ = 'points_auto_reply'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    trigger_keyword = db.Column(db.String(255), nullable=False)
    points_cost = db.Column(db.Integer, default=0)  # 消耗积分
    content = db.Column(db.Text, nullable=True)
    media_type = db.Column(db.String(20), default='text')
    media_url = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='points_auto_reply', lazy=True)


class PointsAuction(db.Model):
    """积分竞拍"""
    __tablename__ = 'points_auction'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    item_name = db.Column(db.String(255), nullable=False)
    item_description = db.Column(db.Text, nullable=True)
    starting_price = db.Column(db.Integer, default=100)
    current_bid = db.Column(db.Integer, default=0)
    current_bidder_id = db.Column(db.BigInteger, nullable=True)
    auction_start = db.Column(db.DateTime, nullable=True)
    auction_end = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, active, ended
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='points_auction', lazy=True)


class PointsLog(db.Model):
    """积分日志"""
    __tablename__ = 'points_log'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    user_id = db.Column(db.BigInteger, index=True)
    points_change = db.Column(db.Integer, nullable=False)  # 正数=获得，负数=消耗
    reason = db.Column(db.String(255), nullable=True)
    balance_after = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    group = db.relationship('BotGroup', backref='points_log', lazy=True)


class UserPoints(db.Model):
    """用户积分"""
    __tablename__ = 'user_points'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    user_id = db.Column(db.BigInteger, index=True)
    points_balance = db.Column(db.Integer, default=0)
    current_level_id = db.Column(db.Integer, db.ForeignKey('member_level.id'), nullable=True)  # Track current member level
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (db.UniqueConstraint('group_id', 'user_id', name='_group_user_points_uc'),)
    
    group = db.relationship('BotGroup', backref='user_points', lazy=True)
    current_level = db.relationship('MemberLevel', backref='users_at_level', lazy=True)


class GroupLottery(db.Model):
    """群抽奖"""
    __tablename__ = 'group_lottery'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    lottery_name = db.Column(db.String(255), nullable=False)
    lottery_type = db.Column(db.String(50), nullable=False)  # message_count, message_rank
    prize_description = db.Column(db.Text, nullable=True)
    # 发言数量抽奖
    min_messages = db.Column(db.Integer, default=10)  # 最少发言数
    # 发言排行抽奖
    top_n_winners = db.Column(db.Integer, default=3)  # 前N名获奖
    # 通用设置
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    winner_ids = db.Column(db.Text, default='[]')  # JSON array of winner user IDs
    status = db.Column(db.String(20), default='pending')  # pending, active, ended
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='group_lottery', lazy=True)


class MemberLevel(db.Model):
    """群成员等级"""
    __tablename__ = 'member_level'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    level_name = db.Column(db.String(255), nullable=False)
    required_points = db.Column(db.Integer, default=0)
    permissions = db.Column(db.Text, default='{}')  # JSON格式的权限配置
    badge_emoji = db.Column(db.String(10), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='member_level', lazy=True)


class UserNameChange(db.Model):
    """用户改名监控"""
    __tablename__ = 'user_name_change'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    user_id = db.Column(db.BigInteger, index=True)
    old_name = db.Column(db.String(255), nullable=True)
    new_name = db.Column(db.String(255), nullable=True)
    changed_at = db.Column(db.DateTime, default=datetime.now)
    
    group = db.relationship('BotGroup', backref='user_name_change', lazy=True)


class GroupBottomButton(db.Model):
    """群底部按钮"""
    __tablename__ = 'group_bottom_button'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    button_text = db.Column(db.String(255), nullable=False)
    button_url = db.Column(db.Text, nullable=True)
    button_callback = db.Column(db.String(255), nullable=True)  # Callback data for inline button
    trigger_keyword = db.Column(db.String(255), nullable=True)  # 触发关键词，支持逗号分隔多个
    input_field_placeholder = db.Column(db.String(255), nullable=True)  # 输入框提示文案（显示在群输入框）
    button_order = db.Column(db.Integer, default=0)  # 显示顺序
    row_position = db.Column(db.Integer, default=0)  # 行号，同一行的按钮会并排显示
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='group_bottom_button', lazy=True)


class SyncGroupMessages(db.Model):
    """同步群消息"""
    __tablename__ = 'sync_group_messages'
    id = db.Column(db.Integer, primary_key=True)
    source_group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    target_group_id = db.Column(db.String(50), nullable=False)  # 目标群组ID
    enabled = db.Column(db.Boolean, default=False)
    sync_media = db.Column(db.Boolean, default=True)  # 是否同步媒体文件
    sync_forwards = db.Column(db.Boolean, default=True)  # 是否同步转发消息
    filter_keywords = db.Column(db.Text, default='[]')  # JSON array of keywords to filter
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='sync_group_messages', lazy=True, foreign_keys=[source_group_id])


class SyncMessageLog(db.Model):
    """同步消息日志"""
    __tablename__ = 'sync_message_logs'
    id = db.Column(db.Integer, primary_key=True)
    source_group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    target_group_id = db.Column(db.String(50), nullable=False)
    source_message_id = db.Column(db.BigInteger, nullable=True)
    target_message_id = db.Column(db.BigInteger, nullable=True)
    user_id = db.Column(db.BigInteger, nullable=True)  # 消息发送者ID
    username = db.Column(db.String(255), nullable=True)  # 消息发送者用户名
    message_type = db.Column(db.String(20), default='text')  # text, photo, video, document, etc.
    content_preview = db.Column(db.Text, nullable=True)  # 内容预览（前100字符）
    status = db.Column(db.String(20), default='success')  # success, failed, filtered
    error_message = db.Column(db.Text, nullable=True)  # 错误信息（如果同步失败）
    synced_at = db.Column(db.DateTime, default=datetime.now, index=True)
    
    group = db.relationship('BotGroup', backref='sync_message_logs', lazy=True, foreign_keys=[source_group_id])


class OtherSettings(db.Model):
    """其他设置"""
    __tablename__ = 'other_settings'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    # 自动删除消息开关
    auto_delete_join_msg = db.Column(db.Boolean, default=False)  # 自动删除进群消息
    auto_delete_leave_msg = db.Column(db.Boolean, default=False)  # 自动删除退群消息
    auto_delete_promote_msg = db.Column(db.Boolean, default=False)  # 自动删除互推消息
    auto_delete_pin_msg = db.Column(db.Boolean, default=False)  # 自动删除置顶提示消息
    cancel_channel_pin = db.Column(db.Boolean, default=False)  # 取消频道消息置顶
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='other_settings', lazy=True)


class BotClone(db.Model):
    """机器人克隆"""
    __tablename__ = 'bot_clones'
    id = db.Column(db.Integer, primary_key=True)
    clone_name = db.Column(db.String(255), nullable=False)  # 克隆机器人名称
    bot_token = db.Column(db.String(255), nullable=False)  # Bot Token (removed unique constraint for flexibility)
    owner_user_id = db.Column(db.BigInteger, nullable=True)  # 克隆机器人拥有者的用户ID
    admin_user_ids = db.Column(db.Text, default='[]')  # 管理员用户ID列表，JSON格式
    is_active = db.Column(db.Boolean, default=True)  # 是否启用
    expiration_date = db.Column(db.DateTime, nullable=True)  # 有效期
    webhook_url = db.Column(db.String(500), nullable=True)  # Webhook URL
    description = db.Column(db.Text, nullable=True)  # 描述
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

class LotteryMessageCount(db.Model):
    """抽奖消息计数 - 跟踪用户在抽奖期间发送的消息数"""
    __tablename__ = 'lottery_message_count'
    id = db.Column(db.Integer, primary_key=True)
    lottery_id = db.Column(db.Integer, db.ForeignKey('group_lottery.id'), index=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    user_id = db.Column(db.BigInteger, nullable=False)  # Telegram user ID
    message_count = db.Column(db.Integer, default=0)  # Number of messages sent
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    __table_args__ = (
        db.UniqueConstraint('lottery_id', 'user_id', name='_lottery_user_uc'),
        db.Index('ix_lottery_message_count_lookup', 'lottery_id', 'group_id', 'user_id'),
    )
    
    lottery = db.relationship('GroupLottery', backref='message_counts', lazy=True)
    group = db.relationship('BotGroup', backref='lottery_message_counts', lazy=True)


class InactiveUserSettings(db.Model):
    """不活跃用户设置"""
    __tablename__ = 'inactive_user_settings'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    enabled = db.Column(db.Boolean, default=False)
    inactivity_days = db.Column(db.Integer, default=30)  # 不活跃天数阈值
    action_type = db.Column(db.String(20), default='kick')  # kick, ban, mute
    check_interval = db.Column(db.Integer, default=86400)  # 检查间隔(秒)，默认24小时
    warning_enabled = db.Column(db.Boolean, default=False)  # 是否提前警告
    warning_days = db.Column(db.Integer, default=7)  # 提前警告天数
    warning_message = db.Column(db.Text, nullable=True)  # 警告消息
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='inactive_user_settings', lazy=True)


class KeywordFilter(db.Model):
    """关键词过滤"""
    __tablename__ = 'keyword_filter'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    keyword = db.Column(db.String(255), nullable=False)
    filter_type = db.Column(db.String(20), default='blacklist')  # blacklist, whitelist
    match_type = db.Column(db.String(20), default='contains')  # contains, exact, regex
    action = db.Column(db.String(20), default='delete')  # delete, warn, mute, kick, ban
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='keyword_filters', lazy=True)


class MessageStatistics(db.Model):
    """消息统计"""
    __tablename__ = 'message_statistics'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    user_id = db.Column(db.BigInteger, index=True)
    date = db.Column(db.Date, index=True, default=datetime.now)
    message_count = db.Column(db.Integer, default=0)
    text_count = db.Column(db.Integer, default=0)
    photo_count = db.Column(db.Integer, default=0)
    video_count = db.Column(db.Integer, default=0)
    sticker_count = db.Column(db.Integer, default=0)
    document_count = db.Column(db.Integer, default=0)
    voice_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    __table_args__ = (
        db.UniqueConstraint('group_id', 'user_id', 'date', name='_group_user_date_uc'),
    )
    
    group = db.relationship('BotGroup', backref='message_statistics', lazy=True)


class GroupVote(db.Model):
    """群投票"""
    __tablename__ = 'group_vote'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    options = db.Column(db.Text, default='[]')  # JSON array of options
    vote_type = db.Column(db.String(20), default='single')  # single, multiple
    max_choices = db.Column(db.Integer, default=1)  # 多选时最多选择数
    is_anonymous = db.Column(db.Boolean, default=False)
    allow_revote = db.Column(db.Boolean, default=True)  # 允许改投
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, active, ended
    message_id = db.Column(db.BigInteger, nullable=True)  # 投票消息ID
    created_by = db.Column(db.BigInteger, nullable=True)  # 创建者用户ID
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='group_votes', lazy=True)


class VoteRecord(db.Model):
    """投票记录"""
    __tablename__ = 'vote_record'
    id = db.Column(db.Integer, primary_key=True)
    vote_id = db.Column(db.Integer, db.ForeignKey('group_vote.id'), index=True)
    user_id = db.Column(db.BigInteger, index=True)
    choices = db.Column(db.Text, default='[]')  # JSON array of option indices
    voted_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    __table_args__ = (
        db.UniqueConstraint('vote_id', 'user_id', name='_vote_user_uc'),
    )
    
    vote = db.relationship('GroupVote', backref='vote_records', lazy=True)


class QuizGame(db.Model):
    """问答游戏"""
    __tablename__ = 'quiz_game'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    question = db.Column(db.Text, nullable=False)
    answers = db.Column(db.Text, default='[]')  # JSON array of answers
    correct_answer_index = db.Column(db.Integer, nullable=False)
    explanation = db.Column(db.Text, nullable=True)  # 答案解析
    points_reward = db.Column(db.Integer, default=10)  # 答对奖励积分
    time_limit = db.Column(db.Integer, default=60)  # 答题时限(秒)
    difficulty = db.Column(db.String(20), default='medium')  # easy, medium, hard
    category = db.Column(db.String(50), nullable=True)  # 题目分类
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='quiz_games', lazy=True)


class QuizSession(db.Model):
    """问答会话"""
    __tablename__ = 'quiz_session'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey('quiz_game.id'), index=True)
    message_id = db.Column(db.BigInteger, nullable=True)  # 问题消息ID
    start_time = db.Column(db.DateTime, default=datetime.now)
    end_time = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='active')  # active, ended
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    group = db.relationship('BotGroup', backref='quiz_sessions', lazy=True)
    quiz = db.relationship('QuizGame', backref='quiz_sessions', lazy=True)


class QuizAnswer(db.Model):
    """问答答案记录"""
    __tablename__ = 'quiz_answer'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('quiz_session.id'), index=True)
    user_id = db.Column(db.BigInteger, index=True)
    answer_index = db.Column(db.Integer, nullable=False)
    is_correct = db.Column(db.Boolean, default=False)
    points_awarded = db.Column(db.Integer, default=0)
    answered_at = db.Column(db.DateTime, default=datetime.now)
    
    __table_args__ = (
        db.UniqueConstraint('session_id', 'user_id', name='_session_user_uc'),
    )
    
    session = db.relationship('QuizSession', backref='quiz_answers', lazy=True)


class RedPacket(db.Model):
    """红包"""
    __tablename__ = 'red_packet'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('bot_groups.id'), index=True)
    creator_id = db.Column(db.BigInteger, index=True)  # 发红包的用户
    packet_type = db.Column(db.String(20), default='random')  # random=拼手气, equal=普通
    total_points = db.Column(db.Integer, nullable=False)  # 总积分
    packet_count = db.Column(db.Integer, nullable=False)  # 红包数量
    remaining_count = db.Column(db.Integer, nullable=False)  # 剩余数量
    remaining_points = db.Column(db.Integer, nullable=False)  # 剩余积分
    message = db.Column(db.Text, nullable=True)  # 红包祝福语
    message_id = db.Column(db.BigInteger, nullable=True)  # 红包消息ID
    expire_time = db.Column(db.DateTime, nullable=True)  # 过期时间
    status = db.Column(db.String(20), default='active')  # active, expired, claimed
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    group = db.relationship('BotGroup', backref='red_packets', lazy=True)


class RedPacketClaim(db.Model):
    """红包领取记录"""
    __tablename__ = 'red_packet_claim'
    id = db.Column(db.Integer, primary_key=True)
    packet_id = db.Column(db.Integer, db.ForeignKey('red_packet.id'), index=True)
    user_id = db.Column(db.BigInteger, index=True)
    points_received = db.Column(db.Integer, nullable=False)
    claimed_at = db.Column(db.DateTime, default=datetime.now)
    
    __table_args__ = (
        db.UniqueConstraint('packet_id', 'user_id', name='_packet_user_uc'),
    )
    
    packet = db.relationship('RedPacket', backref='red_packet_claims', lazy=True)


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
