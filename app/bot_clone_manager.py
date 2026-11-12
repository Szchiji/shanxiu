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
from telegram import Update
from telegram.ext import Application
from telegram.error import TimedOut
from telegram.request import HTTPXRequest
from datetime import datetime
import traceback

# Constants
RESTART_DELAY_SECONDS = 1  # Brief delay before restart to ensure clean shutdown
CLONE_HTTP_TIMEOUT = 20
CLONE_START_ATTEMPTS = 3
CLONE_RETRY_DELAY = 2
CLONE_ATTEMPT_TIMEOUT = 60  # Bound the entire initialize/start/webhook sequence.
CLONE_CLEANUP_TIMEOUT = 10
CLONE_STOP_BUDGET = 30
CLONE_START_BUDGET = (CLONE_START_ATTEMPTS *
                      (CLONE_ATTEMPT_TIMEOUT + CLONE_CLEANUP_TIMEOUT) +
                      (CLONE_START_ATTEMPTS - 1) * CLONE_RETRY_DELAY)

# 存储所有运行中的克隆Bot实例
# Key: clone_id (int), Value: {'app': Application, 'loop_task': Task or None, 'mode': str}
active_clones: Dict[int, dict] = {}

logger = logging.getLogger(__name__)


async def start_clone_bot(clone_id: int, bot_token: str, webhook_url: Optional[str] = None,
                          flask_app=None, handlers_setup_func=None, post_init_func=None):
    """
    启动一个克隆机器人实例
    
    Args:
        clone_id: 克隆机器人的数据库ID
        bot_token: Bot token
        webhook_url: Webhook URL (如果使用webhook模式)
        flask_app: Flask应用实例 (用于数据库访问)
        handlers_setup_func: 设置处理器的函数 (接收Application实例)
        post_init_func: 可选的异步函数，在 initialize() 完成后调用，参数为 bot 实例
    
    Returns:
        bool: 成功返回True, 失败返回False
    """
    global active_clones

    # Clone tokens appear in Telegram API URLs; make sure log scrubbing is active.
    from app.log_redaction import install_log_redaction
    install_log_redaction(wrap_std_streams=False)
    
    # Check if already running
    if clone_id in active_clones:
        logger.warning(f"Clone bot {clone_id} is already running")
        return True
    
    async def run_attempt(app):
        # Setup handlers if provided
        if handlers_setup_func:
            handlers_setup_func(app, flask_app, clone_id)
        
        # Initialize and start
        await app.initialize()
        await app.start()

        # Run optional post-init hook (e.g. set_my_commands)
        if post_init_func:
            try:
                await post_init_func(app.bot)
            except Exception as _pie:
                logger.warning(f"post_init_func for clone {clone_id} failed: {_pie}")
        
        # Build explicit allowed_updates list so my_chat_member is always received.
        # Exclude high-volume types that are not needed by the bot to keep traffic low.
        _all_allowed = [t.value for t in Update.ALL_TYPES]

        # Determine mode: Webhook or Polling
        if webhook_url:
            # Webhook mode
            logger.info(f"Clone bot {clone_id} using Webhook mode: {webhook_url}")
            await app.bot.set_webhook(webhook_url, allowed_updates=_all_allowed)
            # Store without starting updater
            active_clones[clone_id] = {
                'app': app,
                'loop_task': None,
                'mode': 'webhook'
            }
        else:
            # Polling mode — do NOT drop pending updates so that my_chat_member
            # events queued while the bot was offline are still processed.
            logger.info(f"Clone bot {clone_id} using Polling mode")
            await app.updater.start_polling(
                drop_pending_updates=False,
                allowed_updates=_all_allowed,
            )

            active_clones[clone_id] = {
                'app': app,
                'loop_task': None,
                'mode': 'polling'
            }

    async def cleanup(app, requests):
        try:
            if app is not None:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
        finally:
            # PTB shutdown is a no-op after partial initialize failure.
            # Close both owned transports even when lifecycle cleanup raises.
            await asyncio.gather(*(request.shutdown() for request in requests))

    for attempt in range(1, CLONE_START_ATTEMPTS + 1):
        app = None
        requests = []
        retry = False
        try:
            logger.info(f"🤖 Starting clone bot {clone_id} (attempt {attempt})...")
            for _ in range(2):
                requests.append(HTTPXRequest(
                    connect_timeout=CLONE_HTTP_TIMEOUT,
                    read_timeout=CLONE_HTTP_TIMEOUT,
                    write_timeout=CLONE_HTTP_TIMEOUT,
                    pool_timeout=CLONE_HTTP_TIMEOUT,
                ))
            app = (Application.builder().token(bot_token)
                   .request(requests[0]).get_updates_request(requests[1]).build())
            await asyncio.wait_for(run_attempt(app), timeout=CLONE_ATTEMPT_TIMEOUT)
            logger.info(f"✅ Clone bot {clone_id} started successfully")
            return True
        except (TimedOut, asyncio.TimeoutError):
            retry = attempt < CLONE_START_ATTEMPTS
            logger.warning(f"Clone bot {clone_id} startup timed out ({attempt}/{CLONE_START_ATTEMPTS})"
                           + ("; retrying" if retry else "; giving up"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"❌ Failed to start clone bot {clone_id}")
        finally:
            if clone_id not in active_clones:
                try:
                    await asyncio.wait_for(cleanup(app, requests), CLONE_CLEANUP_TIMEOUT)
                except Exception:
                    logger.exception(f"Failed to clean up clone bot {clone_id}")
                    retry = False  # Never create another app while cleanup is incomplete.
        if not retry:
            return False
        await asyncio.sleep(CLONE_RETRY_DELAY)
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
    try:
        if not await asyncio.wait_for(stop_clone_bot(clone_id), CLONE_STOP_BUDGET):
            return False
    except asyncio.TimeoutError:
        logger.warning(f"Clone bot {clone_id} stop timed out before restart")
        return False
    await asyncio.sleep(RESTART_DELAY_SECONDS)  # Brief delay before restart
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
