# -*- encoding: utf-8 -*-
"""
Authentication URL configuration
"""

from django.urls import path
from django.contrib.auth.views import LogoutView
from django.contrib.auth.decorators import login_required
from .views import login_view, register_user, statistics_view, send_message_api, broadcast_message_api

urlpatterns = [
    path('login/', login_view, name="login"),
    path('register/', register_user, name="register"),
    path('', login_required(statistics_view), name='statistics'),
    path('search/', statistics_view, name='statistics_view'),
    path('sendmessage/', send_message_api, name='send_message_api'),
    path('broadcastmessage/', broadcast_message_api, name='broadcast_message_api'),
    path('logout/', LogoutView.as_view(next_page='/login/'), name="logout"),
    path('core/logout', LogoutView.as_view(next_page='/login/'), name="core_logout"),
]
