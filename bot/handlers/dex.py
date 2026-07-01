# bot/handlers/dex.py
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from core.database import is_on_dex
from radar.apex_checker import format_apex_block, refresh_apex, _store
from radar.dex_db import dex_db

async def cmd_apex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Проверка монеты на ApeX Omni."""
    if not ctx.args:
        await update.effective_message.reply_text("❌ Укажите символ монеты. Пример: `/apex SAHARA`")
        return

    sym = ctx.args[0].upper()
    dex_list = await is_on_dex(sym)
    
    if "apex" not in dex_list:
        if update.callback_query:
            await update.callback_query.edit_message_text(f"❌ Монеты *{sym}* нет в реестре ApeX Omni.", parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(f"❌ Монеты *{sym}* нет в реестре ApeX Omni.", parse_mode="Markdown")
        return

    active_sym = sym
    if sym.endswith("U") and sym != "LITU": active_sym = sym[:-1]
    
    if not _store.is_fresh():
        await update.effective_message.reply_text(f"⏳ Обновляю данные ApeX для {active_sym}...")
        await refresh_apex()
    
    text = format_apex_block(active_sym, "okx", -0.01)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown")

async def cmd_dexoi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Мгновенная проверка OI на CEX через родной работающий модуль oi_monitor.
    """
    if not ctx.args:
        await update.effective_message.reply_text("❌ Укажите символ. Пример: `/dexoi SAHARA`")
        return

    sym = ctx.args[0].upper()
    from radar.dex_db import _clean_ticker
    clean_sym = _clean_ticker(sym)
    
    # 1. Проверяем DEX
    dex_line = dex_db.format_dex_line(clean_sym) or "❌ Нет на DEX"
    
    # 2. Вызываем РОДНУЮ команду /oi для получения данных
    # Она сама умеет собирать OI и форматировать его
    from radar.oi_monitor import cmd_oi
    
    # Подменяем аргументы для cmd_oi, чтобы она искала конкретную монету
    ctx.args = [clean_sym]
    
    # Сначала выводим DEX-инфо
    await update.effective_message.reply_text(f"🛰 *DEX Registry: {clean_sym}*\n{dex_line}", parse_mode="Markdown")
    
    # Затем запускаем стандартный парсер OI
    await cmd_oi(update, ctx)
