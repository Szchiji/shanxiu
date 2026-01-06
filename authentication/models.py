# -*- encoding: utf-8 -*-
"""
Authentication models
"""

from django.db import models


class TelegramUser(models.Model):
    """Telegram user model for tracking bot users"""
    telegram_id = models.BigIntegerField(primary_key=True, db_column='id')
    joining_date = models.DateField(db_column='joining_date', null=True, blank=True)

    class Meta:
        db_table = 'telegram_users'
        verbose_name = 'Telegram用户'
        verbose_name_plural = 'Telegram用户'

    def __str__(self):
        return str(self.telegram_id)
