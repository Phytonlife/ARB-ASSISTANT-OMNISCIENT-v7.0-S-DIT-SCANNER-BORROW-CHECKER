# bot/handlers/analyze.py
# /analyze → Hunter pipeline + checklist 12 пунктов + кнопки

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from hunter.math_engine import calc_net_spread, calc_ofi, estimate_slippage, score_signal, check_ticker_trap
from hunter.risk_engine import check_risk
from hunter.execution import get_order_type
from data.exchanges import get_orderbook_depth
from oracle.regime import detect_regime
from bot.keyboards import signal_actions_kb
from radar.margin_monitor import format_margin_info_for_symbol
from radar.index_deviation_radar import format_deviation_by_symbol
from radar.oi_monitor import format_oi_confirmation
from radar.index_avg_analyzer import format_avg_multi


def _checklist(sig: dict) -> str:
    items = []
    ok = "✅"
    no = "❌"
    warn = "⚠️"

    items.append(f"{ok if sig['diff'] >= 0.175 else no} diff {sig['diff']:.3f}% >= 0.175%")
    items.append(f"{ok if sig['net'] >= 0.10 else no} net {sig['net']:.3f}% >= 0.10%")
    items.append(f"{ok if sig['net'] >= 0.30 else warn} net {sig['net']:.3f}% >= 0.30% (хорошо)")
    items.append(f"{ok if not sig.get('ex_a_blacklist') else no} ex_a не в BLACKLIST")
    items.append(f"{ok if not sig.get('ex_b_blacklist') else no} ex_b не в BLACKLIST")
    items.append(f"{ok if sig['risk_ok'] else no} риск-лимиты: {sig['risk_reason']}")
    items.append(f"{ok if sig['ofi_long'] > 0.1 else warn} OFI лонг {sig['ofi_long']:+.2f}")
    items.append(f"{ok if sig['slip_a'] < 0.3 else warn} слипаж лонг {sig['slip_a']:.3f}%")
    items.append(f"{ok if sig['slip_b'] < 0.3 else warn} слипаж шорт {sig['slip_b']:.3f}%")
    items.append(f"{ok if sig['regime_ok'] else no} режим: {sig['regime_name']}")
    items.append(f"{ok if sig['order_ok'] else no} тип ордера: {sig['order_type']}")
    items.append(f"{warn} войти ПОСЛЕ выплаты фандинга")

    return "\n".join(items)


