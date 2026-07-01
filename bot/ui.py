# bot/ui.py
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict
from loguru import logger

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ForceReply,
)
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)

# ════════════════════════════════════════════════════════════════
# КОНСТАНТЫ
# ════════════════════════════════════════════════════════════════

WATCH_SYMBOL, WATCH_LONG, WATCH_SHORT, WATCH_SPREAD, WATCH_SIZE = range(5)

POPULAR_COINS = [
    ["BTC", "ETH", "SOL"],
    ["ARB", "OP", "STBL"],
    ["COS", "LYN", "SAHARA"],
    ["KITE", "AXS", "MBOX"]
]

FAST_EXCHANGES = ["BINANCE", "OKX", "BYBIT"]
SLOW_EXCHANGES = ["GATE", "KUCOIN", "COINEX", "MEXC", "APEX"]

# ════════════════════════════════════════════════════════════════
# 1. ГЛАВНОЕ МЕНЮ (Reply Keyboard)
# ════════════════════════════════════════════════════════════════

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["🛰 РАДАРЫ", "🔍 АНАЛИЗ", "🛡 GUARDIAN"],
        ["📒 ЖУРНАЛ", "🤖 ОРАКУЛ", "🛠 ИНСТРУМЕНТЫ"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👑 *ARB ASSISTANT OMNISCIENT v8*\n"
        "Все системы мониторинга и анализа активны.\n\n"
        "Выберите раздел на клавиатуре ниже 👇"
    )
    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard()
    )

async def handle_menu_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text.strip()
    
    if text == "🛰 РАДАРЫ":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Deviation (Все)", callback_data="r:dev_all")],
            [InlineKeyboardButton("📉 Dev NEG", callback_data="r:dev_neg"), InlineKeyboardButton("📈 Dev POS", callback_data="r:dev_pos")],
            [InlineKeyboardButton("🚀 Разгоны (Accel)", callback_data="r:accel")],
            [InlineKeyboardButton("🏆 Gold All", callback_data="r:gold_all")],
            [InlineKeyboardButton("🏆 Gold NEG", callback_data="r:gold_neg"), InlineKeyboardButton("🏆 Gold POS", callback_data="r:gold_pos")],
            [InlineKeyboardButton("🏹 Ramp Hunter", callback_data="r:ramp")]
        ])
        await update.effective_message.reply_text("🛰 *СИСТЕМЫ СЛЕЖЕНИЯ*", parse_mode="Markdown", reply_markup=kb)

    elif text == "🔍 АНАЛИЗ":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 FULL Analyze", callback_data="sel:analyze:MENU")],
            [InlineKeyboardButton("📈 Open Interest", callback_data="sel:oi:MENU"), InlineKeyboardButton("🛑 Margin Check", callback_data="sel:margin:MENU")],
            [InlineKeyboardButton("🔮 Predict Ramp", callback_data="sel:predict:MENU"), InlineKeyboardButton("📉 Avg Premium", callback_data="sel:avg:MENU")],
            [InlineKeyboardButton("💰 ApeX DEX", callback_data="sel:apex:MENU"), InlineKeyboardButton("💸 Fees Calc", callback_data="sel:fees:MENU")],
            [InlineKeyboardButton("📊 Funding All", callback_data="sel:funding:MENU")]
        ])
        await update.effective_message.reply_text("🔍 *ИНСТРУМЕНТЫ АНАЛИЗА*", parse_mode="Markdown", reply_markup=kb)

    elif text == "🛡 GUARDIAN":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁 Запустить нового Стража", callback_data="w_ui:start")],
            [InlineKeyboardButton("📊 Статус позиции", callback_data="j:pos")],
            [InlineKeyboardButton("⏹ Остановить всё", callback_data="w_ui:stop")]
        ])
        await update.effective_message.reply_text("🛡 *POSITION GUARDIAN*", parse_mode="Markdown", reply_markup=kb)

    elif text == "📒 ЖУРНАЛ":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Открытые позиции (/pos)", callback_data="j:pos")],
            [InlineKeyboardButton("📈 Статистика PnL (/stats)", callback_data="j:stats")],
            [InlineKeyboardButton("📜 История сделок (/history)", callback_data="j:history")],
            [InlineKeyboardButton("🧠 Одобрение правил ИИ", callback_data="j:rules")]
        ])
        await update.effective_message.reply_text("📒 *ЖУРНАЛ СУДЬБЫ*", parse_mode="Markdown", reply_markup=kb)

    elif text == "🤖 ОРАКУЛ":
        await update.effective_message.reply_text(
            "🧠 *ЗАДАТЬ ВОПРОС ОРАКУЛУ*\nОтправьте текст вопроса (например: 'анализ COS рампа') в ответ на это сообщение.",
            reply_markup=ForceReply(selective=True),
            parse_mode="Markdown"
        )
        ctx.user_data["waiting_for"] = "oracle_query"

    elif text == "🛠 ИНСТРУМЕНТЫ":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("☀️ Morning Report", callback_data="t:morning")],
            [InlineKeyboardButton("📊 Exchange Rank", callback_data="t:rank"), InlineKeyboardButton("📅 Calendar", callback_data="t:calendar")],
            [InlineKeyboardButton("🌍 Market Regime", callback_data="t:regime"), InlineKeyboardButton("⚙️ System Status", callback_data="t:status")]
        ])
        await update.effective_message.reply_text("🛠 *ТЕХНИЧЕСКИЙ ОТСЕК*", parse_mode="Markdown", reply_markup=kb)

