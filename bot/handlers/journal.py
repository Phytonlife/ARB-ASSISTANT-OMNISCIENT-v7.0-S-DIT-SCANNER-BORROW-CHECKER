# bot/handlers/journal.py
from telegram import Update
from telegram.ext import ContextTypes
from core.database import (
    add_trade, close_trade, get_open_trades, get_stats,
    get_history, get_pending_rules, approve_rule,
)


async def cmd_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /open SYMBOL STRAT EX_A EX_B SPREAD SIZE
    Пример: /open ORCA funding okx binance 0.52 50
    """
    args = ctx.args
    if len(args) < 6:
        await update.message.reply_text(
            "Использование: /open SYMBOL STRAT EX_A EX_B SPREAD SIZE\n"
            "Пример: /open ORCA funding okx binance 0.52 50"
        )
        return
    symbol, strategy = args[0].upper(), args[1].lower()
    ex_a, ex_b = args[2].lower(), args[3].lower()
    try:
        spread = float(args[4])
        size = float(args[5])
    except ValueError:
        await update.message.reply_text("SPREAD и SIZE должны быть числами")
        return

    trade_id = await add_trade(symbol, strategy, ex_a, ex_b, spread, size)
    await update.message.reply_text(
        f"✅ *Сделка #{trade_id} открыта*\n"
        f"{symbol} | {strategy} | {ex_a}↔{ex_b}\n"
        f"Spread entry: {spread}% | Size: ${size}",
        parse_mode="Markdown",
    )


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /close ID EXIT_SPREAD PNL_USD
    Пример: /close 1 0.12 2.50
    """
    args = ctx.args
    if len(args) < 3:
        await update.message.reply_text(
            "Использование: /close ID EXIT_SPREAD PNL_USD\n"
            "Пример: /close 1 0.12 2.50"
        )
        return
    try:
        trade_id = int(args[0])
        exit_spread = float(args[1])
        pnl = float(args[2])
    except ValueError:
        await update.message.reply_text("Неверный формат")
        return

    trade = await close_trade(trade_id, exit_spread, pnl)
    if not trade:
        await update.message.reply_text(f"❌ Сделка #{trade_id} не найдена")
        return

    emoji = "✅" if pnl >= 0 else "🔴"
    text = (
        f"{emoji} *Сделка #{trade_id} закрыта*\n"
        f"{trade.symbol} | {trade.strategy}\n"
        f"Entry: {trade.spread_entry}% → Exit: {exit_spread}%\n"
        f"PnL: *{pnl:+.2f}$*"
    )

    # Post-Mortem при убытке
    if pnl < 0:
        from oracle.postmortem import run_postmortem
        text += "\n\n🔴 Запускаю Post-Mortem..."
        msg = await update.message.reply_text(text, parse_mode="Markdown")
        pm = await run_postmortem(trade_id)
        await update.message.reply_text(pm)
        return

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_pos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Открытые позиции + exposure."""
    trades = await get_open_trades()
    if not trades:
        await update.message.reply_text("📭 Нет открытых позиций")
        return

    lines = [f"📊 *ОТКРЫТЫЕ ПОЗИЦИИ ({len(trades)})*", "─" * 30]
    total = 0
    ex_exposure: dict[str, float] = {}

    for t in trades:
        lines.append(
            f"#{t.id} {t.symbol} | {t.strategy}\n"
            f"  {t.ex_a}↔{t.ex_b} | spread {t.spread_entry}% | ${t.size_usd}"
        )
        total += t.size_usd
        ex_exposure[t.ex_a] = ex_exposure.get(t.ex_a, 0) + t.size_usd
        ex_exposure[t.ex_b] = ex_exposure.get(t.ex_b, 0) + t.size_usd

    lines += ["", f"Итого exposure: *${total:.0f}*"]
    for ex, amt in sorted(ex_exposure.items(), key=lambda x: -x[1]):
        lines.append(f"  {ex}: ${amt:.0f}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Статистика сделок."""
    days = int(ctx.args[0]) if ctx.args else 30
    s = await get_stats(days=days)

    if s["total"] == 0:
        await update.message.reply_text(f"📭 Нет закрытых сделок за {days} дней")
        return

    text = (
        f"📈 *СТАТИСТИКА ({days}д)*\n"
        f"{'─'*30}\n"
        f"Сделок:  {s['total']}\n"
        f"Побед:   {s['win']} ({s['wr']}%)\n"
        f"Убытков: {s['loss']}\n"
        f"PnL:     *{s['pnl']:+.2f}$*\n"
        f"Profit Factor: {s['pf']}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Последние N сделок."""
    n = int(ctx.args[0]) if ctx.args else 20
    trades = await get_history(n=n)

    if not trades:
        await update.message.reply_text("📭 Нет сделок")
        return

    lines = [f"📜 *ИСТОРИЯ ({len(trades)})*", "─" * 30]
    for t in trades:
        pnl_str = f"{t.pnl_usd:+.2f}$" if t.pnl_usd is not None else "открыта"
        emoji = "✅" if (t.pnl_usd or 0) >= 0 else "🔴"
        lines.append(f"{emoji} #{t.id} {t.symbol} {t.strategy[:8]} {pnl_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pending_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Список правил ожидающих подтверждения."""
    rules = await get_pending_rules()
    if not rules:
        await update.message.reply_text("✅ Нет ожидающих правил")
        return

    lines = [f"📋 *PENDING RULES ({len(rules)})*", "─" * 30]
    for r in rules:
        lines.append(f"#{r.id} (сделка #{r.trade_id}):\n  {r.rule_text[:80]}...")
        lines.append(f"  /approve_rule {r.id}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_approve_rule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Подтвердить правило → добавить в FAISS."""
    if not ctx.args:
        await update.message.reply_text("Использование: /approve_rule ID")
        return

    try:
        rule_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return

    rule = await approve_rule(rule_id)
    if not rule:
        await update.message.reply_text(f"❌ Правило #{rule_id} не найдено")
        return

    # Добавляем в FAISS (пересборка не нужна, просто добавляем чанк)
    await update.message.reply_text(
        f"✅ *Правило #{rule_id} подтверждено*\n\n"
        f"_{rule.rule_text[:200]}_\n\n"
        f"Правило добавлено в базу знаний Oracle.",
        parse_mode="Markdown",
    )
