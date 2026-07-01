# bot/handlers/rank.py
from telegram import Update
from telegram.ext import ContextTypes


async def cmd_rank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Strategy Rank: топ стратегий с баллами."""
    from oracle.regime import get_regime_info, REGIME_RULES
    from core.database import get_stats

    info = await get_regime_info()
    allowed = REGIME_RULES.get(info["name"], REGIME_RULES["SIDEWAYS"]).allowed

    strategies = [
        ("Funding Arb (perp-perp)", 8.5, "funding_arb",
         "diff>0.30%, net=diff-0.075%"),
        ("Listing Arb Phase 1", 6.0, "listing_arb",
         "первые 6ч, gross>2.15%"),
        ("Index Arb", 5.0, "index_arb",
         "diff>0.175%, разные индексы"),
        ("Binance Rate Ramp", 4.0, "ramp_arb",
         "rate>1.5%, цикл ускоряется"),
        ("Spread Arb $100", 3.8, "spread_arb",
         "gross>1.155%, spot+perp"),
        ("Spread Arb $50", 3.5, "spread_arb",
         "gross>2.155%, spot+perp"),
        ("Gate Exploit", 3.0, "gate_exploit",
         "LONG Gate за 2-3ч до выплаты"),
        ("Delisting Arb", 2.0, "delisting_arb",
         "высокий риск ликвидности"),
    ]

    lines = [
        f"🏆 *STRATEGY RANK — {info['name']}*",
        "─" * 35,
    ]

    for i, (name, score, key, desc) in enumerate(strategies, 1):
        ok = "✅" if key in allowed else "❌"
        lines.append(f"{i}. {ok} *{name}* {score}/10\n   _{desc}_")

    lines += [
        "",
        f"Режим: {info['name']} | {info.get('description', '')}",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
