# -*- encoding: utf-8 -*-
"""
Django models for the app
"""

from django.db import models
from django.utils import timezone
import json


class BotGroup(models.Model):
    """群组信息"""
    chat_id = models.CharField(max_length=50, unique=True, db_index=True)
    title = models.CharField(max_length=255, blank=True, null=True)
    type = models.CharField(max_length=50, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    config = models.TextField(default='{}')
    fields_config = models.TextField(blank=True, null=True)
    last_query_msg_id = models.IntegerField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bot_groups'
        verbose_name = '群组'
        verbose_name_plural = '群组'

    def __str__(self):
        return self.title or self.chat_id


class GroupUser(models.Model):
    """群用户"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='users')
    tg_id = models.BigIntegerField(db_index=True)
    profile_data = models.TextField(default='{}')
    expiration_date = models.DateTimeField(blank=True, null=True)
    is_banned = models.BooleanField(default=False)
    checkin_time = models.DateTimeField(blank=True, null=True)
    last_activity = models.DateTimeField(auto_now=True)
    online = models.BooleanField(default=False)

    class Meta:
        db_table = 'group_users'
        unique_together = [['group', 'tg_id']]
        verbose_name = '群用户'
        verbose_name_plural = '群用户'

    def __str__(self):
        return f"{self.tg_id} in {self.group}"


class AuthSession(models.Model):
    """认证会话"""
    user_id = models.BigIntegerField(db_index=True)
    session_token = models.CharField(max_length=100, unique=True, db_index=True)
    verification_code = models.CharField(max_length=10)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'auth_sessions'
        verbose_name = '认证会话'
        verbose_name_plural = '认证会话'


class AutoReply(models.Model):
    """自动回复规则"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='auto_replies')
    trigger_keyword = models.CharField(max_length=255)
    media_type = models.CharField(max_length=20, default='text')
    media_url = models.TextField(blank=True, null=True)
    content = models.TextField(blank=True, null=True)
    links = models.TextField(default='[]')
    delete_after = models.IntegerField(default=0)
    remark = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'auto_replies'
        verbose_name = '自动回复'
        verbose_name_plural = '自动回复'


class ScheduledMessage(models.Model):
    """定时消息"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='scheduled_messages')
    media_type = models.CharField(max_length=20, default='text')
    media_url = models.TextField(blank=True, null=True)
    content = models.TextField(blank=True, null=True)
    links = models.TextField(default='[]')
    repeat_interval = models.IntegerField(default=0)
    delete_previous = models.BooleanField(default=False)
    last_message_id = models.BigIntegerField(blank=True, null=True)
    start_time = models.DateTimeField(blank=True, null=True)
    stop_time = models.DateTimeField(blank=True, null=True)
    remark = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    last_sent_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'scheduled_messages'
        verbose_name = '定时消息'
        verbose_name_plural = '定时消息'


class StartMessage(models.Model):
    """自定义 /start 消息"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='start_messages')
    message_type = models.CharField(max_length=20, default='user')
    media_type = models.CharField(max_length=20, default='text')
    media_url = models.TextField(blank=True, null=True)
    content = models.TextField(blank=True, null=True)
    links = models.TextField(default='[]')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'start_messages'
        verbose_name = 'Start消息'
        verbose_name_plural = 'Start消息'


class GroupEntryExitSettings(models.Model):
    """进退群设置"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='entry_exit_settings')
    entry_verification_enabled = models.BooleanField(default=False)
    verification_question = models.TextField(blank=True, null=True)
    verification_answer = models.TextField(blank=True, null=True)
    verification_timeout = models.IntegerField(default=60)
    welcome_enabled = models.BooleanField(default=False)
    welcome_message = models.TextField(blank=True, null=True)
    welcome_media_type = models.CharField(max_length=20, default='text')
    welcome_media_url = models.TextField(blank=True, null=True)
    exit_ban_enabled = models.BooleanField(default=False)
    exit_ban_duration = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'group_entry_exit_settings'
        verbose_name = '进退群设置'
        verbose_name_plural = '进退群设置'


class SpamProtection(models.Model):
    """垃圾防护"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='spam_protection')
    enabled = models.BooleanField(default=False)
    max_messages_per_minute = models.IntegerField(default=10)
    block_links = models.BooleanField(default=False)
    block_forwards = models.BooleanField(default=False)
    block_stickers = models.BooleanField(default=False)
    punishment_type = models.CharField(max_length=20, default='mute')
    punishment_duration = models.IntegerField(default=60)
    whitelist_users = models.TextField(default='[]')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'spam_protection'
        verbose_name = '垃圾防护'
        verbose_name_plural = '垃圾防护'


class TimedGroupControl(models.Model):
    """定时开关群"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='timed_group_control')
    enabled = models.BooleanField(default=False)
    open_time = models.TimeField(blank=True, null=True)
    close_time = models.TimeField(blank=True, null=True)
    timezone = models.CharField(max_length=50, default='Asia/Shanghai')
    close_message = models.TextField(blank=True, null=True)
    open_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'timed_group_control'
        verbose_name = '定时开关群'
        verbose_name_plural = '定时开关群'


class InvitationActivity(models.Model):
    """邀请活动"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='invitation_activity')
    enabled = models.BooleanField(default=False)
    reward_points = models.IntegerField(default=10)
    minimum_invites = models.IntegerField(default=1)
    activity_start = models.DateTimeField(blank=True, null=True)
    activity_end = models.DateTimeField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'invitation_activity'
        verbose_name = '邀请活动'
        verbose_name_plural = '邀请活动'


class ForcedChannelSubscription(models.Model):
    """强制订阅频道"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='forced_channel_subscription')
    enabled = models.BooleanField(default=False)
    channel_id = models.CharField(max_length=50, blank=True, null=True)
    channel_username = models.CharField(max_length=255, blank=True, null=True)
    check_interval = models.IntegerField(default=3600)
    unsubscribe_action = models.CharField(max_length=20, default='kick')
    verification_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'forced_channel_subscription'
        verbose_name = '强制订阅频道'
        verbose_name_plural = '强制订阅频道'


class PointsRule(models.Model):
    """积分规则"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='points_rules')
    rule_name = models.CharField(max_length=255)
    rule_type = models.CharField(max_length=50)
    points_amount = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'points_rules'
        verbose_name = '积分规则'
        verbose_name_plural = '积分规则'


class PointsAutoReply(models.Model):
    """积分自动回复"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='points_auto_reply')
    trigger_keyword = models.CharField(max_length=255)
    points_cost = models.IntegerField(default=0)
    content = models.TextField(blank=True, null=True)
    media_type = models.CharField(max_length=20, default='text')
    media_url = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'points_auto_reply'
        verbose_name = '积分自动回复'
        verbose_name_plural = '积分自动回复'


class PointsAuction(models.Model):
    """积分竞拍"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='points_auction')
    item_name = models.CharField(max_length=255)
    item_description = models.TextField(blank=True, null=True)
    starting_price = models.IntegerField(default=100)
    current_bid = models.IntegerField(default=0)
    current_bidder_id = models.BigIntegerField(blank=True, null=True)
    auction_start = models.DateTimeField(blank=True, null=True)
    auction_end = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=20, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'points_auction'
        verbose_name = '积分竞拍'
        verbose_name_plural = '积分竞拍'


class PointsLog(models.Model):
    """积分日志"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='points_log')
    user_id = models.BigIntegerField(db_index=True)
    points_change = models.IntegerField()
    reason = models.CharField(max_length=255, blank=True, null=True)
    balance_after = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'points_log'
        verbose_name = '积分日志'
        verbose_name_plural = '积分日志'


class MemberLevel(models.Model):
    """群成员等级"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='member_level')
    level_name = models.CharField(max_length=255)
    required_points = models.IntegerField(default=0)
    permissions = models.TextField(default='{}')
    badge_emoji = models.CharField(max_length=10, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'member_level'
        verbose_name = '成员等级'
        verbose_name_plural = '成员等级'


class UserPoints(models.Model):
    """用户积分"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='user_points')
    user_id = models.BigIntegerField(db_index=True)
    points_balance = models.IntegerField(default=0)
    current_level = models.ForeignKey(MemberLevel, on_delete=models.SET_NULL, blank=True, null=True, related_name='users_at_level')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_points'
        unique_together = [['group', 'user_id']]
        verbose_name = '用户积分'
        verbose_name_plural = '用户积分'


class GroupLottery(models.Model):
    """群抽奖"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='group_lottery')
    lottery_name = models.CharField(max_length=255)
    lottery_type = models.CharField(max_length=50)
    prize_description = models.TextField(blank=True, null=True)
    min_messages = models.IntegerField(default=10)
    top_n_winners = models.IntegerField(default=3)
    start_time = models.DateTimeField(blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    winner_ids = models.TextField(default='[]')
    status = models.CharField(max_length=20, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'group_lottery'
        verbose_name = '群抽奖'
        verbose_name_plural = '群抽奖'


class UserNameChange(models.Model):
    """用户改名监控"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='user_name_change')
    user_id = models.BigIntegerField(db_index=True)
    old_name = models.CharField(max_length=255, blank=True, null=True)
    new_name = models.CharField(max_length=255, blank=True, null=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_name_change'
        verbose_name = '用户改名'
        verbose_name_plural = '用户改名'


class GroupBottomButton(models.Model):
    """群底部按钮"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='group_bottom_button')
    button_text = models.CharField(max_length=255)
    button_url = models.TextField(blank=True, null=True)
    button_callback = models.CharField(max_length=255, blank=True, null=True)
    trigger_keyword = models.CharField(max_length=255, blank=True, null=True)
    input_field_placeholder = models.CharField(max_length=255, blank=True, null=True)
    button_order = models.IntegerField(default=0)
    row_position = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'group_bottom_button'
        verbose_name = '群底部按钮'
        verbose_name_plural = '群底部按钮'


class SyncGroupMessages(models.Model):
    """同步群消息"""
    source_group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='sync_group_messages')
    target_group_id = models.CharField(max_length=50)
    enabled = models.BooleanField(default=False)
    sync_media = models.BooleanField(default=True)
    sync_forwards = models.BooleanField(default=True)
    filter_keywords = models.TextField(default='[]')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sync_group_messages'
        verbose_name = '同步群消息'
        verbose_name_plural = '同步群消息'


class SyncMessageLog(models.Model):
    """同步消息日志"""
    source_group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='sync_message_logs')
    target_group_id = models.CharField(max_length=50)
    source_message_id = models.BigIntegerField(blank=True, null=True)
    target_message_id = models.BigIntegerField(blank=True, null=True)
    user_id = models.BigIntegerField(blank=True, null=True)
    username = models.CharField(max_length=255, blank=True, null=True)
    message_type = models.CharField(max_length=20, default='text')
    content_preview = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, default='success')
    error_message = models.TextField(blank=True, null=True)
    synced_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'sync_message_logs'
        verbose_name = '同步消息日志'
        verbose_name_plural = '同步消息日志'


class OtherSettings(models.Model):
    """其他设置"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='other_settings')
    auto_delete_join_msg = models.BooleanField(default=False)
    auto_delete_leave_msg = models.BooleanField(default=False)
    auto_delete_promote_msg = models.BooleanField(default=False)
    auto_delete_pin_msg = models.BooleanField(default=False)
    cancel_channel_pin = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'other_settings'
        verbose_name = '其他设置'
        verbose_name_plural = '其他设置'


class BotClone(models.Model):
    """机器人克隆"""
    clone_name = models.CharField(max_length=255)
    bot_token = models.CharField(max_length=255)
    owner_user_id = models.BigIntegerField(blank=True, null=True)
    admin_user_ids = models.TextField(default='[]')
    is_active = models.BooleanField(default=True)
    expiration_date = models.DateTimeField(blank=True, null=True)
    webhook_url = models.CharField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bot_clones'
        verbose_name = '机器人克隆'
        verbose_name_plural = '机器人克隆'


class LotteryMessageCount(models.Model):
    """抽奖消息计数"""
    lottery = models.ForeignKey(GroupLottery, on_delete=models.CASCADE, related_name='message_counts')
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='lottery_message_counts')
    user_id = models.BigIntegerField()
    message_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'lottery_message_count'
        unique_together = [['lottery', 'user_id']]
        verbose_name = '抽奖消息计数'
        verbose_name_plural = '抽奖消息计数'


class InactiveUserSettings(models.Model):
    """不活跃用户设置"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='inactive_user_settings')
    enabled = models.BooleanField(default=False)
    inactivity_days = models.IntegerField(default=30)
    action_type = models.CharField(max_length=20, default='kick')
    check_interval = models.IntegerField(default=86400)
    warning_enabled = models.BooleanField(default=False)
    warning_days = models.IntegerField(default=7)
    warning_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'inactive_user_settings'
        verbose_name = '不活跃用户设置'
        verbose_name_plural = '不活跃用户设置'


class KeywordFilter(models.Model):
    """关键词过滤"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='keyword_filters')
    keyword = models.CharField(max_length=255)
    filter_type = models.CharField(max_length=20, default='blacklist')
    match_type = models.CharField(max_length=20, default='contains')
    action = models.CharField(max_length=20, default='delete')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'keyword_filter'
        verbose_name = '关键词过滤'
        verbose_name_plural = '关键词过滤'


class MessageStatistics(models.Model):
    """消息统计"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='message_statistics')
    user_id = models.BigIntegerField(db_index=True)
    date = models.DateField(db_index=True, default=timezone.now)
    message_count = models.IntegerField(default=0)
    text_count = models.IntegerField(default=0)
    photo_count = models.IntegerField(default=0)
    video_count = models.IntegerField(default=0)
    sticker_count = models.IntegerField(default=0)
    document_count = models.IntegerField(default=0)
    voice_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'message_statistics'
        unique_together = [['group', 'user_id', 'date']]
        verbose_name = '消息统计'
        verbose_name_plural = '消息统计'


class GroupVote(models.Model):
    """群投票"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='group_votes')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    options = models.TextField(default='[]')
    vote_type = models.CharField(max_length=20, default='single')
    max_choices = models.IntegerField(default=1)
    is_anonymous = models.BooleanField(default=False)
    allow_revote = models.BooleanField(default=True)
    start_time = models.DateTimeField(blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=20, default='pending')
    message_id = models.BigIntegerField(blank=True, null=True)
    created_by = models.BigIntegerField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'group_vote'
        verbose_name = '群投票'
        verbose_name_plural = '群投票'


class VoteRecord(models.Model):
    """投票记录"""
    vote = models.ForeignKey(GroupVote, on_delete=models.CASCADE, related_name='vote_records')
    user_id = models.BigIntegerField(db_index=True)
    choices = models.TextField(default='[]')
    voted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'vote_record'
        unique_together = [['vote', 'user_id']]
        verbose_name = '投票记录'
        verbose_name_plural = '投票记录'


class QuizGame(models.Model):
    """问答游戏"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='quiz_games')
    question = models.TextField()
    answers = models.TextField(default='[]')
    correct_answer_index = models.IntegerField()
    explanation = models.TextField(blank=True, null=True)
    points_reward = models.IntegerField(default=10)
    time_limit = models.IntegerField(default=60)
    difficulty = models.CharField(max_length=20, default='medium')
    category = models.CharField(max_length=50, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quiz_game'
        verbose_name = '问答游戏'
        verbose_name_plural = '问答游戏'


class QuizSession(models.Model):
    """问答会话"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='quiz_sessions')
    quiz = models.ForeignKey(QuizGame, on_delete=models.CASCADE, related_name='quiz_sessions')
    message_id = models.BigIntegerField(blank=True, null=True)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=20, default='active')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'quiz_session'
        verbose_name = '问答会话'
        verbose_name_plural = '问答会话'


class QuizAnswer(models.Model):
    """问答答案记录"""
    session = models.ForeignKey(QuizSession, on_delete=models.CASCADE, related_name='quiz_answers')
    user_id = models.BigIntegerField(db_index=True)
    answer_index = models.IntegerField()
    is_correct = models.BooleanField(default=False)
    points_awarded = models.IntegerField(default=0)
    answered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'quiz_answer'
        unique_together = [['session', 'user_id']]
        verbose_name = '问答答案'
        verbose_name_plural = '问答答案'


class RedPacket(models.Model):
    """红包"""
    group = models.ForeignKey(BotGroup, on_delete=models.CASCADE, related_name='red_packets')
    creator_id = models.BigIntegerField(db_index=True)
    packet_type = models.CharField(max_length=20, default='random')
    total_points = models.IntegerField()
    packet_count = models.IntegerField()
    remaining_count = models.IntegerField()
    remaining_points = models.IntegerField()
    message = models.TextField(blank=True, null=True)
    message_id = models.BigIntegerField(blank=True, null=True)
    expire_time = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=20, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'red_packet'
        verbose_name = '红包'
        verbose_name_plural = '红包'


class RedPacketClaim(models.Model):
    """红包领取记录"""
    packet = models.ForeignKey(RedPacket, on_delete=models.CASCADE, related_name='red_packet_claims')
    user_id = models.BigIntegerField(db_index=True)
    points_received = models.IntegerField()
    claimed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'red_packet_claim'
        unique_together = [['packet', 'user_id']]
        verbose_name = '红包领取'
        verbose_name_plural = '红包领取'


# 默认字段配置
DEFAULT_FIELDS = [
    {"key": "name", "label": "昵称", "type": "text"},
    {"key": "region", "label": "地区", "type": "select", "options": ["福田", "南山"]},
]

# 默认系统配置
DEFAULT_SYSTEM = {
    "checkin_open": True,
    "checkin_cmd": "打卡",
    "query_open": True,
    "query_cmd": "查询",
    "query_filter_open": True,
    "checkin_del_time": 30,
    "query_del_time": 60,
    "page_size": 10,
    "auto_like": True,
    "like_emoji": "❤️",
    "auto_reply_open": True,
    "scheduled_msg_open": True,
    "start_msg_open": True,
    "push_channel_id": "",
    "msg_checkin_success": "✅ <b>打卡成功！</b>",
    "msg_not_registered": "⚠️ <b>未认证用户</b>",
    "msg_repeat_checkin": "🔄 <b>今天已打卡</b>",
    "msg_query_header": "🔍 <b>今日在线用户：</b>\n",
    "msg_filter_header": "🔍 <b>筛选结果：</b>\n",
    "msg_expired_ban": "⛔️ <b>您的认证已过期，已被暂时禁言。请联系管理员续费。</b>",
    "msg_private_start": "👋 你好！我是打卡机器人。",
    "template": "{onlineEmoji} {昵称} | {地区}",
    "push_template": "<b>👤 名片推送</b>\n昵称：{昵称}\n<a href='tg://user?id={tg_id}'>联系我</a>",
    "custom_buttons": "[]"
}
