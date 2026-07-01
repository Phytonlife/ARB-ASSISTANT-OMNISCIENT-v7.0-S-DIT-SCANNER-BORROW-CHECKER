# radar/scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from datetime import datetime
import asyncio

_scheduler = AsyncIOScheduler()

def setup_scheduler(app):
    """Настройка всех фоновых задач."""
    from bot.ui import cmd_menu
    
    # 1. Скан отклонений (Index Deviation) - каждые 15 минут
    _scheduler.add_job(
        _deviation_job, "interval", minutes=15,
        id="deviation_scan",
        kwargs={"app": app},
        next_run_time=datetime.now()
    )

    # 2. Мониторинг OI - каждые 15 минут (смещение от отклонений)
    _scheduler.add_job(
        _oi_job, "interval", minutes=15,
        id="oi_monitor",
        kwargs={"app": app},
        next_run_time=datetime.now()
    )

    # 3. Ramp Hunter - каждые 15 минут
    _scheduler.add_job(
        _ramp_job, "interval", minutes=15,
        id="ramp_hunter",
        kwargs={"app": app},
        next_run_time=datetime.now()
    )

    # 4. Мониторинг Маржи - каждые 5 минут
    _scheduler.add_job(
        _margin_job, "interval", minutes=5,
        id="margin_monitor",
        kwargs={"app": app},
        next_run_time=datetime.now()
    )

    # 5. Утренний отчет (8:00 UTC)
    _scheduler.add_job(
        _morning_job, "cron", hour=8, minute=0,
        id="morning_report",
        kwargs={"app": app}
    )

    # 6. Обновление режима рынка - каждые 5 минут
    _scheduler.add_job(
        _regime_job, "interval", minutes=5,
        id="regime_update",
    )

    # 7. Обновление реестра ApeX - каждые 30 минут
    _scheduler.add_job(
        _apex_refresh_job, "interval", minutes=30,
        id="apex_refresh",
        next_run_time=datetime.now()
    )

    # 8. DEX Registry Scan: 1 раз в сутки
    _scheduler.add_job(
        _dex_scan_job, "cron", hour=3, minute=0,
        id="dex_assets_scan",
        next_run_time=datetime.now()
    )

    # 9. Proactive DEX-CEX OI Hunter: Каждые 5 минут
    _scheduler.add_job(
        _dex_proactive_oi_job, "interval", minutes=5,
        id="dex_proactive_oi",
        kwargs={"app": app},
        next_run_time=datetime.now()
    )

    # 10. Очистка истории займов - каждый час
    _scheduler.add_job(
        _borrow_history_gc_job, "interval", hours=1,
        id="borrow_history_gc",
    )

    # 11. Очистка RampDB снимков - каждые 6 часов
    _scheduler.add_job(
        _rampdb_cleanup_job, "interval", hours=6,
        id="rampdb_cleanup",
    )

    _scheduler.start()
    logger.info("Scheduler: ALL JOBS ACTIVE")


async def _deviation_job(app):
    """Задача сканирования отклонений индекса."""
    from radar.index_deviation_radar import deviation_scan, format_alerts_batch
    from core.config import settings
    
    try:
        signals = await deviation_scan()
        # format_alerts_batch теперь асинхронный
        alerts = await format_alerts_batch(signals)
        
        for text, kb in alerts:
            try:
                await app.bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to send deviation alert: {e}")
    except Exception as e:
        logger.error(f"[Scheduler] deviation_job failed: {e}")


async def _oi_job(app):
    from radar.oi_monitor import oi_scan, format_oi_alert
    from core.config import settings
    try:
        signals = await oi_scan()
        for sig in signals:
            text, kb = format_oi_alert(sig)
            await app.bot.send_message(chat_id=settings.telegram_chat_id, text=text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e: logger.error(f"[Scheduler] oi_job failed: {e}")

async def _ramp_job(app):
    from radar.ramp_hunter import ramp_scan, format_ramp_alert
    from core.config import settings
    try:
        signals = await ramp_scan()
        for sig in signals:
            text, kb = format_ramp_alert(sig)
            await app.bot.send_message(chat_id=settings.telegram_chat_id, text=text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e: logger.error(f"[Scheduler] ramp_job failed: {e}")

async def _margin_job(app):
    from radar.margin_monitor import margin_scan, format_margin_event
    from core.config import settings
    try:
        events = await margin_scan()
        for ev in events:
            text = format_margin_event(ev)
            await app.bot.send_message(chat_id=settings.telegram_chat_id, text=text, parse_mode="Markdown")
    except Exception as e: logger.error(f"[Scheduler] margin_job failed: {e}")

async def _morning_job(app):
    from bot.handlers.morning import get_morning_report
    from core.config import settings
    try:
        text = await get_morning_report()
        await app.bot.send_message(chat_id=settings.telegram_chat_id, text=text, parse_mode="Markdown")
    except Exception as e: logger.error(f"[Scheduler] morning_job failed: {e}")

async def _regime_job():
    from oracle.regime import update_regime
    try: await update_regime()
    except Exception as e: logger.error(f"[Scheduler] regime_job failed: {e}")

async def _apex_refresh_job():
    from radar.apex_checker import refresh_apex
    try: await refresh_apex()
    except Exception as e: logger.error(f"[Scheduler] apex_refresh_job failed: {e}")

async def _dex_scan_job():
    from radar.dex_db import dex_db
    try: await dex_db.refresh_all()
    except Exception as e: logger.error(f"[Scheduler] dex_scan_job failed: {e}")

async def _dex_proactive_oi_job(app):
    from radar.dex_oi_hunter import dex_proactive_scan
    try: await dex_proactive_scan(app)
    except Exception as e: logger.error(f"[Scheduler] dex_proactive_oi_job failed: {e}")

async def _borrow_history_gc_job():
    from radar.borrow_checker import garbage_collect_borrow_history
    try: await garbage_collect_borrow_history()
    except Exception as e: logger.error(f"[Scheduler] borrow_history_gc_job failed: {e}")

async def _rampdb_cleanup_job():
    from radar.ramp_memory import db
    try:
        db.cleanup_old_snapshots(keep_hours=48.0)
    except Exception as e:
        logger.error(f"[Scheduler] rampdb_cleanup_job failed: {e}")

def stop_scheduler():
    if _scheduler.running: _scheduler.shutdown()
