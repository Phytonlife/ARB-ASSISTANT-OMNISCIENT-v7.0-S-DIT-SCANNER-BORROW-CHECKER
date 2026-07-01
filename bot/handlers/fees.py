# bot/handlers/fees.py
from telegram import Update
from telegram.ext import ContextTypes


async def cmd_fees(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /fees EX_A EX_B DIFF SIZE [perp|spot]
    Пример: /fees okx binance 0.52 50 perp
    """
    args = ctx.args
    if len(args) < 4:
        await update.message.reply_text(
            "Использование: /fees EX_A EX_B DIFF SIZE [perp|spot]\n"
            "Пример: /fees okx binance 0.52 50"
        )
        return

    ex_a, ex_b = args[0].lower(), args[1].lower()
    try:
        diff = float(args[2])
        size = float(args[3])
    except ValueError:
        await update.message.reply_text("DIFF и SIZE должны быть числами")
        return

    mode = args[4].lower() if len(args) > 4 else "perp"
    has_wd = (mode == "spot")
    a_type = "spot" if has_wd else "perp"
    b_type = "perp"

    from hunter.math_engine import calc_net_spread
    r = calc_net_spread(diff, ex_a, ex_b, size, a_type, b_type, has_wd)

    if "error" in r:
        await update.message.reply_text(f"❌ {r['error']}")
        return

    verdict = "✅ ВХОДИТЬ" if r["ok"] else "❌ НЕВЫГОДНО"
    great = " 🔥 ОТЛИЧНО" if r["good"] else ""

    text = (
        f"💸 *РАСЧЁТ КОМИССИЙ*\n"
        f"{'─'*30}\n"
        f"{ex_a.upper()} ({a_type}) ←→ {ex_b.upper()} ({b_type})\n"
        f"Позиция: ${size} | Gross: {diff}%\n\n"
        f"fee_a:    {r['fee_a']}%\n"
        f"fee_b:    {r['fee_b']}%\n"
        f"withdraw: {r['wd_pct']}%"
        f"{'  ← wd_usd/size*100!' if has_wd else ''}\n"
        f"{'─'*20}\n"
        f"Итого:    {r['total']}%\n"
        f"NET:      *{r['net']:.4f}%*{great}\n\n"
        f"Вердикт: {verdict}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