# ════════════════════════════════════════════════════════════════
# 2. УНИВЕРСАЛЬНЫЙ ВЫБОР МОНЕТЫ
# ════════════════════════════════════════════════════════════════

async def _ask_symbol(q, ctx, cmd_tag: str, title: str):
    rows = []
    for row in POPULAR_COINS:
        rows.append([InlineKeyboardButton(s, callback_data=f"sel:{cmd_tag}:{s}") for s in row])
    rows.append([InlineKeyboardButton("⌨️ Ввести вручную", callback_data=f"sel:{cmd_tag}:CUSTOM")])
    await q.edit_message_text(f"🎯 *{title}*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

async def handle_selection_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data; await q.answer()
    parts = data.split(":")
    tag, sym = parts[1], parts[2]
    
    if sym == "MENU":
        titles = {"oi":"Open Interest", "margin":"Margin Check", "apex":"ApeX DEX", "predict":"Predict Ramp", "avg":"Avg Premium", "analyze":"Full Analyze", "fees":"Fees Calc", "funding":"Funding All"}
        await _ask_symbol(q, ctx, tag, f"Выберите монету для {titles.get(tag, tag.upper())}:")
        return

    if sym == "CUSTOM":
        await q.edit_message_text(f"⌨️ Введите символ монеты для *{tag.upper()}*:", parse_mode="Markdown", reply_markup=ForceReply(selective=True))
        ctx.user_data["waiting_for"] = f"{tag}_symbol"
        return

    ctx.args = [sym]
    # Выполнение команд
    if tag == "oi": from bot.handlers.admin import cmd_oi; await cmd_oi(update, ctx)
    elif tag == "margin": from bot.handlers.admin import cmd_margin; await cmd_margin(update, ctx)
    elif tag == "apex": from bot.handlers.dex import cmd_apex; await cmd_apex(update, ctx)
    elif tag == "predict": from bot.handlers.admin import cmd_predict; await cmd_predict(update, ctx)
    elif tag == "avg": from bot.handlers.admin import cmd_avg; await cmd_avg(update, ctx)
    elif tag == "funding": from bot.handlers.funding import cmd_funding; await cmd_funding(update, ctx)
    elif tag == "analyze": from bot.handlers.analyze import cmd_analyze; ctx.args = [sym, "binance", "gate", "1.0", "50"]; await cmd_analyze(update, ctx)
    elif tag == "fees": from bot.handlers.fees import cmd_fees; ctx.args = ["binance", "gate", "1.0", "50"]; await cmd_fees(update, ctx)

# ════════════════════════════════════════════════════════════════
# 3. ПОШАГОВЫЙ ДИАЛОГ GUARDIAN
# ════════════════════════════════════════════════════════════════

async def watch_dialog_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(s, callback_data=f"w_c:{s}") for s in row] for row in POPULAR_COINS] + 
                              [[InlineKeyboardButton("⌨️ Ввести вручную", callback_data="w_c:CUSTOM")]])
    msg = "👁 *GUARDIAN: Шаг 1/5*\nВыберите монету:"
    if update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
    else: await update.effective_message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    return WATCH_SYMBOL

# ... (остальные шаги диалога такие же, но с исправленными фильтрами в build_watch_conversation)

