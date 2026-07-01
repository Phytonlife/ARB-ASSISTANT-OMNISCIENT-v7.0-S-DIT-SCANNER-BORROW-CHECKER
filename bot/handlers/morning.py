# bot/handlers/morning.py
from telegram import Update
from telegram.ext import ContextTypes


async def build_morning_report() -> str:
    """Собирает утреннюю сводку."""
    from oracle.regime import get_regime_info
    from core.database import get_stats
    from data.listings import get_upcoming_listings
    from core.config import settings

    info = await get_regime_info()
    emoji = {"PANIC": "🔴", "EUPHORIA": "🟢", "TREND": "🟡", "SIDEWAYS": "🔵"}
    e = emoji.get(info["name"], "⚪")

    stats = await get_stats(days=7)
    listings = await get_upcoming_listings(days=3)

    btc_price = info.get('btc_price', 0)
    change_4h = info.get('change_4h', 0)
    try:
        btc_price = float(btc_price)
        change_4h = float(change_4h)
    except (TypeError, ValueError):
        btc_price = 0
        change_4h = 0

    lines = [
        f"☀️ *УТРЕННЯЯ СВОДКА*",
        f"{'─'*35}",
        f"{e} Режим: *{info['name']}* — {info.get('description', '')}",
        f"BTC: {btc_price:.0f}$ {change_4h:+.1f}% 4h",
        "",
        f"📊 *Статистика за неделю:*",
        f"Сделок: {stats['total']} | WR: {stats['wr']}% | PnL: {stats['pnl']:+.2f}$",
        "",
        f"✅ *Разрешённые стратегии:*",
    ]

    for s in info.get("allowed", []):
        lines.append(f"  • {s}")

    # Strategy Rank
    rank_text = _strategy_rank(info["name"])
    lines += ["", "🏆 *Strategy Rank сегодня:*", rank_text]

    # Листинги
    if listings:
        lines += ["", "📅 *Листинги (3 дня):*"]
        for lst in listings[:3]:
            coins = ", ".join(lst.get("coins", [])[:3])
            lines.append(f"  • {lst.get('date', '')[:10]}: {lst.get('title', '')} [{coins}]")

    lines += ["", f"Удачного дня! Символов в отслеживании: {len(settings.symbols)}"]
    return "\n".join(lines)


def _strategy_rank(regime: str) -> str:
    """Ранг стратегий для текущего режима."""
    base = [
        ("Funding Arb", 8.5, "funding_arb"),
        ("Listing Arb", 6.0, "listing_arb"),
        ("Index Arb", 5.0, "index_arb"),
        ("Binance Ramp", 4.0, "ramp_arb"),
        ("Spread Arb", 3.5, "spread_arb"),
        ("Gate Exploit", 3.0, "gate_exploit"),
    ]

    from oracle.regime import REGIME_RULES
    allowed = REGIME_RULES.get(regime, REGIME_RULES["SIDEWAYS"]).allowed

    lines = []
    for name, score, key in base:
        ok = "✅" if key in allowed else "❌"
        lines.append(f"  {ok} {name}: {score}/10")

    return "\n".join(lines[:5])


async def cmd_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("☀️ Готовлю сводку...")
    text = await build_morning_report()
    await msg.edit_text(text, parse_mode="Markdown")
