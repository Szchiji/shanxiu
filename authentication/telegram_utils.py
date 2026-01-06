# -*- encoding: utf-8 -*-
"""
Telegram utility functions
"""

import telegram
from django.conf import settings

TOKEN = settings.TELEGRAM_BOT_TOKEN


async def get_username_from_telegram(user_id):
    """Get username from Telegram user ID"""
    if not TOKEN:
        return None

    bot = telegram.Bot(token=TOKEN)

    try:
        user = await bot.get_chat(user_id)
        username = user.username if user.username else 'N/A'
        return username
    except Exception as e:
        print('Error retrieving username:', e)
        return None


async def send_message_to_telegram_user(user_id, message):
    """Send message to a Telegram user"""
    if not TOKEN:
        return

    bot = telegram.Bot(token=TOKEN)

    try:
        await bot.send_message(chat_id=user_id, text=message)
    except Exception as e:
        print('Error sending message:', e)


async def send_message_to_telegram_user_with_image(user_id, message, image_data):
    """Send message with image to a Telegram user"""
    if not TOKEN:
        return

    bot = telegram.Bot(token=TOKEN)

    try:
        await bot.send_photo(chat_id=user_id, photo=image_data, caption=message)
    except Exception as e:
        print('Error sending message with image:', e)


async def broadcast_message_to_telegram_users(message, chat_ids):
    """Broadcast message to multiple Telegram users"""
    if not TOKEN:
        return

    bot = telegram.Bot(token=TOKEN)

    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            print(f'Error broadcasting to {chat_id}:', e)
            continue


async def broadcast_message_to_telegram_users_with_image(message, chat_ids, image_data):
    """Broadcast message with image to multiple Telegram users"""
    if not TOKEN:
        return

    bot = telegram.Bot(token=TOKEN)

    for chat_id in chat_ids:
        try:
            await bot.send_photo(chat_id=chat_id, photo=image_data, caption=message)
        except Exception as e:
            print(f'Error broadcasting with image to {chat_id}:', e)
            continue
