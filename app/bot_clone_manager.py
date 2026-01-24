"""
Bot Clone Manager - 管理克隆机器人实例

This module manages the lifecycle of clone bots, allowing multiple bot instances
to run concurrently with the main bot.

Webhook Mode Note:
- Clone bots can use webhook mode if a webhook_url is provided
- The webhook endpoint needs to be handled by the hosting infrastructure
  (e.g., reverse proxy routing different paths to different handlers)
- For simpler deployments, polling mode is recommended for clone bots
"""
import asyncio
import os
import logging
from typing import Dict, Optional
from telegram.ext import Application
from datetime import datetime
import traceback

# 存储所有运行中的克隆Bot实例
# Key: clone_id (int), Value: {'app': Application, 'loop_task': Task or None, 'mode': str}
active_clones: Dict[int, dict] = {}

logger = logging.getLogger(__name__)


async def start_clone_bot(clone_id: int, bot_token: str, webhook_url: Optional[str] = None, 
                          flask_app=None, handlers_setup_func=None):
    """
    启动一个克隆机器人实例
    
    Args:
        clone_id: 克隆机器人的数据库ID
        bot_token: Bot token
        webhook_url: Webhook URL (如果使用webhook模式)
        flask_app: Flask应用实例 (用于数据库访问)
        handlers_setup_func: 设置处理器的函数 (接收Application实例)
    
    Returns:
        bool: 成功返回True, 失败返回False
    """
    global active_clones
    
    # Check if already running
    if clone_id in active_clones:
        logger.warning(f"Clone bot {clone_id} is already running")
        return True
    
    try:
        logger.info(f"🤖 Starting clone bot {clone_id}...")
        
        # Create Application instance
        app_builder = Application.builder().token(bot_token)
        app = app_builder.build()
        
        # Setup handlers if provided
        if handlers_setup_func:
            handlers_setup_func(app, flask_app, clone_id)
        
        # Initialize and start
        await app.initialize()
        await app.start()
        
        # Determine mode: Webhook or Polling
        if webhook_url:
            # Webhook mode
            logger.info(f"Clone bot {clone_id} using Webhook mode: {webhook_url}")
            await app.bot.set_webhook(webhook_url)
            # Store without starting updater
            active_clones[clone_id] = {
                'app': app,
                'loop_task': None,
                'mode': 'webhook'
            }
        else:
            # Polling mode
            logger.info(f"Clone bot {clone_id} using Polling mode")
            await app.updater.start_polling(drop_pending_updates=True)
            
            active_clones[clone_id] = {
                'app': app,
                'loop_task': None,
                'mode': 'polling'
            }
        
        logger.info(f"✅ Clone bot {clone_id} started successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to start clone bot {clone_id}: {e}")
        traceback.print_exc()
        return False


async def stop_clone_bot(clone_id: int):
    """
    停止一个克隆机器人实例
    
    Args:
        clone_id: 克隆机器人的数据库ID
    
    Returns:
        bool: 成功返回True, 失败返回False
    """
    global active_clones
    
    if clone_id not in active_clones:
        logger.warning(f"Clone bot {clone_id} is not running")
        return True
    
    try:
        logger.info(f"🛑 Stopping clone bot {clone_id}...")
        
        clone_info = active_clones[clone_id]
        app = clone_info['app']
        
        # Stop updater if in polling mode
        if clone_info['mode'] == 'polling' and app.updater:
            await app.updater.stop()
        
        # Stop and shutdown app
        await app.stop()
        await app.shutdown()
        
        # Remove from active clones
        del active_clones[clone_id]
        
        logger.info(f"✅ Clone bot {clone_id} stopped successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to stop clone bot {clone_id}: {e}")
        traceback.print_exc()
        return False


async def restart_clone_bot(clone_id: int, bot_token: str, webhook_url: Optional[str] = None,
                           flask_app=None, handlers_setup_func=None):
    """
    重启一个克隆机器人实例
    
    Args:
        clone_id: 克隆机器人的数据库ID
        bot_token: Bot token
        webhook_url: Webhook URL
        flask_app: Flask应用实例
        handlers_setup_func: 设置处理器的函数
    
    Returns:
        bool: 成功返回True, 失败返回False
    """
    await stop_clone_bot(clone_id)
    await asyncio.sleep(1)  # Brief delay before restart
    return await start_clone_bot(clone_id, bot_token, webhook_url, flask_app, handlers_setup_func)


def is_clone_running(clone_id: int) -> bool:
    """检查克隆机器人是否正在运行"""
    return clone_id in active_clones


def get_active_clone_ids() -> list:
    """获取所有正在运行的克隆机器人ID"""
    return list(active_clones.keys())


async def stop_all_clones():
    """停止所有克隆机器人"""
    global active_clones
    
    clone_ids = list(active_clones.keys())
    for clone_id in clone_ids:
        await stop_clone_bot(clone_id)
    
    logger.info("✅ All clone bots stopped")
