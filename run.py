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


def run_django():
    """Run Django server"""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
    from django.core.management import execute_from_command_line
    
    port = int(os.getenv('PORT', 8000))
    execute_from_command_line(['manage.py', 'runserver', f'0.0.0.0:{port}'])


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
        # Run both in separate threads
        django_thread = threading.Thread(target=run_django, daemon=True)
        django_thread.start()
        
        # Run bot in main thread
        run_bot()


if __name__ == '__main__':
    main()