# radar/dex_oi_hunter.py
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

class DexOIStore:
    def __init__(self, max_history=36):
        self._hist = defaultdict(lambda: deque(maxlen=max_history))
        self._last_alert = {}

    def add(self, symbol: str, exchange: str, oi_usd: float):
        if oi_usd > 0:
            self._hist[(symbol, exchange)].append(OISnap(symbol, exchange, oi_usd, time.time()))

    def get_change(self, symbol: str, exchange: str, window_minutes: int = 15) -> float:
        history = self._hist.get((symbol, exchange))
        if not history or len(history) < 2: return 0.0
        now = time.time()
        latest = history[-1]
        target_ts = now - (window_minutes * 60)
        oldest = None
        for snap in reversed(history):
            if snap.ts <= target_ts:
                oldest = snap
                break
        if not oldest: oldest = history[0]
        if oldest.oi_usd <= 0: return 0.0
        return (latest.oi_usd - oldest.oi_usd) / oldest.oi_usd * 100

    def cooldown_ok(self, symbol: str, min_gap_minutes: int = 20) -> bool:
        last = self._last_alert.get(symbol, 0)
        return (time.time() - last) / 60 > min_gap_minutes

    def mark_alerted(self, symbol: str):
        self._last_alert[symbol] = time.time()

oi_hunter_store = DexOIStore()

async def fetch_fast_oi(ex_id: str, specific_symbol: str = None) -> Dict[str, float]:
    """Быстрый способ получить OI. Если указан specific_symbol, пробует достать его точечно."""
    import ccxt.async_support as ccxt
    res = {}
    try:
        ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
        try:
            # 1. Если нужен конкретный символ, пробуем точечный метод
            if specific_symbol:
                try:
                    # Пробуем разные вариации названия для биржи
                    for s in [f"{specific_symbol}/USDT:USDT", f"{specific_symbol}USDT", f"{specific_symbol}/USDT"]:
                        try:
                            oi_data = await ex.fetch_open_interest(s)
                            val = float(oi_data.get("openInterestValue") or oi_data.get("info", {}).get("openInterestValue") or 0)
                            if val > 0:
                                res[specific_symbol.upper()] = val
                                return res # Нашли - выходим
                        except: continue
                except: pass

            # 2. Массовый скан (для планировщика)
            rates = await ex.fetch_funding_rates()
            for sym, data in rates.items():
                s_clean = sym.split("/")[0].split(":")[0].upper()
                info = data.get("info", {})
                oi = 0.0
                if ex_id == "binance":
                    oi = float(info.get("openInterestValue") or 0)
                elif ex_id == "gate":
                    oi = float(info.get("open_interest_value") or info.get("openInterest") or 0)
                if oi > 0: res[s_clean] = oi
            
            if ex_id == "gate" or not res:
                tkrs = await ex.fetch_tickers()
                for sym, t in tkrs.items():
                    s_clean = sym.split("/")[0].split(":")[0].upper()
                    if s_clean in res: continue
                    info = t.get("info", {})
                    oi = float(info.get("open_interest_value") or info.get("openInterestValue") or 0)
                    if oi > 0: res[s_clean] = oi
        finally:
            await ex.close()
    except Exception as e:
        logger.error(f"[DexHunter] OI error {ex_id}: {e}")
    return res

async def dex_proactive_scan(app):
    from radar.dex_db import dex_db, DEX_ICONS
    from bot.ui import make_alert_keyboard
    from core.config import settings

    logger.info("[DexHunter] Starting Proactive OI scan...")
    apex_set = dex_db._data.get("apex", set())
    raydium_set = dex_db._data.get("raydium", set())
    all_dex_symbols = apex_set.union(raydium_set)
    
    if not all_dex_symbols: return

    binance_oi = await fetch_fast_oi("binance")
    gate_oi = await fetch_fast_oi("gate")
    cex_data = {"BINANCE": binance_oi, "GATE": gate_oi}
    
    signals = []
    for sym in all_dex_symbols:
        for cex_name, oi_map in cex_data.items():
            if sym in oi_map:
                oi_val = oi_map[sym]
                oi_hunter_store.add(sym, cex_name, oi_val)
                ch5 = oi_hunter_store.get_change(sym, cex_name, 5)
                ch15 = oi_hunter_store.get_change(sym, cex_name, 15)
                
                if (ch5 > 1.0 or ch15 > 3.0) and oi_hunter_store.cooldown_ok(sym):
                    where = []
                    if sym in apex_set: where.append(f"{DEX_ICONS['apex']} ApeX")
                    if sym in raydium_set: where.append(f"{DEX_ICONS['raydium']} Raydium")
                    
                    signals.append({
                        "sym": sym, "cex": cex_name, "dex_info": " | ".join(where),
                        "ch5": ch5, "ch15": ch15, "oi": oi_val
                    })

    for sig in signals:
        text = (
            f"⚡️ *DEX-CEX PROACTIVE SIGNAL*\n"
            f"🔥 Монета: #{sig['sym']}\n"
            f"🏛 Доступна на: {sig['dex_info']}\n"
            f"─────────────────────────────\n"
            f"📈 Рост OI на *{sig['cex']}*:\n"
            f"   • 5 мин:  `{sig['ch5']:+.2f}%`\n"
            f"   • 15 мин: `{sig['ch15']:+.2f}%`\n"
            f"💰 Текущий OI: `${sig['oi']/1e6:.1f}M`\n"
            f"─────────────────────────────\n"
            f"💡 *СТРАТЕГИЯ*: LONG {sig['cex']} / SHORT DEX"
        )
        oi_hunter_store.mark_alerted(sig['sym'])
        kb = make_alert_keyboard(sig['sym'])
        try:
            await app.bot.send_message(chat_id=settings.telegram_chat_id, text=text, reply_markup=kb, parse_mode="Markdown")
        except: pass
    logger.info(f"[DexHunter] Done. B:{len(binance_oi)} G:{len(gate_oi)}")
