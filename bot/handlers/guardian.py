# bot/handlers/guardian.py
# POSITION GUARDIAN v4.1 — TELEGRAM HANDLER
# Интеграция с radar/position_guardian.py

from telegram import Update
from telegram.ext import ContextTypes
from radar.position_guardian import cmd_watch as guardian_cmd

async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Прокси для cmd_watch из radar/position_guardian.py.
    """
    await guardian_cmd(update, context)
