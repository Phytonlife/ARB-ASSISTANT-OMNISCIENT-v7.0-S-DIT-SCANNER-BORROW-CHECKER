# oracle/postmortem.py
# Post-Mortem при убытке + /approve_rule модерация

from loguru import logger
from core.database import get_history, add_pending_rule
from oracle.groq_client import oracle_analyze
from oracle.rag_memory import get_context


async def run_postmortem(trade_id: int) -> str:
    """Запускает Post-Mortem анализ убыточной сделки."""
    trades = await get_history(n=100)
    trade = next((t for t in trades if t.id == trade_id), None)

    if not trade:
        return f"Сделка #{trade_id} не найдена"

    if (trade.pnl_usd or 0) >= 0:
        return f"Сделка #{trade_id} прибыльная, Post-Mortem не нужен"

    signal = {
        "type": "postmortem",
        "trade_id": trade_id,
        "symbol": trade.symbol,
        "strategy": trade.strategy,
        "ex_a": trade.ex_a,
        "ex_b": trade.ex_b,
        "spread_entry": trade.spread_entry,
        "spread_exit": trade.spread_exit,
        "pnl_usd": trade.pnl_usd,
        "size_usd": trade.size_usd,
        "question": f"Почему сделка {trade.symbol} {trade.strategy} принесла убыток {trade.pnl_usd}$? "
                    f"Какое правило надо добавить чтобы избежать подобного?",
    }

    rag_ctx = await get_context(f"{trade.symbol} {trade.strategy} убыток")
    result = await oracle_analyze(signal, rag_context=rag_ctx)

    reasoning = result.get("reasoning", "Анализ недоступен")
    warning = result.get("warning", "")
    rule_text = f"[{trade.symbol}/{trade.strategy}] {reasoning}"
    if warning:
        rule_text += f" | WARNING: {warning}"

    rule_id = await add_pending_rule(trade_id, rule_text)
    logger.info(f"Post-Mortem #{trade_id} → правило #{rule_id} ожидает подтверждения")

    return (
        f"🔴 POST-MORTEM #{trade_id}\n"
        f"{'─'*35}\n"
        f"📊 {trade.symbol} | {trade.strategy}\n"
        f"💸 PnL: {trade.pnl_usd:+.2f}$\n\n"
        f"🧠 Анализ:\n{reasoning}\n"
        f"{('⚠️ ' + warning) if warning else ''}\n\n"
        f"📋 Правило #{rule_id} добавлено в очередь\n"
        f"Используй /approve_rule {rule_id} для подтверждения"
    )
