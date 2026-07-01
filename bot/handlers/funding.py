# bot/handlers/funding.py
from telegram import Update
from telegram.ext import ContextTypes
import asyncio
from loguru import logger


async def cmd_funding(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /funding SYMBOL — фандинг по символу на всех биржах + OFI
    """
    if not ctx.args:
        await update.effective_message.reply_text(
            "Использование: /funding SYMBOL\nПример: /funding SOL"
        )
        return

    symbol = ctx.args[0].upper()
    msg = await update.effective_message.reply_text(f"📡 Получаю данные {symbol}...")

    from data.exchanges import get_all_rates, get_orderbook_depth
    from hunter.math_engine import calc_ofi

    rates = await get_all_rates(symbol)
    if not rates:
        await msg.edit_text(f"❌ Нет данных по {symbol}")
        return

    lines = [f"💰 *FUNDING {symbol}*", "─" * 30]
    sorted_rates = sorted(rates.items(), key=lambda x: -x[1])

    for ex, rate in sorted_rates:
        bar = "🔴" if rate > 0.3 else ("🟡" if rate > 0.1 else "🟢")
        lines.append(f"{bar} {ex.upper():10} {rate:+.4f}%")

    if len(sorted_rates) >= 2:
        max_ex, max_r = sorted_rates[0]
        min_ex, min_r = sorted_rates[-1]
        diff = round(max_r - min_r, 5)

        # OFI для топ-2
        ob_max = await get_orderbook_depth(max_ex, symbol)
        ob_min = await get_orderbook_depth(min_ex, symbol)
        ofi_max = calc_ofi(ob_max.get("bids", []), ob_max.get("asks", []))
        ofi_min = calc_ofi(ob_min.get("bids", []), ob_min.get("asks", []))

        lines += [
            "",
            f"📊 Лучший спред: {min_ex.upper()} → {max_ex.upper()}",
            f"DIFF: {diff:.4f}%",
            f"OFI {max_ex[:4]}: {ofi_max:+.2f} | OFI {min_ex[:4]}: {ofi_min:+.2f}",
        ]

    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


async def _analyze_single_ramp(sym: str, RAMP_EXCHANGES: list):
    """Вспомогательная функция для параллельного анализа."""
    from radar.ramp_hunter import premium_store, detect_ramp_hunter, fetch_all_premiums, check_price_context
    try:
        snaps = await asyncio.wait_for(fetch_all_premiums(sym, RAMP_EXCHANGES), 20)
        if not snaps: return None
        
        fast_snaps = [s["premium"] for ex, s in snaps.items() if ex in ["binance", "okx", "bybit"]]
        if not fast_snaps: return None
        
        avg_premium = sum(fast_snaps) / len(fast_snaps)
        velocity = premium_store.get_velocity(sym)
        rates = {ex: s["rate"] for ex, s in snaps.items()}
        
        sig = detect_ramp_hunter(sym, rates, velocity, avg_premium)
        if sig:
            # Сразу подтягиваем контекст цены
            context = await check_price_context(sym)
            sig.is_at_high = context["is_high"]
            sig.price_change_24h = context["change"]
            return sig
    except:
        pass
    return None


async def cmd_ramp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /ramp — показывает топ монет с самым высоким Momentum (Ramp Hunter)
    """
    msg = await update.effective_message.reply_text("📡 Анализирую Momentum топ-15 монет...")
    
    from data.exchanges import get_all_active_symbols
    from radar.ramp_hunter import format_ramp_alert
    
    try:
        # Берем топ-15 активных символов для скорости
        symbols = await get_all_active_symbols(min_exchanges=3)
        symbols = symbols[:15]
        
        RAMP_EXCHANGES = ["binance", "okx", "bybit", "gate", "kucoin"]
        
        # Запускаем всё параллельно с общим таймаутом
        tasks = [_analyze_single_ramp(sym, RAMP_EXCHANGES) for sym in symbols]
        results = await asyncio.gather(*tasks)
        
        found_signals = [r for r in results if r is not None]
        
        if not found_signals:
            await msg.edit_text("📭 Активных Momentum-сигналов сейчас нет.\nПопробуй через 15-30 минут.")
            return
            
        # Сортируем: сначала те, где вход сейчас, затем по velocity
        found_signals.sort(key=lambda x: (x.entry_now, -x.premium_velocity), reverse=True)
        
        # Отправляем топ-3
        for sig in found_signals[:3]:
            await update.effective_message.reply_text(format_ramp_alert(sig), parse_mode="Markdown")
            
        await msg.delete()
        
    except Exception as e:
        logger.error(f"Error in cmd_ramp: {e}")
        await msg.edit_text(f"❌ Ошибка анализа: {e}")
