# radar/margin_monitor.py
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict
from loguru import logger

# ════════════════════════════════════════════════════════════════
# КОНФИГ
# ════════════════════════════════════════════════════════════════

BATCH_SIZE = 50
BATCH_SLEEP = 1.0
MARGIN_EXCHANGES = ["bybit", "binance", "okx", "gate"]

@dataclass
class MarginAsset:
    symbol: str
    exchange: str
    timestamp: float
    borrowable: bool
    borrow_usage_rate: float = 0.0
    hourly_borrow_rate: float = 0.0
    daily_borrow_rate: float = 0.0
    available_amount: float = 0.0
    max_leverage: int = 3

class MarginStore:
    def __init__(self):
        self._data: Dict[tuple, MarginAsset] = {}
    def update(self, a: MarginAsset): self._data[(a.symbol, a.exchange)] = a
    def get(self, sym: str, ex: str) -> Optional[MarginAsset]: return self._data.get((sym.upper(), ex.lower()))
    def get_all_latest(self) -> List[MarginAsset]: return list(self._data.values())

margin_store = MarginStore()

# ════════════════════════════════════════════════════════════════
# СКАНЕРЫ (ЗАГЛУШКИ / УПРОЩЕННЫЕ ВЕРСИИ)
# ════════════════════════════════════════════════════════════════

async def fetch_bybit_margin(ex):
    # Упрощенно для примера, в реальности тут сложнее
    return []

async def margin_scan():
    # В реальности тут опрос бирж через ccxt
    logger.info("[Margin] Scan triggered...")
    # Для теста добавим одну монету
    margin_store.update(MarginAsset("BTC", "bybit", time.time(), True, 0.1, 0.0001, 0.0024, 100, 10))
    return []

async def ensure_margin_data():
    if not margin_store.get_all_latest():
        await margin_scan()

# ════════════════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ (ВОССТАНОВЛЕНИЕ СТИЛЯ)
# ════════════════════════════════════════════════════════════════

def format_margin_list(exchange: str, top_n: int = 20) -> str:
    assets = [a for a in margin_store.get_all_latest() if a.exchange == exchange.lower()]
    assets.sort(key=lambda x: x.borrow_usage_rate, reverse=True)
    
    lines = [f"📊 MARGIN {exchange.upper()} (TOP {top_n})", "─" * 30]
    for a in assets[:top_n]:
        status = "✅" if a.borrowable else "❌"
        lines.append(f"{status} {a.symbol:8} Usage:{a.borrow_usage_rate*100:4.1f}% Rate:{a.daily_borrow_rate*100:5.3f}%")
    return "\n".join(lines) if assets else f"Нет данных по {exchange}"

async def format_margin_info_for_symbol(symbol: str) -> str:
    from radar.borrow_checker import check_all_borrow
    return await check_all_borrow(symbol)

def format_margin_event(asset: MarginAsset) -> str:
    """Форматирует уведомление об изменении доступности маржи."""
    status = "✅ ДОСТУПНО" if asset.borrowable else "❌ НЕДОСТУПНО"
    return (
        f"🔔 *MARGIN ALERT: {asset.symbol}*\n"
        f"{'─'*30}\n"
        f"Биржа: {asset.exchange.upper()}\n"
        f"Статус: {status}\n"
        f"Usage: `{asset.borrow_usage_rate*100:.1f}%` | Rate: `{asset.daily_borrow_rate*100:.3f}%` день\n"
        f"Leverage: {asset.max_leverage}x"
    )
