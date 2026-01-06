# -*- encoding: utf-8 -*-
"""
App views - Group management functionality
"""

import json
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.template import loader
from django.http import HttpResponse, JsonResponse
from django import template
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from datetime import timedelta

from .models import (
    BotGroup, GroupUser, AutoReply, ScheduledMessage, StartMessage,
    GroupEntryExitSettings, SpamProtection, TimedGroupControl,
    InvitationActivity, ForcedChannelSubscription, PointsRule,
    PointsAutoReply, PointsAuction, PointsLog, MemberLevel,
    GroupLottery, UserNameChange, GroupBottomButton, SyncGroupMessages,
    SyncMessageLog, OtherSettings, InactiveUserSettings, KeywordFilter,
    MessageStatistics, GroupVote, QuizGame, RedPacket, BotClone,
    DEFAULT_FIELDS, DEFAULT_SYSTEM
)


def get_session_context(request):
    """Get session context for templates"""
    return {
        'session': {
            'logged_in': request.user.is_authenticated
        }
    }


def get_group_stats(group):
    """Get statistics for a group"""
    today = timezone.now().date()
    return {
        'users': GroupUser.objects.filter(group=group).count(),
        'online': GroupUser.objects.filter(group=group, online=True).count(),
        'today_checkins': GroupUser.objects.filter(
            group=group,
            checkin_time__date=today
        ).count(),
        'expired': GroupUser.objects.filter(
            group=group,
            expiration_date__lt=timezone.now()
        ).count(),
        'banned': GroupUser.objects.filter(group=group, is_banned=True).count(),
        'auto_replies': AutoReply.objects.filter(group=group, is_active=True).count(),
        'scheduled_msgs': ScheduledMessage.objects.filter(group=group, is_active=True).count(),
        'start_msgs': StartMessage.objects.filter(group=group, is_active=True).count(),
    }


@login_required(login_url="/login/")
def index(request):
    """Home page"""
    context = get_session_context(request)
    context['segment'] = 'index'

    html_template = loader.get_template('index.html')
    return HttpResponse(html_template.render(context, request))


@login_required(login_url="/login/")
def select_group(request):
    """Select group page"""
    groups = BotGroup.objects.filter(is_active=True).order_by('-updated_at')
    context = get_session_context(request)
    context.update({
        'groups': groups,
        'page': 'select_group',
    })
    return render(request, 'select_group.html', context)


@login_required(login_url="/login/")
def group_dashboard(request, group_id):
    """Group dashboard view"""
    group = get_object_or_404(BotGroup, id=group_id)
    stats = get_group_stats(group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'stats': stats,
        'page': 'dashboard',
    })
    return render(request, 'dashboard.html', context)


@login_required(login_url="/login/")
def group_users(request, group_id):
    """Group users management"""
    group = get_object_or_404(BotGroup, id=group_id)
    users = GroupUser.objects.filter(group=group).order_by('-last_activity')
    
    # Parse fields config
    try:
        fields = json.loads(group.fields_config) if group.fields_config else DEFAULT_FIELDS
    except json.JSONDecodeError:
        fields = DEFAULT_FIELDS
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'users': users,
        'fields': fields,
        'stats': get_group_stats(group),
        'page': 'users',
    })
    return render(request, 'users.html', context)


@login_required(login_url="/login/")
def group_fields(request, group_id):
    """Group fields configuration"""
    group = get_object_or_404(BotGroup, id=group_id)
    
    try:
        fields = json.loads(group.fields_config) if group.fields_config else DEFAULT_FIELDS
    except json.JSONDecodeError:
        fields = DEFAULT_FIELDS
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'fields': fields,
        'stats': get_group_stats(group),
        'page': 'fields',
    })
    return render(request, 'fields.html', context)


@login_required(login_url="/login/")
def group_settings(request, group_id):
    """Group settings page"""
    group = get_object_or_404(BotGroup, id=group_id)
    
    try:
        config = json.loads(group.config) if group.config else DEFAULT_SYSTEM.copy()
    except json.JSONDecodeError:
        config = DEFAULT_SYSTEM.copy()
    
    # Merge with defaults
    for key, value in DEFAULT_SYSTEM.items():
        if key not in config:
            config[key] = value
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'config': config,
        'stats': get_group_stats(group),
        'page': 'settings',
    })
    return render(request, 'settings.html', context)


@login_required(login_url="/login/")
def group_auto_replies(request, group_id):
    """Auto replies management"""
    group = get_object_or_404(BotGroup, id=group_id)
    auto_replies = AutoReply.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'auto_replies': auto_replies,
        'stats': get_group_stats(group),
        'page': 'auto_replies',
    })
    return render(request, 'auto_replies.html', context)


