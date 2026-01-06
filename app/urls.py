# -*- encoding: utf-8 -*-
"""
App URL configuration - Group management routes
"""

from django.urls import path, re_path
from app import views

urlpatterns = [
    # The home page
    path('home/', views.index, name='home'),
    
    # Group selection
    path('core/select_group', views.select_group, name='select_group'),
    
    # Group dashboard
    path('core/group/<int:group_id>/dashboard', views.group_dashboard, name='group_dashboard'),
    
    # User management
    path('core/group/<int:group_id>/users', views.group_users, name='group_users'),
    path('core/group/<int:group_id>/fields', views.group_fields, name='group_fields'),
    path('core/group/<int:group_id>/settings', views.group_settings, name='group_settings'),
    
    # Messages & Automation
    path('core/group/<int:group_id>/auto_replies', views.group_auto_replies, name='group_auto_replies'),
    path('core/group/<int:group_id>/scheduled_messages', views.group_scheduled_messages, name='group_scheduled_messages'),
    path('core/group/<int:group_id>/start_messages', views.group_start_messages, name='group_start_messages'),
    path('core/group/<int:group_id>/group_bottom_button', views.group_bottom_button, name='group_bottom_button'),
    
    # Group Security
    path('core/group/<int:group_id>/entry_exit_settings', views.group_entry_exit_settings, name='group_entry_exit_settings'),
    path('core/group/<int:group_id>/spam_protection', views.group_spam_protection, name='group_spam_protection'),
    path('core/group/<int:group_id>/keyword_filter', views.group_keyword_filter, name='group_keyword_filter'),
    path('core/group/<int:group_id>/forced_channel_subscription', views.group_forced_channel, name='group_forced_channel'),
    path('core/group/<int:group_id>/inactive_user_settings', views.group_inactive_users, name='group_inactive_users'),
    
    # Group Features
    path('core/group/<int:group_id>/timed_group_control', views.group_timed_control, name='group_timed_control'),
    path('core/group/<int:group_id>/invitation_activity', views.group_invitation_activity, name='group_invitation_activity'),
    path('core/group/<int:group_id>/member_level', views.group_member_level, name='group_member_level'),
    path('core/group/<int:group_id>/sync_group_messages', views.group_sync_messages, name='group_sync_messages'),
    path('core/group/<int:group_id>/sync_message_logs', views.group_sync_logs, name='group_sync_logs'),
    
    # Analytics & Monitoring
    path('core/group/<int:group_id>/message_statistics', views.group_message_statistics, name='group_message_statistics'),
    path('core/group/<int:group_id>/user_name_change', views.group_user_name_change, name='group_user_name_change'),
    
    # Points & Activities
    path('core/group/<int:group_id>/points_rules', views.group_points_rules, name='group_points_rules'),
    path('core/group/<int:group_id>/points_auto_reply', views.group_points_auto_reply, name='group_points_auto_reply'),
    path('core/group/<int:group_id>/points_auction', views.group_points_auction, name='group_points_auction'),
    path('core/group/<int:group_id>/points_log', views.group_points_log, name='group_points_log'),
    path('core/group/<int:group_id>/group_lottery', views.group_lottery, name='group_lottery'),
    path('core/group/<int:group_id>/group_votes', views.group_votes, name='group_votes'),
    path('core/group/<int:group_id>/quiz_games', views.group_quiz_games, name='group_quiz_games'),
    path('core/group/<int:group_id>/red_packet_settings', views.group_red_packet, name='group_red_packet'),
    
    # Other settings
    path('core/group/<int:group_id>/other_settings', views.group_other_settings, name='group_other_settings'),
    
    # Bot clones (global)
    path('core/bot_clones', views.bot_clones, name='bot_clones'),

    # Matches any html file - must be last!
    re_path(r'^.*\.*', views.pages, name='pages'),
]
