#!/usr/bin/env python
"""
Entry point for running the application.

This script can:
1. Run the Django development server
2. Run the Telegram bot
3. Run both together (for production)
"""

import os
import sys
import threading


def run_django(use_reloader=True):
    """Run Django server
    
    Args:
        use_reloader: Whether to use Django's autoreloader. Must be False when
                      running in a non-main thread.
    """
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
    from django.core.management import execute_from_command_line
    
    port = int(os.getenv('PORT', 8000))
    cmd = [sys.argv[0], 'runserver', f'0.0.0.0:{port}']
    if not use_reloader:
        cmd.append('--noreload')
    execute_from_command_line(cmd)


def is_bot_configured():
    """Check if Telegram bot token is configured and not empty"""
    return bool(os.getenv('TELEGRAM_BOT_TOKEN'))


def run_bot():
    """Run Telegram bot"""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
    
    # Wait for Django to be ready
    import time
    time.sleep(3)
    
    from core.main import main
    main()


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Run the application')
    parser.add_argument('--mode', choices=['web', 'bot', 'both'], default='both',
                       help='Running mode: web (Django), bot (Telegram), both')
    args = parser.parse_args()
    
    print(f"🚀 Starting in {args.mode} mode...")
    
    if args.mode == 'web':
        run_django()
    elif args.mode == 'bot':
        run_bot()
    else:
        # Check if bot is configured
        if is_bot_configured():
            # Run both in separate threads
            # Disable autoreload when running Django in a thread (signal handlers only work in main thread)
            django_thread = threading.Thread(target=run_django, args=(False,), daemon=True)
            django_thread.start()
            
            # Run bot in main thread
            run_bot()
        else:
            # Bot token not configured, only run Django
            print("ℹ️ TELEGRAM_BOT_TOKEN not configured, starting web server only.")
            run_django()


if __name__ == '__main__':
    main()