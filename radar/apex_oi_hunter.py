# radar/apex_oi_hunter.py
import asyncio
import time
from dataclasses import dataclass
from collections import defaultdict, deque
from loguru import logger
from typing import Dict, List, Optional

@dataclass
class OISnap:
    symbol: str
    exchange: str
    oi_usd: float
    ts: float

class ApexOIStore:
    def __init__(self, max_history=24): # 2 часа при скане каждые 5 мин
        self._hist = defaultdict(lambda: deque(maxlen=max_history))
        self._last_alert = {}

    def add(self, symbol: str, exchange: str, oi_usd: float):
        self._hist[(symbol, exchange)].append(OISnap(symbol, exchange, oi_usd, time.time()))

    def get_change(self, symbol: str, exchange: str, window_minutes: int = 15) -> float:
        history = self._hist.get((symbol, exchange))
        if not history or len(history) < 2: return 0.0
        
        now = time.time()
        latest = history[-1]
        # Ищем снимок наиболее близкий к window_minutes назад
        target_ts = now - (window_minutes * 60)
        
        oldest = None
        for snap in reversed(history):
            if snap.ts <= target_ts:
                oldest = snap
                break
        if not oldest: oldest = history[0]
        
        if oldest.oi_usd == 0: return 0.0
        return (latest.oi_usd - oldest.oi_usd) / oldest.oi_usd * 100

    def cooldown_ok(self, symbol: str, min_gap_minutes: int = 30) -> bool:
        last = self._last_alert.get(symbol, 0)
        return (time.time() - last) / 60 > min_gap_minutes

    def mark_alerted(self, symbol: str):
        self._last_alert[symbol] = time.time()

oi_hunter_store = ApexOIStore()

# ════════════════════════════════════════════════════════════════
# ЛОГИКА СКАНИРОВАНИЯ
# ════════════════════════════════════════════════════════════════

async def apex_proactive_scan(app):
    from radar.dex_db import dex_db
    from core.database import is_on_dex
    from radar.oi_monitor import fetch_oi_for_exchange
    from bot.ui import make_alert_keyboard
    from core.config import settings

    logger.info("[Hunter] Starting Proactive ApeX OI scan...")
    
    # 1. Берем все монеты, которые есть на ApeX
    apex_symbols = list(dex_db._data.get("apex", set()))
    if not apex_symbols: return

    # 2. Получаем текущий OI с Binance и Gate (они самые ликвидные индикаторы)
    # Мы используем кэшированные или свежие данные из oi_monitor
    binance_oi = await fetch_oi_for_exchange("binance") # Внутри oi_monitor уже есть логика fetch
    gate_oi = await fetch_oi_for_exchange("gate")

    signals = []

    for sym in apex_symbols:
        # Проверяем Binance
        if sym in binance_oi:
            oi_val = binance_oi[sym]
            oi_hunter_store.add(sym, "binance", oi_val)
            
            ch5 = oi_hunter_store.get_change(sym, "binance", 5)
            ch15 = oi_hunter_store.get_change(sym, "binance", 15)
            
            if (ch5 > 3.0 or ch15 > 7.0) and oi_hunter_store.cooldown_ok(sym):
                signals.append({
                    "sym": sym, "ex": "BINANCE", "ch5": ch5, "ch15": ch15, "oi": oi_val
                })

        # Проверяем Gate (если на бинансе нет или для доп. подтверждения)
        if sym in gate_oi:
            oi_val = gate_oi[sym]
            oi_hunter_store.add(sym, "gate", oi_val)
            # Аналогичная логика для Gate...

    # 3. Рассылка алертов
    for sig in signals:
        sym = sig['sym']
        text = (
            f"🚀 *PRE-PUMP ALERT (ApeX Asset)*\n"
            f"🔥 Монета: #{sym}\n"
            f"🏛 Биржа (CEX): {sig['ex']}\n"
            f"─────────────────────────────\n"
            f"📈 Рост OI (5м):  `{sig['ch5']:+.2f}%`\n"
            f"📈 Рост OI (15м): `{sig['ch15']:+.2f}%`\n"
            f"💰 Текущий OI:   `${sig['oi']/1e6:.1f}M`\n"
            f"─────────────────────────────\n"
            f"⚠️ *Ожидается разгон индекса на ApeX!*\n"
            f"Будьте готовы открывать SHORT на ApeX при расширении спреда."
        )
        
        oi_hunter_store.mark_alerted(sym)
        kb = make_alert_keyboard(sym)
        
        await app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            reply_markup=kb,
            parse_mode="Markdown"
        )
        logger.info(f"[Hunter] Proactive Alert sent for {sym}")

    logger.info(f"[Hunter] Scan done. Checked {len(apex_symbols)} assets.")
