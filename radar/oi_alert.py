# radar/oi_alert.py
import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from loguru import logger
from radar.ramp_memory import record_ramp_from_oi_alert

# ════════════════════════════════════════════════════════════════
# КОНФИГ
# ════════════════════════════════════════════════════════════════

SHORTS_FLOOD_OI_H    = +8.0    
SHORTS_FLOOD_OI_5M   = +1.5    
SHORTS_FLOOD_PREM    = -0.3    

STEADY_OI_H_MIN      = +3.0    
STEADY_OI_H_MAX      = +8.0    
STEADY_PERIODS       = 2       
STEADY_OI_VOL        = 1.2     

EXIT_OI_5M           = -2.0    
EXIT_OI_H            = -5.0    
EXIT_PREM_UP         = +0.10   

ALERT_COOLDOWN_MIN   = 10      
OI_ALERT_MIN_SCORE   = 5       
EXCHANGES_TO_SCAN = ["binance", "bybit", "okx", "gate"]

# ════════════════════════════════════════════════════════════════
# МОДЕЛИ
# ════════════════════════════════════════════════════════════════

@dataclass
class OIPoint:
    ts:           float
    symbol:       str
    exchange:     str
    oi_usdt:      float
    price:        float
    volume_5m:    float
    ls_ratio:     float

@dataclass
class OISignal:
    pattern:      str     # SHORTS_FLOOD / STEADY_BUILD / POSITION_EXIT / LONGS_FLOOD
    symbol:       str
    exchange:     str
    ts:           float
    oi_now:       float
    oi_delta_5m:  float
    oi_delta_1h:  float
    oi_delta_4h:  float
    oi_vol_ratio: float
    price_delta:  float
    ls_ratio:     float
    premium:      float
    prem_vel:     float
    score:        int     # 0-10
    recommendation: str   # ENTER / WATCH / EXIT / IGNORE
    description:  str

class OIStore:
    MAX_PER_KEY = 60
    def __init__(self):
        self._hist = defaultdict(lambda: deque(maxlen=self.MAX_PER_KEY))
        self._alerted = {}
    def add(self, p: OIPoint): self._hist[(p.symbol, p.exchange)].append(p)
    def get_window(self, symbol: str, exchange: str, hours: float) -> List[OIPoint]:
        now = datetime.now(timezone.utc).timestamp()
        return [pt for pt in self._hist[(symbol, exchange)] if pt.ts > now - hours * 3600]
    def cooldown_ok(self, symbol: str) -> bool:
        last = self._alerted.get(symbol, 0)
        return (datetime.now(timezone.utc).timestamp() - last) / 60 > ALERT_COOLDOWN_MIN
    def mark_alerted(self, symbol: str): self._alerted[symbol] = datetime.now(timezone.utc).timestamp()

oi_store = OIStore()

# ════════════════════════════════════════════════════════════════
# АНАЛИЗ
# ════════════════════════════════════════════════════════════════

def _calc_delta(pts: List[OIPoint], hours: float) -> float:
    if len(pts) < 2: return 0.0
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    old = next((p for p in reversed(pts) if p.ts <= cutoff + 300), pts[0])
    return (pts[-1].oi_usdt - old.oi_usdt) / old.oi_usdt * 100 if old.oi_usdt > 0 else 0.0

def detect_oi_pattern(symbol: str, exchange: str) -> Optional[OISignal]:
    pts = oi_store.get_window(symbol, exchange, 5.0)
    if len(pts) < 3: return None
    cur = pts[-1]; prev = pts[-2]
    d5m = _calc_delta(pts, 5/60); d1h = _calc_delta(pts, 1.0); d4h = _calc_delta(pts, 4.0)
    price_d = (cur.price - prev.price) / prev.price * 100 if prev.price > 0 else 0.0
    
    premium = 0.0; prem_vel = 0.0
    try:
        from radar.index_deviation_radar import dev_store, calc_velocity
        snap = dev_store.get_latest(symbol, exchange)
        if snap:
            premium = snap.deviation
            prem_vel = calc_velocity(dev_store.get(symbol, exchange, hours=2.0))
    except: pass

    # Паттерны
    if d1h >= SHORTS_FLOOD_OI_H and price_d <= 0.5 and (premium <= SHORTS_FLOOD_PREM or prem_vel < -0.3):
        score = 5 + (2 if d5m > 1.5 else 0) + (2 if premium < -1.0 else 0)
        return OISignal("SHORTS_FLOOD", symbol, exchange, cur.ts, cur.oi_usdt, d5m, d1h, d4h, 1.5, price_d, cur.ls_ratio, premium, prem_vel, min(score, 10), "ENTER" if score >= 7 else "WATCH", f"Заливка шортов: OI {d1h:+.1f}%/ч")

    if (d5m <= EXIT_OI_5M or d1h <= EXIT_OI_H) and prem_vel > EXIT_PREM_UP:
        score = 6 + (2 if d5m < -3.0 else 0)
        return OISignal("POSITION_EXIT", symbol, exchange, cur.ts, cur.oi_usdt, d5m, d1h, d4h, 1.0, price_d, cur.ls_ratio, premium, prem_vel, min(score, 10), "EXIT", f"Выход из позиций: OI {d5m:+.1f}%/5м")

    return None

# ════════════════════════════════════════════════════════════════
# СКАНЕР
# ════════════════════════════════════════════════════════════════

async def oi_scan_all(symbols: List[str] = None):
    if not symbols:
        try:
            from radar.index_deviation_radar import dev_store
            symbols = list({s.symbol for s in dev_store.get_all_latest() if s.deviation < -0.2})[:40]
        except: return []

    import ccxt.async_support as ccxt
    signals = []
    now = datetime.now(timezone.utc).timestamp()

    for ex_id in EXCHANGES_TO_SCAN:
        try:
            ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
            for sym in symbols:
                try:
                    raw = await ex.fetch_open_interest(f"{sym}/USDT:USDT")
                    oi_usd = float(raw.get("openInterestValue") or raw.get("openInterest", 0) or 0)
                    ticker = await ex.fetch_ticker(f"{sym}/USDT:USDT")
                    price = float(ticker.get("last", 0))
                    if oi_usd < 1000: oi_usd *= price
                    
                    oi_store.add(OIPoint(now, sym, ex_id, oi_usd, price, 0, 0))
                    sig = detect_oi_pattern(sym, ex_id)
                    if sig and sig.score >= OI_ALERT_MIN_SCORE and oi_store.cooldown_ok(sym):
                        signals.append(sig)
                        oi_store.mark_alerted(sym)
                        
                        # Ramp Memory Integration
                        if sig.pattern == "SHORTS_FLOOD":
                            try:
                                record_ramp_from_oi_alert(sig)
                            except Exception as ree:
                                logger.error(f"RampDB record error: {ree}")
                except: continue
            await ex.close()
        except: continue
    return signals

def format_oi_signal(sig: OISignal) -> str:
    icon = {"SHORTS_FLOOD":"🔴","POSITION_EXIT":"⚠️"}.get(sig.pattern, "📊")
    return (
        f"{icon} *OI ALERT: {sig.pattern}* [{sig.symbol}]\n"
        f"Биржа: {sig.exchange.upper()}\n"
        f"----------------------------\n"
        f"OI Change: {sig.oi_delta_5m:+.1f}% (5м) / {sig.oi_delta_1h:+.1f}% (1ч)\n"
        f"Premium: {sig.premium:+.3f}% (vel {sig.prem_vel:+.2f}/ч)\n"
        f"----------------------------\n"
        f"💡 {sig.description}\n"
        f"🚀 Рекомендация: *{sig.recommendation}*"
    )