async def watch_sym_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer(); sym = q.data.split(":")[1]
    if sym == "CUSTOM": await q.edit_message_text("Введите символ (напр. BTC):", reply_markup=ForceReply(selective=True)); return WATCH_SYMBOL
    ctx.user_data["w_sym"] = sym; return await _ask_long(q, ctx)

async def watch_sym_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["w_sym"] = update.effective_message.text.strip().upper(); return await _ask_long(update, ctx)

async def _ask_long(upd, ctx):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(e, callback_data=f"w_l:{e.lower()}") for e in FAST_EXCHANGES]])
    txt = f"✅ Монета: {ctx.user_data['w_sym']}\n*Шаг 2/5: LONG биржа*:"
    if hasattr(upd, "edit_message_text"): await upd.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb)
    else: await upd.effective_message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)
    return WATCH_LONG

async def watch_long_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer(); ctx.user_data["w_long"] = q.data.split(":")[1]; return await _ask_short(q, ctx)

async def _ask_short(upd, ctx):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(e, callback_data=f"w_s:{e.lower()}") for e in SLOW_EXCHANGES]])
    txt = f"✅ LONG: {ctx.user_data['w_long'].upper()}\n*Шаг 3/5: SHORT биржа*:"
    await upd.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb)
    return WATCH_SHORT

async def watch_short_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer(); ctx.user_data["w_short"] = q.data.split(":")[1]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"-{i}.0%", callback_data=f"w_sp:-{i}") for i in range(1, 4)]])
    await q.edit_message_text(f"✅ SHORT: {ctx.user_data['w_short'].upper()}\n*Шаг 4/5: Спред входа (%):*", reply_markup=kb)
    return WATCH_SPREAD

async def watch_spread_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer(); ctx.user_data["w_sp"] = float(q.data.split(":")[1])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"${s}", callback_data=f"w_sz:{s}") for s in [50, 100, 200, 500]]])
    await q.edit_message_text(f"✅ Спред: {ctx.user_data['w_sp']}%\n*Шаг 5/5: Размер позиции ($):*", reply_markup=kb)
    return WATCH_SIZE

async def watch_size_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer(); size = q.data.split(":")[1]
    from bot.handlers.guardian import cmd_watch
    ctx.args = ["START", ctx.user_data["w_sym"], ctx.user_data["w_long"], ctx.user_data["w_short"], str(ctx.user_data["w_sp"]), size]
    await q.edit_message_text("🚀 Запускаю мониторинг...")
    await cmd_watch(q, ctx); return ConversationHandler.END

