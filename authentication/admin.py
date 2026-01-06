# -*- encoding: utf-8 -*-
"""
Authentication admin configuration
"""

from django.contrib import admin
from .models import TelegramUser


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ('telegram_id', 'joining_date')
    search_fields = ('telegram_id',)
    list_filter = ('joining_date',)
