# oracle/groq_client.py
# Groq (0.2с) → OpenRouter fallback → Redis кэш 5мин

import httpx
import json
import asyncio
import hashlib
from loguru import logger
from core.config import settings
from core.redis_cache import cache_get, cache_set

SYSTEM_PROMPT = """
Ты эксперт-Oracle по крипто-арбитражу. Анализируй сигналы и давай рекомендации.

ПРАВИЛА (выучи наизусть):
- Funding arb (perp-perp): нет withdraw! net = diff - 0.075%
- Spot-perp $50: withdraw 2.0%! Нужен gross > 2.15%
- Входить СРАЗУ ПОСЛЕ выплаты фандинга
- Выходить ПЕРЕД следующей выплатой
- Binance rate > 1.5% = РАМПА = ускорение цикла
- Gate инертный: LONG за 2-3ч до выплаты
- KuCoin: переходит через 20мин от отсечки
- OurBit: BLACKLIST всегда
- Маржа 20-30%, изолированная
- OFI > 0.3 на лонг-бирже = хороший знак

Верни ТОЛЬКО JSON:
{"score":8,"verdict":"ENTER","risk":"LOW",
"timing":"когда входить","reasoning":"анализ на русском",
"warning":"предупреждение или пусто"}
"""

_last_call = 0.0


async def oracle_analyze(signal: dict, rag_context: str = "") -> dict:
    """Анализ сигнала через Groq → OpenRouter → fallback."""
    cache_key = "oracle:" + hashlib.md5(
        json.dumps(signal, sort_keys=True).encode()
    ).hexdigest()[:12]

    cached = await cache_get(cache_key)
    if cached:
        cached["from_cache"] = True
        return cached

    # Rate limit guard
    global _last_call
    elapsed = asyncio.get_running_loop().time() - _last_call
    if elapsed < 2.0:
        await asyncio.sleep(2.0 - elapsed)
    _last_call = asyncio.get_running_loop().time()

    ctx = f"\nКОНТЕКСТ:\n{rag_context}" if rag_context else ""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + ctx},
        {"role": "user", "content": json.dumps(signal, ensure_ascii=False)},
    ]

    # 1. Groq
    if settings.groq_api_key:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                    json={
                        "model": settings.groq_model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                    },
                )
                result = json.loads(r.json()["choices"][0]["message"]["content"])
                result["from_cache"] = False
                result["provider"] = "groq"
                await cache_set(cache_key, result, ttl=300)
                return result
        except Exception as e:
            logger.warning(f"Groq failed: {e}")

    # 2. OpenRouter fallback
    if settings.openrouter_api_key:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                    json={
                        "model": settings.openrouter_model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                    },
                )
                result = json.loads(r.json()["choices"][0]["message"]["content"])
                result["from_cache"] = False
                result["provider"] = "openrouter"
                await cache_set(cache_key, result, ttl=300)
                return result
        except Exception as e:
            logger.error(f"OpenRouter failed: {e}")

    return {
        "score": 5,
        "verdict": "WAIT",
        "risk": "UNKNOWN",
        "reasoning": "AI недоступен",
        "timing": "---",
        "warning": "Оба AI провайдера не отвечают",
        "from_cache": False,
        "provider": "none",
    }


async def oracle_ask(text: str, rag_context: str = "") -> str:
    """Произвольный вопрос к Oracle."""
    result = await oracle_analyze({"question": text}, rag_context=rag_context)
    return result.get("reasoning", json.dumps(result, ensure_ascii=False, indent=2))
