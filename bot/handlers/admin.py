# bot/handlers/admin.py
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict
from loguru import logger

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.config import settings
from core.database import get_db_size, get_chunk_count, get_uptime
from oracle.regime import get_regime_info

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from bot.ui import cmd_menu
    await cmd_menu(update, ctx)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *СПРАВОЧНИК КОМАНД*\n\n"
        "🛰 *РАДАРЫ*\n"
        "  /dev — Скан отклонений\n"
        "  /minus — Только дисконты (отриц. откл)\n"
        "  /accel — Топ разгонов (Velocity)\n"
        "  /gold — Золотые фандинги\n"
        "  /funding — Мониторинг ставок\n"
        "  /ramp — Охотник за рампами\n"
        "  /oi [sym] — Анализ Open Interest\n\n"
        "🔍 *АНАЛИЗ*\n"
        "  /analyze [sym] — Глубокий разбор\n"
        "  /dexoi [sym] — OI для DEX-арбитража\n"
        "  /apex [sym] — Данные ApeX Omni\n"
        "  /dex [sym] — Реестр 10+ DEX\n"
        "  /fees — Калькулятор комиссий\n\n"
        "📒 *ЖУРНАЛ*\n"
        "  /pos — Мои позиции\n"
        "  /stats — Статистика PnL\n"
        "  /history — История сделок\n\n"
        "⚙️ *СИСТЕМА*\n"
        "  /status — Тех. состояние\n"
        "  /regime — Режим рынка\n"
        "  /morning — Утренний отчет\n"
        "  /calendar — Листинги монет\n"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from core.redis_cache import get_redis
    redis_ok = False
    try:
        r = await get_redis()
        if r:
            await r.ping()
            redis_ok = True
    except:
        pass

    chunks = get_chunk_count()
    db_rows = await get_db_size()
    regime = await get_regime_info()
    uptime = get_uptime()
    groq_ok = bool(settings.groq_api_key)

    text = (
        f"⚙️ *STATUS ARB ASSISTANT*\n"
        f"{'─'*30}\n"
        f"Redis:  {'✅ OK' if redis_ok else '❌ FAIL (fallback)'}\n"
        f"FAISS:  ✅ {chunks} чанков\n"
        f"DB:     ✅ {db_rows} сделок\n"
        f"Groq:   {'✅ OK' if groq_ok else '⚠️ нет ключа'}\n"
        f"Режим:  {regime['name']}\n"
        f"Аптайм: {uptime}\n"
        f"RAM:    ~350MB"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def cmd_regime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from oracle.regime import get_regime_info
    info = await get_regime_info()

    emoji = {"PANIC": "🔴", "EUPHORIA": "🟢", "TREND": "🟡", "SIDEWAYS": "🔵"}
    e = emoji.get(info["name"], "⚪")
    allowed = ", ".join(info.get("allowed", [])) or "нет"

    btc = f"BTC: {info.get('btc_price', '?')}$  {info.get('change_4h', '?'):+}% 4h"

    text = (
        f"{e} *MARKET REGIME: {info['name']}*\n"
        f"{'─'*30}\n"
        f"{info.get('description', '')}\n\n"
        f"{btc}\n\n"
        f"✅ Разрешено: {allowed}"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def cmd_margin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from radar.margin_monitor import (
        format_margin_list, format_margin_info_for_symbol, 
        MARGIN_EXCHANGES, ensure_margin_data
    )
    
    # FIX: Если данных нет, быстро сканим
    await ensure_margin_data()
    
    if not ctx.args:
        # По умолчанию — список Bybit
        text = format_margin_list("bybit", top_n=20)
        await update.effective_message.reply_text(f"`{text}`", parse_mode="Markdown")
        return

    arg = ctx.args[0].lower()
    
    # 1. Если это биржа — показываем список
    if arg in MARGIN_EXCHANGES or arg in ["bybit", "binance", "okx", "gateio"]:
        text = format_margin_list(arg, top_n=20)
        await update.effective_message.reply_text(f"`{text}`", parse_mode="Markdown")
    else:
        # 2. Иначе считаем, что это символ монеты
        symbol = arg.upper()
        text = await format_margin_info_for_symbol(symbol)
        await update.effective_message.reply_text(f"{text}", parse_mode="Markdown")


async def cmd_deviation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from radar.index_deviation_radar import format_deviation_dashboard, format_deviation_by_symbol, ensure_deviation_data
    # FIX: Если данных нет, быстро сканим
    await ensure_deviation_data()

    command_name = update.effective_message.text.split()[0].lower().replace("/", "")
    
    if not ctx.args:
        if command_name in ["minus", "neg"]:
            text = format_deviation_dashboard(direction="neg")
        else:
            text = format_deviation_dashboard(direction="both")
    elif ctx.args[0].lower() in ["neg", "minus"]:
        text = format_deviation_dashboard(direction="neg")
    elif ctx.args[0].lower() in ["pos", "plus"]:
        text = format_deviation_dashboard(direction="pos")
    else:
        arg = ctx.args[0].upper()
        text = format_deviation_by_symbol(arg)

    await update.effective_message.reply_text(f"`{text}`" if len(text) > 100 else text, parse_mode="Markdown")


async def cmd_acceleration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from radar.index_deviation_radar import format_acceleration_report, ensure_deviation_data
    # FIX: холодный старт
    await ensure_deviation_data()

    text = format_acceleration_report()
    await update.effective_message.reply_text(f"`{text}`", parse_mode="Markdown")


async def cmd_oi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from radar.oi_monitor import cmd_oi as oi_handler
    await oi_handler(update, ctx)


async def cmd_avg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from radar.index_avg_analyzer import cmd_avg as avg_handler
    await avg_handler(update, ctx)


async def cmd_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from radar.index_avg_analyzer import cmd_predict as predict_handler
    await predict_handler(update, ctx)

async def cmd_chat_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID этого чата: `{update.effective_chat.id}`", parse_mode="Markdown")
