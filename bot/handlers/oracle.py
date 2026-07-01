# bot/handlers/oracle.py
from telegram import Update
from telegram.ext import ContextTypes


async def cmd_oracle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /oracle [ТЕКСТ] — произвольный вопрос к Oracle с RAG контекстом
    """
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text(
            "Использование: /oracle ТЕКСТ\n"
            "Пример: /oracle фандинг binance ARB рампа"
        )
        return

    msg = await update.message.reply_text("🧠 Думаю...")

    from oracle.groq_client import oracle_analyze
    from oracle.rag_memory import get_context

    rag_ctx = await get_context(text)
    result = await oracle_analyze({"question": text}, rag_context=rag_ctx)

    reasoning = result.get("reasoning", str(result))
    provider = result.get("provider", "")
    cached = "💾 кэш" if result.get("from_cache") else f"🤖 {provider}"

    reply = f"🧠 *ORACLE* [{cached}]\n{'─'*30}\n{reasoning}"
    await msg.edit_text(reply, parse_mode="Markdown")