def build_watch_conversation():
    return ConversationHandler(
        entry_points=[CommandHandler("watch_ui", watch_dialog_start), CallbackQueryHandler(watch_dialog_start, pattern="^w_ui:start$")],
        states={
            WATCH_SYMBOL: [CallbackQueryHandler(watch_sym_cb, pattern="^w_c:"), MessageHandler(filters.TEXT & ~filters.COMMAND, watch_sym_text)],
            WATCH_LONG:   [CallbackQueryHandler(watch_long_cb, pattern="^w_l:")],
            WATCH_SHORT:  [CallbackQueryHandler(watch_short_cb, pattern="^w_s:")],
            WATCH_SPREAD: [CallbackQueryHandler(watch_spread_cb, pattern="^w_sp:")],
            WATCH_SIZE:   [CallbackQueryHandler(watch_size_cb, pattern="^w_sz:")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        allow_reentry=True
    )

# ════════════════════════════════════════════════════════════════
# 4. ГЛОБАЛЬНЫЕ ОБРАБОТЧИКИ
# ════════════════════════════════════════════════════════════════

async def handle_global_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data; await q.answer()
    
    # Радары
    if data.startswith("r:"):
        tag = data.split(":")[1]
        if tag == "dev_all": from radar.index_deviation_radar import format_deviation_dashboard; await q.message.reply_text(format_deviation_dashboard(direction="both"), parse_mode="Markdown")
        elif tag == "dev_neg": from radar.index_deviation_radar import format_deviation_dashboard; await q.message.reply_text(format_deviation_dashboard(direction="neg"), parse_mode="Markdown")
        elif tag == "dev_pos": from radar.index_deviation_radar import format_deviation_dashboard; await q.message.reply_text(format_deviation_dashboard(direction="pos"), parse_mode="Markdown")
        elif tag == "accel": from radar.index_deviation_radar import format_acceleration_report; await q.message.reply_text(format_acceleration_report(), parse_mode="Markdown")
        elif tag == "gold_all": from radar.gold_funding import get_gold_funding; await q.message.reply_text(await get_gold_funding(mode="all"), parse_mode="Markdown")
        elif tag == "gold_neg": from radar.gold_funding import get_gold_funding; await q.message.reply_text(await get_gold_funding(mode="neg"), parse_mode="Markdown")
        elif tag == "gold_pos": from radar.gold_funding import get_gold_funding; await q.message.reply_text(await get_gold_funding(mode="pos"), parse_mode="Markdown")
        elif tag == "ramp": from bot.handlers.funding import cmd_ramp; await cmd_ramp(update, ctx)

    # Инструменты и Журнал
    elif data.startswith("t:") or data.startswith("j:"):
        tag = data.split(":")[1]
        if tag == "morning": from bot.handlers.morning import build_morning_report; await q.message.reply_text(await build_morning_report(), parse_mode="Markdown")
        elif tag == "rank": from bot.handlers.rank import cmd_rank; await cmd_rank(update, ctx)
        elif tag == "calendar": from bot.main import calendar_handler; await calendar_handler(update, ctx)
        elif tag == "regime": from bot.handlers.admin import cmd_regime; await cmd_regime(update, ctx)
        elif tag == "status": from bot.handlers.admin import cmd_status; await cmd_status(update, ctx)
        elif tag == "pos": from bot.handlers.journal import cmd_pos; await cmd_pos(update, ctx)
        elif tag == "stats": from bot.handlers.journal import cmd_stats; await cmd_stats(update, ctx)
        elif tag == "history": from bot.handlers.journal import cmd_history; await cmd_history(update, ctx)
        elif tag == "rules": from bot.handlers.journal import cmd_pending_rules; await cmd_pending_rules(update, ctx)

    # Быстрые действия из алертов
    elif data.startswith("analyze:"):
        sym = data.split(":")[1]; from bot.handlers.analyze import cmd_analyze; ctx.args = [sym, "binance", "gate", "1.0", "50"]; await cmd_analyze(update, ctx)
    elif data.startswith("deep_analyze:"):
        from bot.handlers.analyze import deep_analyze_callback; await deep_analyze_callback(update, ctx)
    elif data.startswith("oi:"):
        sym = data.split(":")[1]; from bot.handlers.admin import cmd_oi; ctx.args = [sym]; await cmd_oi(update, ctx)
    elif data.startswith("apex:"):
        sym = data.split(":")[1]; from radar.apex_checker import cmd_apex; ctx.args = [sym]; await cmd_apex(update, ctx)
    elif data.startswith("watch_ui_start:"):
        ctx.user_data["w_sym"] = data.split(":")[1]; return await _ask_long(q, ctx)

async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    waiting = ctx.user_data.get("waiting_for")
    if not waiting: return
    text = update.effective_message.text.strip().upper()
    ctx.user_data.pop("waiting_for", None)
    
    if waiting == "oracle_query":
        from bot.handlers.oracle import cmd_oracle; ctx.args = text.split(); await cmd_oracle(update, ctx)
        return

    tag = waiting.replace("_symbol", "")
    ctx.args = [text]
    if tag == "oi": from bot.handlers.admin import cmd_oi; await cmd_oi(update, ctx)
    elif tag == "margin": from bot.handlers.admin import cmd_margin; await cmd_margin(update, ctx)
    elif tag == "apex": from radar.apex_checker import cmd_apex; await cmd_apex(update, ctx)
    elif tag == "predict": from bot.handlers.admin import cmd_predict; await cmd_predict(update, ctx)
    elif tag == "avg": from bot.handlers.admin import cmd_avg; await cmd_avg(update, ctx)
    elif tag == "funding": from bot.handlers.funding import cmd_funding; await cmd_funding(update, ctx)
    elif tag == "analyze": from bot.handlers.analyze import cmd_analyze; ctx.args = [text, "binance", "gate", "1.0", "50"]; await cmd_analyze(update, ctx)
    elif tag == "fees": from bot.handlers.fees import cmd_fees; ctx.args = ["binance", "gate", "1.0", "50"]; await cmd_fees(update, ctx)

def make_alert_keyboard(symbol: str, exchange: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Анализ", callback_data=f"analyze:{symbol}"), InlineKeyboardButton("👁 Watch", callback_data=f"watch_ui_start:{symbol}")],
        [InlineKeyboardButton("💰 ApeX", callback_data=f"apex:{symbol}"), InlineKeyboardButton("📈 OI", callback_data=f"oi:{symbol}")]
    ])
