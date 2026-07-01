# hunter/execution.py
# Blacklist проверяется ПЕРВЫМ. Обе ноги — ПАРАЛЛЕЛЬНО.

import asyncio
from loguru import logger
from data.fees import BLACKLIST


def get_order_type(exchange: str, position_usd: float, depth_usd: float) -> dict:
    """Определяет тип ордера. Blacklist — до проверки depth!"""
    if exchange.lower() in BLACKLIST:
        return {"type": "BLOCKED", "safe": False,
                "reason": f"{exchange}: BLACKLIST"}

    ratio = depth_usd / position_usd if position_usd > 0 else 0

    if ratio >= 3.0:
        return {"type": "MARKET_IOC", "safe": True, "ratio": ratio}
    elif ratio >= 1.5:
        return {"type": "LIMIT_FOK_5S", "safe": True, "ratio": ratio}

    return {"type": "SKIP", "safe": False,
            "reason": f"Тонкий стакан {ratio:.1f}x < 1.5x"}


async def open_hedge(signal: dict) -> dict:
    """Открывает обе ноги ПАРАЛЛЕЛЬНО. 2x быстрее последовательного."""
    ea = signal["ex_long"]
    eb = signal["ex_short"]
    size = signal["size_usd"]
    symbol = signal["symbol"]
    depth_a = signal.get("depth_a", 9999)
    depth_b = signal.get("depth_b", 9999)

    ot_a = get_order_type(ea, size, depth_a)
    ot_b = get_order_type(eb, size, depth_b)

    if not ot_a["safe"]:
        return {"success": False, "reason": f"Long: {ot_a['reason']}"}
    if not ot_b["safe"]:
        return {"success": False, "reason": f"Short: {ot_b['reason']}"}

    logger.info(f"Hedge {symbol}: {ea}[{ot_a['type']}] + {eb}[{ot_b['type']}]")

    try:
        long_r, short_r = await asyncio.gather(
            _exec(ea, symbol, "buy", size, ot_a["type"]),
            _exec(eb, symbol, "sell", size, ot_b["type"]),
            return_exceptions=True,
        )

        if isinstance(long_r, Exception) or isinstance(short_r, Exception):
            await _emergency_close(ea, eb, symbol, size)
            return {"success": False, "reason": "Partial fill — emergency close"}

        return {"success": True, "long": long_r, "short": short_r}

    except Exception as e:
        logger.error(f"Hedge failed: {e}")
        return {"success": False, "reason": str(e)}


async def _exec(exchange: str, symbol: str, side: str,
                size_usd: float, order_type: str) -> dict:
    """
    Заглушка исполнения — реализуй через ccxt на Неделе 2.
    В реальном боте здесь будет ccxt.create_order().
    """
    logger.info(f"  {side} {symbol} ${size_usd} @ {exchange} [{order_type}]")
    await asyncio.sleep(0.05)   # имитация сетевой задержки
    return {"exchange": exchange, "side": side, "filled": True}


async def _emergency_close(ea: str, eb: str, sym: str, size: float):
    """Аварийное закрытие обеих ног при частичном исполнении."""
    logger.warning(f"EMERGENCY CLOSE {sym}")
    await asyncio.gather(
        _exec(ea, sym, "sell", size, "MARKET_IOC"),
        _exec(eb, sym, "buy", size, "MARKET_IOC"),
        return_exceptions=True,
    )
