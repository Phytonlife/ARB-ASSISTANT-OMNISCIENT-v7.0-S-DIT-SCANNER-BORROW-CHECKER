# radar/apex_checker.py
# ═══════════════════════════════════════════════════════════════════════
# ApeX Checker v3.1 — UPDATED FOR SAHARA & OMNI API
# ═══════════════════════════════════════════════════════════════════════

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
import logging

try:
    from loguru import logger
except ImportError:
    logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# КОНФИГ
# ════════════════════════════════════════════════════════════════

APEX_DEFAULT_RATE_H = 0.000125
APEX_CACHE_TTL_MIN  = 30
APEX_TIMEOUT_S      = 15

APEX_SYMBOL_MAP = {"ZERO": "ZRO", "BNANA": "BANANA"}

STATIC_APEX_SYMBOLS = {
    "BTC","ETH","SOL","BNB","ARB","OP","DOGE","LINK","UNI","MATIC",
    "APT","SUI","TIA","SEI","STRK","WIF","BONK","JUP","PYTH",
    "STBL","ZRO","AAVE","AVAX","SPX","SAHARA"
}

# ════════════════════════════════════════════════════════════════
# МОДЕЛИ
# ════════════════════════════════════════════════════════════════

@dataclass
class ApexTicker:
    symbol:           str
    mark_price:       float
    index_price:      float
    funding_rate_h:   float
    predicted_rate_h: float
    open_interest:    float
    next_funding_s:   float
    volume_24h_usdt:  float
    deviation:        float
    source:           str
    ts:               float

@dataclass
class ApexInfo:
    symbol:         str
    available:      bool
    funding_rate_h: float
    predicted_h:    float
    open_interest:  float
    deviation:      float
    next_funding_s: float
    source:         str

    @property
    def is_stable(self) -> bool: return abs(self.funding_rate_h) <= 0.002
    @property
    def apex_pays_shorts(self) -> bool: return self.funding_rate_h < -0.002
    @property
    def oi_m(self) -> str:
        oi = self.open_interest
        if oi >= 1e9: return f"${oi/1e9:.2f}B"
        if oi >= 1e6: return f"${oi/1e6:.1f}M"
        if oi >= 1e3: return f"${oi/1e3:.0f}K"
        return f"${oi:.0f}"

class _ApexStore:
    def __init__(self):
        self._tickers: Dict[str, ApexTicker] = {}
        self._cache_ts: float = 0
        self._source: str = ""
    def set(self, sym: str, t: ApexTicker): self._tickers[sym.upper()] = t
    def get(self, sym: str) -> Optional[ApexTicker]: return self._tickers.get(sym.upper())
    def is_fresh(self) -> bool: return (time.time() - self._cache_ts) / 60 < APEX_CACHE_TTL_MIN
    def mark_updated(self, source: str): self._cache_ts = time.time(); self._source = source

_store = _ApexStore()

# ════════════════════════════════════════════════════════════════
# ПАРСИНГ
# ════════════════════════════════════════════════════════════════

def _clean_sym(raw: str) -> str:
    s = raw.upper().strip()
    # Обработка сложных символов ccxt типа SAHARA/USDT:USDT
    if "/" in s: s = s.split("/")[0]
    for sfx in ("-USDT", "-USDC", "_USDT", "_USDC", "USDT", "USDC"):
        if s.endswith(sfx): s = s[:-len(sfx)]; break
    return APEX_SYMBOL_MAP.get(s, s)

def _parse_next_funding(val, now_ts: float) -> float:
    if val is None: return 3600.0
    try:
        # ISO формат: 2024-05-25T07:00:00Z
        if isinstance(val, str) and 'T' in val:
            dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
            return max(0.0, dt.timestamp() - now_ts)
        
        v = int(float(str(val)))
        if v > 1e12: return max(0.0, v / 1000 - now_ts)
        return max(0.0, float(v) - now_ts)
    except: pass
    return 3600.0

def _parse_raw_ticker(sym: str, raw: dict, now: float, source: str) -> Optional[ApexTicker]:
    if not sym: return None
    # Приоритет полям из ccxt (t.get('mark')) и info
    mark = float(raw.get("markPrice") or raw.get("mark") or raw.get("last") or 0)
    idx = float(raw.get("indexPrice") or raw.get("index") or mark)
    
    # Фандинг на ApeX всегда в долях (0.0001 = 0.01%)
    fund_raw = float(raw.get("fundingRate") or raw.get("funding_rate") or (APEX_DEFAULT_RATE_H/100))
    fund_h = fund_raw * 100
    
    pred_raw = float(raw.get("predictedFundingRate") or raw.get("predicted_funding_rate") or fund_raw)
    pred_h = pred_raw * 100
    
    oi_coins = float(raw.get("openInterest") or raw.get("open_interest") or 0)
    oi_usdt = oi_coins * mark if mark > 0 else oi_coins
    
    next_s = _parse_next_funding(raw.get("nextFundingTime") or raw.get("next_funding_time"), now)
    
    return ApexTicker(
        symbol=sym, mark_price=mark, index_price=idx, 
        funding_rate_h=round(fund_h, 8), predicted_rate_h=round(pred_h, 8), 
        open_interest=round(oi_usdt, 2), next_funding_s=next_s, 
        volume_24h_usdt=0, source=source, ts=now,
        deviation=round((mark-idx)/idx*100, 4) if idx>0 else 0
    )