async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /analyze SYMBOL EX_A EX_B DIFF SIZE
    Пример: /analyze ORCA okx binance 0.52 50
    """
    args = ctx.args
    if len(args) < 5:
        await update.message.reply_text(
            "Использование: /analyze SYMBOL EX_A EX_B DIFF SIZE\n"
            "Пример: /analyze ORCA okx binance 0.52 50"
        )
        return

    symbol = args[0].upper()
    ex_a, ex_b = args[1].lower(), args[2].lower()

    try:
        diff = float(args[3])
        size = float(args[4])
    except ValueError:
        await update.message.reply_text("DIFF и SIZE должны быть числами")
        return

    # FIX: проверяем ловушку тикера
    trap = check_ticker_trap(diff)
    if trap["trap"]:
        await update.message.reply_text(trap["warning"])
        return

    msg = await update.message.reply_text("🔍 Анализирую...")

    # Собираем данные
    net_r = calc_net_spread(diff, ex_a, ex_b, size, "perp", "perp", False)
    ob_a = await get_orderbook_depth(ex_a, symbol)
    ob_b = await get_orderbook_depth(ex_b, symbol)

    ofi_a = calc_ofi(ob_a.get("bids", []), ob_a.get("asks", []))
    ofi_b = calc_ofi(ob_b.get("bids", []), ob_b.get("asks", []))
    slip_a = estimate_slippage(size, ob_a.get("depth_usd", 9999))
    slip_b = estimate_slippage(size, ob_b.get("depth_usd", 9999))

    risk = await check_risk(size, ex_a, ex_b)
    regime = await detect_regime()
    regime_ok = "funding_arb" in regime.allowed

    ot_a = get_order_type(ex_a, size, ob_a.get("depth_usd", 9999))
    ot_b = get_order_type(ex_b, size, ob_b.get("depth_usd", 9999))

    from data.fees import BLACKLIST
    sig_data = {
        "diff": diff,
        "net": net_r.get("net", 0),
        "ex_a_blacklist": ex_a in BLACKLIST,
        "ex_b_blacklist": ex_b in BLACKLIST,
        "risk_ok": risk.ok,
        "risk_reason": risk.reason or "OK",
        "ofi_long": ofi_a,
        "slip_a": slip_a,
        "slip_b": slip_b,
        "regime_ok": regime_ok,
        "regime_name": regime.name,
        "order_type": ot_a.get("type", "?"),
        "order_ok": ot_a.get("safe", False) and ot_b.get("safe", False),
    }

    score = score_signal(diff, net_r.get("net", 0), ofi_a, slip_a, slip_b)
    
    # S-DIT Score
    from radar.sdit_scanner import quick_score_from_analyze
    sdit_score, sdit_rec = quick_score_from_analyze(symbol, ex_a, diff, 0, 0, ofi_a, size)
    
    checklist = _checklist(sig_data)

    if "error" in net_r:
        fee_info = f"⛔ {net_r['error']}"
    else:
        fee_info = (
            f"fee_a={net_r['fee_a']}% | fee_b={net_r['fee_b']}% | "
            f"wd={net_r['wd_pct']}% | net={net_r['net']:.4f}%"
        )

    verdict = "✅ ВХОДИТЬ" if (risk.ok and regime_ok and net_r.get("ok")) else "⏳ ЖДАТЬ"
    
    margin_text = await format_margin_info_for_symbol(symbol)
    dev_text = format_deviation_by_symbol(symbol)
    oi_text = format_oi_confirmation(symbol)
    avg_text = format_avg_multi(symbol)
    
    text = (
        f"📊 *АНАЛИЗ {symbol}*  Score: {score}/10\n"
        f"📡 *S-DIT Score:* {sdit_score}/20 | {sdit_rec}\n"
        f"{'─'*35}\n"
        f"{ex_a.upper()} ←→ {ex_b.upper()} | diff {diff}% | ${size}\n"
        f"{fee_info}\n\n"
        f"*Checklist:*\n{checklist}\n\n"
        f"{margin_text}\n"
        f"{dev_text}\n"
        f"{oi_text}\n\n"
        f"{avg_text}\n\n"
        f"*Вердикт: {verdict}*"
    )

    await msg.edit_text(text, parse_mode="Markdown",
                        reply_markup=signal_actions_kb(symbol, ex_a, ex_b, diff))


async def deep_analyze_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback кнопки [🔍 Deep Analysis]."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 5:
        return

    symbol, ex_a, ex_b = parts[1], parts[2], parts[3]
    try:
        diff = float(parts[4])
    except ValueError:
        diff = 0.0

    await query.edit_message_text(f"🔍 Провожу глубокий анализ {symbol}...")

    from radar.position_guardian import (
        calc_entry_quality, fmt_entry_quality, check_pair_warning,
        cfg, rate_per_h
    )
    from radar.index_deviation_radar import dev_store, calc_velocity

    # Собираем данные для Quality Score
    vel = oi_d = 0.0; confs = 1; hist = []
    try:
        snaps_all = dev_store.get(symbol, ex_a, hours=4.0)
        if snaps_all:
            vel = calc_velocity(snaps_all)
            confs = sum(1 for ex in [ex_a, ex_b, "binance", "gate"]
                         if dev_store.get_latest(symbol, ex) is not None)
            hist = [s.deviation for s in snaps_all[-100:]]
    except Exception as e:
        logger.error(f"Deep analysis data fetch error: {e}")

    eq = calc_entry_quality(symbol, ex_a, ex_b, diff, hist, vel, oi_d, confs)
    warn = check_pair_warning(ex_a, ex_b)

    fees = (cfg(ex_a)["fee"] + cfg(ex_b)["fee"]) * 2
    rl_h = rate_per_h(diff, ex_a)
    rs_h = rate_per_h(diff, ex_b)

    text = (
        f"🔍 *ГЛУБОКИЙ АНАЛИЗ: {symbol}*\n"
        f"  {ex_a.upper()}↑  /  {ex_b.upper()}↓\n"
        f"  Спред: {diff:+.3f}%\n"
        f"  Fees: {fees:.2f}%  |  Net/ч: {rl_h-rs_h:+.5f}%\n\n"
        f"{fmt_entry_quality(eq, symbol, ex_a, ex_b)}"
        + (f"\n\n⚠️ {warn}" if warn else "")
        + f"\n\nЗапустить слежку:\n`/watch START {symbol} {ex_a.upper()} {ex_b.upper()} {diff} 50`"
    )
    
    await query.edit_message_text(text, parse_mode="Markdown")


async def oracle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback кнопки [🧠 Спросить Оракула]."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 4:
        return

    _, symbol, ex_a, ex_b = parts[0], parts[1], parts[2], parts[3]

    await query.edit_message_text(f"🧠 Спрашиваю Oracle про {symbol}...")

    from oracle.groq_client import oracle_analyze
    from oracle.rag_memory import get_context

    signal = {"symbol": symbol, "ex_a": ex_a, "ex_b": ex_b, "type": "funding_arb"}
    ctx_text = await get_context(f"{symbol} фандинг арбитраж")
    result = await oracle_analyze(signal, rag_context=ctx_text)

    score = result.get("score", "?")
    verdict = result.get("verdict", "?")
    risk = result.get("risk", "?")
    timing = result.get("timing", "")
    reasoning = result.get("reasoning", "")
    warning = result.get("warning", "")
    provider = result.get("provider", "")
    cached = "💾 кэш" if result.get("from_cache") else f"🤖 {provider}"

    text = (
        f"🧠 *ORACLE: {symbol}* [{cached}]\n"
        f"{'─'*35}\n"
        f"Score: {score}/10 | Вердикт: *{verdict}* | Риск: {risk}\n"
        f"Timing: {timing}\n\n"
        f"{reasoning}\n"
        f"{('⚠️ ' + warning) if warning else ''}"
    )
    await query.edit_message_text(text, parse_mode="Markdown")