@login_required(login_url="/login/")
def group_scheduled_messages(request, group_id):
    """Scheduled messages management"""
    group = get_object_or_404(BotGroup, id=group_id)
    scheduled_messages = ScheduledMessage.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'scheduled_messages': scheduled_messages,
        'stats': get_group_stats(group),
        'page': 'scheduled_messages',
    })
    return render(request, 'scheduled_messages.html', context)


@login_required(login_url="/login/")
def group_start_messages(request, group_id):
    """Start messages management"""
    group = get_object_or_404(BotGroup, id=group_id)
    start_messages = StartMessage.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'start_messages': start_messages,
        'stats': get_group_stats(group),
        'page': 'start_messages',
    })
    return render(request, 'start_messages.html', context)


@login_required(login_url="/login/")
def group_bottom_button(request, group_id):
    """Group bottom button management"""
    group = get_object_or_404(BotGroup, id=group_id)
    buttons = GroupBottomButton.objects.filter(group=group).order_by('row_position', 'button_order')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'buttons': buttons,
        'stats': get_group_stats(group),
        'page': 'group_bottom_button',
    })
    return render(request, 'group_bottom_button.html', context)


@login_required(login_url="/login/")
def group_entry_exit_settings(request, group_id):
    """Entry/exit settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    settings_obj, created = GroupEntryExitSettings.objects.get_or_create(group=group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'settings': settings_obj,
        'stats': get_group_stats(group),
        'page': 'entry_exit_settings',
    })
    return render(request, 'entry_exit_settings.html', context)


@login_required(login_url="/login/")
def group_spam_protection(request, group_id):
    """Spam protection settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    settings_obj, created = SpamProtection.objects.get_or_create(group=group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'settings': settings_obj,
        'stats': get_group_stats(group),
        'page': 'spam_protection',
    })
    return render(request, 'spam_protection.html', context)


@login_required(login_url="/login/")
def group_keyword_filter(request, group_id):
    """Keyword filter settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    filters = KeywordFilter.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'filters': filters,
        'stats': get_group_stats(group),
        'page': 'keyword_filter',
    })
    return render(request, 'keyword_filter.html', context)


@login_required(login_url="/login/")
def group_forced_channel(request, group_id):
    """Forced channel subscription settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    settings_obj, created = ForcedChannelSubscription.objects.get_or_create(group=group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'settings': settings_obj,
        'stats': get_group_stats(group),
        'page': 'forced_channel_subscription',
    })
    return render(request, 'forced_channel_subscription.html', context)


@login_required(login_url="/login/")
def group_inactive_users(request, group_id):
    """Inactive user settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    settings_obj, created = InactiveUserSettings.objects.get_or_create(group=group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'settings': settings_obj,
        'stats': get_group_stats(group),
        'page': 'inactive_user_settings',
    })
    return render(request, 'inactive_user_settings.html', context)


@login_required(login_url="/login/")
def group_timed_control(request, group_id):
    """Timed group control settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    settings_obj, created = TimedGroupControl.objects.get_or_create(group=group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'settings': settings_obj,
        'stats': get_group_stats(group),
        'page': 'timed_group_control',
    })
    return render(request, 'timed_group_control.html', context)


@login_required(login_url="/login/")
def group_invitation_activity(request, group_id):
    """Invitation activity settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    settings_obj, created = InvitationActivity.objects.get_or_create(group=group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'settings': settings_obj,
        'stats': get_group_stats(group),
        'page': 'invitation_activity',
    })
    return render(request, 'invitation_activity.html', context)


@login_required(login_url="/login/")
def group_member_level(request, group_id):
    """Member level management"""
    group = get_object_or_404(BotGroup, id=group_id)
    levels = MemberLevel.objects.filter(group=group).order_by('required_points')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'levels': levels,
        'stats': get_group_stats(group),
        'page': 'member_level',
    })
    return render(request, 'member_level.html', context)


@login_required(login_url="/login/")
def group_sync_messages(request, group_id):
    """Sync group messages settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    sync_settings = SyncGroupMessages.objects.filter(source_group=group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'sync_settings': sync_settings,
        'stats': get_group_stats(group),
        'page': 'sync_group_messages',
    })
    return render(request, 'sync_group_messages.html', context)


@login_required(login_url="/login/")
def group_sync_logs(request, group_id):
    """Sync message logs"""
    group = get_object_or_404(BotGroup, id=group_id)
    logs = SyncMessageLog.objects.filter(source_group=group).order_by('-synced_at')[:100]
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'logs': logs,
        'stats': get_group_stats(group),
        'page': 'sync_message_logs',
    })
    return render(request, 'sync_message_logs.html', context)


@login_required(login_url="/login/")
def group_message_statistics(request, group_id):
    """Message statistics"""
    group = get_object_or_404(BotGroup, id=group_id)
    today = timezone.now().date()
    last_7_days = today - timedelta(days=7)
    
    statistics = MessageStatistics.objects.filter(
        group=group,
        date__gte=last_7_days
    ).order_by('-date')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'statistics': statistics,
        'stats': get_group_stats(group),
        'page': 'message_statistics',
    })
    return render(request, 'message_statistics.html', context)