# ════════════════════════════════════════════════════════════════
# ЛОГИКА ОБНОВЛЕНИЯ
# ════════════════════════════════════════════════════════════════

async def refresh_apex() -> dict:
    now = time.time()
    try:
        import ccxt.async_support as ccxt
        ex = ccxt.apex({"enableRateLimit": True})
        try:
            tickers = await asyncio.wait_for(ex.fetch_tickers(), timeout=APEX_TIMEOUT_S)
            count = 0
            for s_full, t in tickers.items():
                sym = _clean_sym(s_full)
                # Объединяем ccxt ticker и raw info
                data = {**t.get("info", {}), "mark": t.get("mark"), "index": t.get("index")}
                ticker = _parse_raw_ticker(sym, data, now, "ccxt")
                if ticker and ticker.mark_price > 0:
                    _store.set(sym, ticker)
                    count += 1
            
            if count > 0:
                _store.mark_updated("ccxt")
                logger.info(f"ApeX refresh [ccxt]: {count} assets")
                return {"ok": True, "source": "ccxt", "count": count}
        finally:
            await ex.close()
    except Exception as e:
        logger.error(f"ApeX refresh error: {e}")

    # Fallback to static
    for sym in STATIC_APEX_SYMBOLS:
        if not _store.get(sym):
            _store.set(sym, ApexTicker(sym, 0, 0, APEX_DEFAULT_RATE_H, APEX_DEFAULT_RATE_H, 0, 3600, 0, 0, "static", now))
    _store.mark_updated("static")
    return {"ok": False, "source": "static", "count": len(STATIC_APEX_SYMBOLS)}

# ════════════════════════════════════════════════════════════════
# ПУБЛИЧНЫЙ API
# ════════════════════════════════════════════════════════════════

def get_apex_info(symbol: str) -> ApexInfo:
    sym = _clean_sym(symbol); t = _store.get(sym)
    if not t:
        in_s = sym in STATIC_APEX_SYMBOLS
        return ApexInfo(sym, in_s, APEX_DEFAULT_RATE_H if in_s else 0, APEX_DEFAULT_RATE_H if in_s else 0, 0, 0, 3600, "static" if in_s else "none")
    return ApexInfo(sym, True, t.funding_rate_h, t.predicted_rate_h, t.open_interest, t.deviation, t.next_funding_s, t.source)

def format_apex_block(symbol: str, long_ex: str, long_rate_h: float, size_usd: float = 50) -> str:
    info = get_apex_info(symbol)
    if not info.available: return f"🔷 ApeX: {symbol} — нет на DEX"
    
    r = info.funding_rate_h
    r_str = f"{r:+.6f}%/ч"
    if info.apex_pays_shorts: r_str += " 🔴 ApeX ПЛАТИТ!"
    
    pred_str = f" | прогноз {info.predicted_h:+.6f}%/ч" if abs(info.predicted_h - r) > 1e-6 else ""
    
    lines = [f"🔷 ApeX Omni [{symbol}] (цикл 1ч):", f"   Funding: {r_str}{pred_str}"]
    if info.open_interest > 0: lines.append(f"   OI: {info.oi_m}")
    if info.deviation != 0: lines.append(f"   Index dev: {info.deviation:+.3f}%")
    
    # Стратегия
    earn_h = abs(long_rate_h) if long_rate_h < 0 else 0.0
    pay_h = r if r > 0 else 0.0; recv_h = abs(r) if r < 0 else 0.0
    net_8h = (earn_h - pay_h + recv_h) * 8
    
    if net_8h > 0.05:
        lines += [f"", f"   💡 LONG {long_ex.upper()} ({long_rate_h:+.4f}%/ч) + SHORT ApeX",
                  f"   Net/8ч: {net_8h:+.4f}% (${net_8h/100*size_usd:+.3f} на $50)"]
    return "\n".join(lines)

async def cmd_apex(update, context):
    if not context.args: await update.message.reply_text("Использование: `/apex SYM`")
    else: 
        sym = context.args[0].upper()
        await refresh_apex()
        await update.message.reply_text(format_apex_block(sym, "okx", -0.01), parse_mode="Markdown")
