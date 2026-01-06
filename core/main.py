# -*- encoding: utf-8 -*-
"""
Telegram bot main script using python-telegram-bot

This module handles all Telegram bot functionality including:
- User registration and management
- Check-in system
- Auto-reply
- Scheduled messages
- Points system
- And more features
"""

import os
import sys
import django

# Setup Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from django.conf import settings
from app.models import BotGroup, GroupUser, DEFAULT_FIELDS, DEFAULT_SYSTEM
from authentication.models import TelegramUser
from datetime import date
import json

# Bot token from settings
TOKEN = settings.TELEGRAM_BOT_TOKEN
ADMIN_IDS = [settings.ADMIN_ID] if settings.ADMIN_ID else []


async def cmd_start(update: Update, context):
    """Handle /start command"""
    user = update.effective_user
    chat = update.effective_chat
    
    print(f"✅ /start command triggered by user ID: {user.id}")
    
    # Record user if not exists
    try:
        tg_user, created = TelegramUser.objects.get_or_create(
            telegram_id=user.id,
            defaults={'joining_date': date.today()}
        )
        if created:
            print(f"📝 New user registered: {user.id}")
    except Exception as e:
        print(f"Error recording user: {e}")
    
    # Different response for private chat vs group
    if chat.type == 'private':
        hello_msg = """👋 你好！欢迎使用机器人。

✅ 可用功能：
• 打卡签到
• 查询用户
• 自动回复
• 更多功能...

如需帮助，请联系管理员。"""
        
        keyboard = [[InlineKeyboardButton('📣 官方群组', url='https://t.me/')]]
        await update.message.reply_text(
            hello_msg,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text("👋 机器人已激活！")


async def cmd_help(update: Update, context):
    """Handle /help command"""
    help_text = """📖 帮助信息

可用命令：
/start - 开始使用
/help - 显示帮助
/checkin 或 打卡 - 每日打卡
/query 或 查询 - 查询用户

更多功能请在管理后台设置。"""
    
    await update.message.reply_text(help_text)


async def on_message(update: Update, context):
    """Handle all messages"""
    if not update.message or not update.message.text:
        return
    
    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text.strip()
    
    # Skip commands
    if text.startswith('/'):
        return
    
    try:
        # Get group configuration
        group = BotGroup.objects.filter(chat_id=str(chat.id)).first()
        if not group or not group.is_active:
            return
        
        # Parse config
        conf = DEFAULT_SYSTEM.copy()
        if group.config:
            try:
                c = json.loads(group.config)
                if isinstance(c, dict) and 'config' in c:
                    c = c['config']
                for k, v in c.items():
                    if v is not None:
                        conf[k] = v
            except Exception:
                pass
        
        # Check-in command
        checkin_cmds = [c.strip() for c in conf.get('checkin_cmd', '打卡').split(',')]
        if conf.get('checkin_open') and text in checkin_cmds:
            db_user = GroupUser.objects.filter(group_id=group.id, tg_id=user.id).first()
            if not db_user:
                await update.message.reply_html(conf.get('msg_not_registered', '⚠️ 未认证用户'))
            else:
                from django.utils import timezone
                db_user.checkin_time = timezone.now()
                db_user.online = True
                db_user.save()
                await update.message.reply_html(conf.get('msg_checkin_success', '✅ 打卡成功！'))
            return
        
        # Query command
        query_cmds = [c.strip() for c in conf.get('query_cmd', '查询').split(',')]
        if conf.get('query_open') and text in query_cmds:
            # Simple query response
            users = GroupUser.objects.filter(group_id=group.id, online=True).count()
            await update.message.reply_text(f"🔍 当前在线用户: {users} 人")
            return
        
    except Exception as e:
        print(f"Message handling error: {e}")


async def on_my_chat_member(update: Update, context):
    """Handle bot being added/removed from groups"""
    try:
        chat = update.effective_chat
        status = update.my_chat_member.new_chat_member.status
        
        if chat.type in ['group', 'supergroup'] and status in ['administrator', 'member']:
            # Register group
            group, created = BotGroup.objects.get_or_create(
                chat_id=str(chat.id),
                defaults={
                    'title': chat.title,
                    'type': chat.type,
                    'is_active': True,
                    'fields_config': json.dumps(DEFAULT_FIELDS, ensure_ascii=False)
                }
            )
            if created:
                print(f"➕ New group registered: {chat.title}")
            
            await context.bot.send_message(chat.id, "✅ 机器人已激活！")
            
    except Exception as e:
        print(f"Error in on_my_chat_member: {e}")


def main():
    """Main function to run the bot"""
    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN not configured!")
        print("Please set TELEGRAM_BOT_TOKEN in your environment variables.")
        return
    
    print("🤖 Starting Telegram bot...")
    
    # Create application
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    application.add_handler(CallbackQueryHandler(lambda u, c: None))  # Placeholder
    
    # Chat member handler
    from telegram.ext import ChatMemberHandler
    application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    
    print("✅ Bot handlers registered")
    print("🚀 Bot is running...")
    
    # Start polling
    application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