@login_required(login_url="/login/")
def group_user_name_change(request, group_id):
    """User name change monitoring"""
    group = get_object_or_404(BotGroup, id=group_id)
    changes = UserNameChange.objects.filter(group=group).order_by('-changed_at')[:100]
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'changes': changes,
        'stats': get_group_stats(group),
        'page': 'user_name_change',
    })
    return render(request, 'user_name_change.html', context)


@login_required(login_url="/login/")
def group_points_rules(request, group_id):
    """Points rules management"""
    group = get_object_or_404(BotGroup, id=group_id)
    rules = PointsRule.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'rules': rules,
        'stats': get_group_stats(group),
        'page': 'points_rules',
    })
    return render(request, 'points_rules.html', context)


@login_required(login_url="/login/")
def group_points_auto_reply(request, group_id):
    """Points auto reply management"""
    group = get_object_or_404(BotGroup, id=group_id)
    auto_replies = PointsAutoReply.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'auto_replies': auto_replies,
        'stats': get_group_stats(group),
        'page': 'points_auto_reply',
    })
    return render(request, 'points_auto_reply.html', context)


@login_required(login_url="/login/")
def group_points_auction(request, group_id):
    """Points auction management"""
    group = get_object_or_404(BotGroup, id=group_id)
    auctions = PointsAuction.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'auctions': auctions,
        'stats': get_group_stats(group),
        'page': 'points_auction',
    })
    return render(request, 'points_auction.html', context)


@login_required(login_url="/login/")
def group_points_log(request, group_id):
    """Points log"""
    group = get_object_or_404(BotGroup, id=group_id)
    logs = PointsLog.objects.filter(group=group).order_by('-created_at')[:100]
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'logs': logs,
        'stats': get_group_stats(group),
        'page': 'points_log',
    })
    return render(request, 'points_log.html', context)


@login_required(login_url="/login/")
def group_lottery(request, group_id):
    """Group lottery management"""
    group = get_object_or_404(BotGroup, id=group_id)
    lotteries = GroupLottery.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'lotteries': lotteries,
        'stats': get_group_stats(group),
        'page': 'group_lottery',
    })
    return render(request, 'group_lottery.html', context)


@login_required(login_url="/login/")
def group_votes(request, group_id):
    """Group votes management"""
    group = get_object_or_404(BotGroup, id=group_id)
    votes = GroupVote.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'votes': votes,
        'stats': get_group_stats(group),
        'page': 'group_votes',
    })
    return render(request, 'group_votes.html', context)


@login_required(login_url="/login/")
def group_quiz_games(request, group_id):
    """Quiz games management"""
    group = get_object_or_404(BotGroup, id=group_id)
    quizzes = QuizGame.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'quizzes': quizzes,
        'stats': get_group_stats(group),
        'page': 'quiz_games',
    })
    return render(request, 'quiz_games.html', context)


@login_required(login_url="/login/")
def group_red_packet(request, group_id):
    """Red packet management"""
    group = get_object_or_404(BotGroup, id=group_id)
    red_packets = RedPacket.objects.filter(group=group).order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'red_packets': red_packets,
        'stats': get_group_stats(group),
        'page': 'red_packet_settings',
    })
    return render(request, 'red_packet_settings.html', context)


@login_required(login_url="/login/")
def group_other_settings(request, group_id):
    """Other settings"""
    group = get_object_or_404(BotGroup, id=group_id)
    settings_obj, created = OtherSettings.objects.get_or_create(group=group)
    
    context = get_session_context(request)
    context.update({
        'group': group,
        'current_group': group,
        'settings': settings_obj,
        'stats': get_group_stats(group),
        'page': 'other_settings',
    })
    return render(request, 'other_settings.html', context)


@login_required(login_url="/login/")
def bot_clones(request):
    """Bot clones management"""
    clones = BotClone.objects.all().order_by('-updated_at')
    
    context = get_session_context(request)
    context.update({
        'clones': clones,
        'page': 'bot_clones',
    })
    return render(request, 'bot_clones.html', context)


@login_required(login_url="/login/")
def pages(request):
    """Load HTML pages"""
    context = get_session_context(request)
    try:
        load_template = request.path.split('/')[-1]
        context['segment'] = load_template

        html_template = loader.get_template(load_template)
        return HttpResponse(html_template.render(context, request))

    except template.TemplateDoesNotExist:
        html_template = loader.get_template('page-404.html')
        return HttpResponse(html_template.render(context, request))

    except Exception:
        html_template = loader.get_template('page-500.html')
        return HttpResponse(html_template.render(context, request))
