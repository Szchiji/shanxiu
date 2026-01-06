# -*- encoding: utf-8 -*-
"""
App URL configuration
"""

from django.urls import path, re_path
from app import views

urlpatterns = [
    # The home page
    path('home/', views.index, name='home'),

    # Matches any html file
    re_path(r'^.*\.*', views.pages, name='pages'),
]
