"""
app/bot/state.py
----------------
Shared global state for the Telegram bot.

These module-level variables are populated by ``run_bot()`` in
``app.modules.core.routes`` at startup and should be *imported by
reference* (i.e. ``from app.bot import state``, then use
``state.global_flask_app``) by any module that needs access to the
live PTB Application or Flask app.

Do NOT do ``from app.bot.state import global_flask_app`` because that
creates a local binding that won't reflect later mutations.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application
    from flask import Flask

# Populated by run_bot(); consumers must import the *module*, not the names.
global_ptb_app: "Application | None" = None
global_bot_loop = None
global_flask_app: "Flask | None" = None
