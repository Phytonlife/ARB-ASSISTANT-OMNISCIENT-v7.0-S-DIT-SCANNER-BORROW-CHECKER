# bot/main.py
import asyncio
from loguru import logger
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from core.config import settings
from core.database import init_db
from oracle.rag_memory import build_index

# Импорт всех хэндлеров для ручного управления
from bot.handlers.admin import (
    cmd_start, cmd_help, cmd_status, cmd_regime, cmd_chat_id, 
    cmd_margin, cmd_deviation, cmd_acceleration, cmd_oi, cmd_avg, cmd_predict
)
from bot.handlers.analyze import cmd_analyze, oracle_callback
from bot.handlers.oracle import cmd_oracle
from bot.handlers.funding import cmd_funding, cmd_ramp
from radar.gold_funding import cmd_gold
from bot.handlers.dex import cmd_apex, cmd_dexoi
from radar.dex_db import cmd_dex
from bot.handlers.guardian import cmd_watch
from bot.handlers.fees import cmd_fees
from bot.handlers.morning import cmd_morning
from bot.handlers.rank import cmd_rank
from bot.handlers.journal import (
    cmd_open, cmd_close, cmd_pos, cmd_stats, cmd_history,
    cmd_pending_rules, cmd_approve_rule,
)
from radar.gate_ramp_radar import run_gate_ramp_radar, cmd_gate as cmd_gate_ramp, stop as stop_gate_ramp_radar
from radar.ramp_memory import cmd_memory, db

# НОВЫЙ UI (кнопки)
from bot.ui import (
    cmd_menu, handle_menu_button, handle_global_callback,
    handle_selection_callback, handle_text_input, build_watch_conversation
)

async def post_init(app: Application):
    await init_db()
    logger.info("Database initialized in post_init")
    db.connect()
    logger.info("RampDB connected")
    await build_index()
    logger.info("FAISS index ready")
    from radar.scheduler import setup_scheduler
    setup_scheduler(app)
    
    # Gate Ramp Radar v3
    if settings.telegram_chat_id:
        async def send_fn(chat_id: str, text: str):
            try:
                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=text,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"GateRamp send error: {e}")
        
        task = asyncio.create_task(
            run_gate_ramp_radar(send_fn, str(settings.telegram_chat_id))
        )
        app.bot_data["gate_radar_task"] = task
        logger.info(f"Gate Ramp Radar v3 task created: {task}")

async def calendar_handler(update, ctx):
    from data.listings import get_upcoming_listings
    listings = await get_upcoming_listings(days=7)
    if not listings: await update.message.reply_text("📭 Листингов не найдено"); return
    lines = [f"📅 *Листинги (7 дней)*", "─" * 30]
    for lst in listings[:10]:
        coins = ", ".join(lst.get("coins", [])[:3])
        lines.append(f"• {lst.get('date', '')[:10]}: {lst.get('title', '')}\n  Монеты: {coins}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def post_shutdown(app: Application):
    from radar.borrow_checker import close_session
    await close_session()
    
    # Останавливаем Gate Radar
    stop_gate_ramp_radar()
    task = app.bot_data.get("gate_radar_task")
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    
    logger.info("Aiohttp session closed and GateRamp stopped")

def main():
    if not settings.telegram_token or settings.telegram_token.startswith("1234567"):
        logger.error("TELEGRAM_TOKEN не настроен!"); return

    app = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # 1. Диалог Guardian (высокий приоритет)
    app.add_handler(build_watch_conversation())

    # 2. Кнопки Главного Меню (Reply Keyboard)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^(🛰 РАДАРЫ|🔍 АНАЛИЗ|🛡 GUARDIAN|📒 ЖУРНАЛ|🤖 ОРАКУЛ|🛠 ИНСТРУМЕНТЫ)$"),
        handle_menu_button
    ))

    # 3. ВСЕ КОМАНДЫ (РУЧНОЕ УПРАВЛЕНИЕ)
    app.add_handler(CommandHandler("start", cmd_menu))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("regime", cmd_regime))
    
    app.add_handler(CommandHandler("oi", cmd_oi))
    app.add_handler(CommandHandler("margin", cmd_margin))
    app.add_handler(CommandHandler("dev", cmd_deviation))
    app.add_handler(CommandHandler("deviation", cmd_deviation))
    app.add_handler(CommandHandler("minus", cmd_deviation))
    app.add_handler(CommandHandler("neg", cmd_deviation))
    app.add_handler(CommandHandler("accel", cmd_acceleration))
    app.add_handler(CommandHandler("acceleration", cmd_acceleration))
    app.add_handler(CommandHandler("gold", cmd_gold))
    app.add_handler(CommandHandler("ramp", cmd_ramp))
    app.add_handler(CommandHandler("funding", cmd_funding))
    
    app.add_handler(CommandHandler("apex", cmd_apex))
    app.add_handler(CommandHandler("dex", cmd_dex))
    app.add_handler(CommandHandler("dexoi", cmd_dexoi))
    
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("oracle", cmd_oracle))
    app.add_handler(CommandHandler("gate", cmd_gate_ramp))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("fees", cmd_fees))
    app.add_handler(CommandHandler("morning", cmd_morning))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("calendar", calendar_handler))
    app.add_handler(CommandHandler("watch", cmd_watch))
    
    app.add_handler(CommandHandler("avg", cmd_avg))
    app.add_handler(CommandHandler("predict", cmd_predict))
    
    app.add_handler(CommandHandler("pos", cmd_pos))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("rules", cmd_pending_rules))

    # 4. Обработка Выбора Монет (Inline sel:)
    app.add_handler(CallbackQueryHandler(handle_selection_callback, pattern="^sel:"))

    # 5. Обработка Глобальных кнопок (Inline t: и j: и r: и алерты)
    app.add_handler(CallbackQueryHandler(handle_global_callback))

    # 6. Обработка ForceReply (текстовый ввод)
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, handle_text_input))

    logger.info("Bot started with ALL MANUAL COMMANDS restored. Listening...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
