# -*- encoding: utf-8 -*-
"""
App admin configuration
"""

from django.contrib import admin
from .models import (
    BotGroup, GroupUser, AutoReply, ScheduledMessage, StartMessage,
    GroupEntryExitSettings, SpamProtection, TimedGroupControl, InvitationActivity,
    ForcedChannelSubscription, PointsRule, PointsAutoReply, PointsAuction,
    PointsLog, UserPoints, GroupLottery, MemberLevel, UserNameChange,
    GroupBottomButton, SyncGroupMessages, SyncMessageLog, OtherSettings,
    BotClone, LotteryMessageCount, InactiveUserSettings, KeywordFilter,
    MessageStatistics, GroupVote, VoteRecord, QuizGame, QuizSession,
    QuizAnswer, RedPacket, RedPacketClaim, AuthSession
)


@admin.register(BotGroup)
class BotGroupAdmin(admin.ModelAdmin):
    list_display = ('title', 'chat_id', 'type', 'is_active', 'updated_at')
    search_fields = ('title', 'chat_id')
    list_filter = ('is_active', 'type')


@admin.register(GroupUser)
class GroupUserAdmin(admin.ModelAdmin):
    list_display = ('tg_id', 'group', 'is_banned', 'online', 'checkin_time')
    search_fields = ('tg_id',)
    list_filter = ('is_banned', 'online')


@admin.register(AutoReply)
class AutoReplyAdmin(admin.ModelAdmin):
    list_display = ('trigger_keyword', 'group', 'media_type', 'is_active')
    search_fields = ('trigger_keyword',)
    list_filter = ('is_active', 'media_type')


@admin.register(ScheduledMessage)
class ScheduledMessageAdmin(admin.ModelAdmin):
    list_display = ('group', 'media_type', 'repeat_interval', 'is_active')
    list_filter = ('is_active', 'media_type')


@admin.register(StartMessage)
class StartMessageAdmin(admin.ModelAdmin):
    list_display = ('group', 'message_type', 'media_type', 'is_active')
    list_filter = ('is_active', 'message_type')


@admin.register(BotClone)
class BotCloneAdmin(admin.ModelAdmin):
    list_display = ('clone_name', 'is_active', 'owner_user_id', 'created_at')
    search_fields = ('clone_name',)
    list_filter = ('is_active',)


@admin.register(PointsRule)
class PointsRuleAdmin(admin.ModelAdmin):
    list_display = ('rule_name', 'group', 'rule_type', 'points_amount', 'is_active')
    list_filter = ('is_active', 'rule_type')


@admin.register(UserPoints)
class UserPointsAdmin(admin.ModelAdmin):
    list_display = ('user_id', 'group', 'points_balance', 'updated_at')
    search_fields = ('user_id',)


@admin.register(PointsLog)
class PointsLogAdmin(admin.ModelAdmin):
    list_display = ('user_id', 'group', 'points_change', 'reason', 'created_at')
    search_fields = ('user_id', 'reason')
    list_filter = ('group',)
