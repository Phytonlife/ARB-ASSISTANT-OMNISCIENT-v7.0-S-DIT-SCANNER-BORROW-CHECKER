# oracle/regime.py
# Market Regime: PANIC / EUPHORIA / SIDEWAYS / TREND
# Пересчитывается каждые 5 мин через BTC OHLCV

import asyncio
from dataclasses import dataclass, field
from loguru import logger
from data.exchanges import get_btc_ohlcv
from core.redis_cache import cache_get, cache_set


@dataclass
class Regime:
    name: str                          # PANIC | EUPHORIA | SIDEWAYS | TREND
    allowed: list[str] = field(default_factory=list)
    description: str = ""


REGIME_RULES = {
    "PANIC": Regime(
        name="PANIC",
        allowed=[],  # ничего не торгуем
        description="BTC упал > 5% за 4ч. Все входы заблокированы.",
    ),
    "EUPHORIA": Regime(
        name="EUPHORIA",
        allowed=["funding_arb", "index_arb"],
        description="BTC вырос > 5% за 4ч. Только перп-перп арб.",
    ),
    "TREND": Regime(
        name="TREND",
        allowed=["funding_arb", "index_arb", "spread_arb", "listing_arb", "ramp_arb"],
        description="Устойчивый тренд. Большинство стратегий разрешено.",
    ),
    "SIDEWAYS": Regime(
        name="SIDEWAYS",
        allowed=["funding_arb", "index_arb", "spread_arb", "listing_arb",
                 "ramp_arb", "gate_exploit", "kucoin_exploit", "delisting_arb"],
        description="Боковик. Все стратегии разрешены.",
    ),
}


async def detect_regime() -> Regime:
    """Определяет текущий Market Regime по BTC OHLCV. Кэш 5 мин."""
    cached = await cache_get("market_regime")
    if cached:
        return REGIME_RULES.get(cached["name"], REGIME_RULES["SIDEWAYS"])

    try:
        ohlcv = await get_btc_ohlcv(timeframe="1h", limit=24)
        if not ohlcv or len(ohlcv) < 4:
            return REGIME_RULES["SIDEWAYS"]

        # Изменение за последние 4 часа
        open_4h = ohlcv[-4][1]   # open
        close_now = ohlcv[-1][4]  # close
        change_4h = (close_now - open_4h) / open_4h * 100

        # Волатильность за 24ч
        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        volatility = (max(highs) - min(lows)) / min(lows) * 100

        if change_4h < -5.0:
            name = "PANIC"
        elif change_4h > 5.0:
            name = "EUPHORIA"
        elif volatility > 8.0:
            name = "TREND"
        else:
            name = "SIDEWAYS"

        regime_data = {
            "name": name,
            "change_4h": round(change_4h, 2),
            "volatility": round(volatility, 2),
            "btc_price": round(close_now, 0),
        }
        await cache_set("market_regime", regime_data, ttl=300)
        logger.info(f"Regime: {name} | BTC {change_4h:+.2f}% 4h | vol {volatility:.1f}%")
        return REGIME_RULES[name]

    except Exception as e:
        logger.warning(f"Regime detection failed: {e}")
        return REGIME_RULES["SIDEWAYS"]


async def get_regime_info() -> dict:
    """Для /regime команды."""
    cached = await cache_get("market_regime")
    if cached:
        regime = REGIME_RULES.get(cached["name"], REGIME_RULES["SIDEWAYS"])
        return {**cached, "allowed": regime.allowed, "description": regime.description}
    regime = await detect_regime()
    return {"name": regime.name, "allowed": regime.allowed, "description": regime.description}

async def update_regime():
    """Фоновое обновление режима рынка."""
    await detect_regime()
