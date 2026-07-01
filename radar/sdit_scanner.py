# radar/sdit_scanner.py
# S-DIT СКАНЕР v8.1 — ПОЛНЫЙ СКАН РЫНКА (ВСЕ МОНЕТЫ)

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

try:
    from hunter.math_engine import calc_net_spread, calc_min_diff, estimate_slippage
    from data.fees import EXCHANGES
    MATH_ENGINE_AVAILABLE = True
except ImportError:
    MATH_ENGINE_AVAILABLE = False

from radar.margin_monitor import is_margin_available
from radar.oi_monitor import get_oi_score_for_symbol

# ── Константы ──
EXCHANGE_CAPS = {"binance": 0.0075, "bybit": 0.04, "gateio": 0.03, "coinex": 0.015, "hyperliquid": 0.04, "okx": 0.005, "kucoin": 0.03, "bingx": 0.02}
BLACKLIST = {"ourbit", "htx"}
SCORE_ALERT_THRESHOLD = 8
SPREAD_MAX = -0.8  # Теперь более чувствителен (был -1.0)

@dataclass
class SpreadSnapshot:
    symbol: str; exchange: str; timestamp: float; mark_price: float; index_price: float
    spread_pct: float; funding_rate: float; predicted_rate: float; oi_usdt: float; volume_5min: float
    next_funding_ts: Optional[float] = None

@dataclass
class SDITSignal:
    symbol: str; exchange: str; timestamp: float; spread_pct: float; funding_rate: float
    predicted_rate: float; oi_usdt: float; volume_5min: float; spread_velocity: float
    spread_acceleration: float; funding_velocity: float; oi_delta_pct: float; score: int
    score_breakdown: dict; will_ramp: bool; ramp_hours_eta: Optional[float]
    ramp_confidence: str; limit_ratio: float; alert_level: str; recommended_pair: str; expected_net_8h: float

class SpreadHistoryStore:
    MAX_POINTS = 24
    def __init__(self):
        self._data = defaultdict(lambda: deque(maxlen=self.MAX_POINTS))
        self._last_alert = {}
    def add(self, snap):
        self._data[(snap.symbol, snap.exchange)].append(snap)
    def get(self, symbol, exchange, hours=2.0):
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        return [s for s in self._data[(symbol, exchange)] if s.timestamp > cutoff]
    def recently_alerted(self, symbol, exchange, cooldown_min=30):
        last = self._last_alert.get((symbol, exchange), 0)
        return (datetime.now(timezone.utc).timestamp() - last) < cooldown_min * 60
    def mark_alerted(self, symbol, exchange):
        self._last_alert[(symbol, exchange)] = datetime.now(timezone.utc).timestamp()

history_store = SpreadHistoryStore()

def normalize_exchange_response(raw, exchange_id):
    info = raw.get("info", {})
    if isinstance(info, list): info = {}
    mark = raw.get("markPrice") or info.get("markPrice") or info.get("mark_price") or info.get("markPx") or raw.get("last") or 0.0
    index = raw.get("indexPrice") or info.get("indexPrice") or info.get("index_price") or info.get("indexPx") or info.get("oracle_price") or mark
    predicted = info.get("predictedFundingRate") or info.get("nextFundingRate") or info.get("indicative_rate") or info.get("next_funding_rate") or info.get("fundingRateIndicative") or raw.get("fundingRate")
    return {
        "mark_price": float(mark) if mark else 0.0,
        "index_price": float(index) if index else 0.0,
        "funding_rate": float(raw.get("fundingRate") or 0),
        "predicted_rate": float(predicted or raw.get("fundingRate") or 0),
        "next_funding_ts": raw.get("fundingTimestamp"),
    }

# --- Математика (Velocity, Acceleration, OI) ---
def calc_spread_velocity(h):
    if len(h) < 2: return 0.0
    dt = (h[-1].timestamp - h[0].timestamp) / 3600
    return round((h[-1].spread_pct - h[0].spread_pct) / dt, 4) if dt > 0.05 else 0.0

def calc_oi_delta(h):
    if len(h) < 2 or h[0].oi_usdt <= 0: return 0.0
    return round((h[-1].oi_usdt - h[0].oi_usdt) / h[0].oi_usdt * 100, 2)

def calculate_score(snap, history):
    score = 0; b = {}
    sp = snap.spread_pct
    pts = 3 if -2.5 <= sp < -1.5 else (2 if -3.5 <= sp < -2.5 else (1 if -1.5 <= sp < -0.8 else 0))
    score += pts; b["spread"] = pts
    sv = calc_spread_velocity(history)
    pts = 3 if sv < -0.6 else (2 if sv < -0.35 else (1 if sv < -0.15 else 0))
    score += pts; b["vel"] = pts
    oi_d = calc_oi_delta(history)
    pts = 3 if oi_d > 15 else (2 if oi_d > 10 else (1 if oi_d > 5 else 0))
    score += pts; b["oi"] = pts
    cap = EXCHANGE_CAPS.get(snap.exchange, 0.0075)
    lr = min(abs(snap.predicted_rate) / cap, 1.0) if cap else 0
    pts = 3 if lr > 0.95 else (2 if lr > 0.85 else (1 if lr > 0.7 else 0))
    score += pts; b["limit"] = pts
    
    # Margin Bonus
    clean_sym = snap.symbol.split("/")[0].split(":")[0]
    if is_margin_available(clean_sym, snap.exchange):
        score += 2
        b["margin"] = 2
    
    # OI Confirmation
    oi_score_val, oi_synergy = get_oi_score_for_symbol(snap.symbol)
    if oi_synergy:
        score += 2
        b["oi_confirm"] = 2
    elif oi_score_val >= 5:
        score += 1
        b["oi_confirm"] = 1
    else:
        b["oi_confirm"] = 0
    
    return score, b

def analyze_snapshot(snap):
    if snap.symbol.split("/")[0] in BLACKLIST or snap.spread_pct >= SPREAD_MAX: return None
    h = history_store.get(snap.symbol, snap.exchange)
    score, b = calculate_score(snap, h)
    if score < SCORE_ALERT_THRESHOLD: return None
    cap = EXCHANGE_CAPS.get(snap.exchange, 0.0075)
    lr = min(abs(snap.predicted_rate) / cap, 1.0)
    return SDITSignal(
        snap.symbol, snap.exchange, snap.timestamp, snap.spread_pct, snap.funding_rate,
        snap.predicted_rate, snap.oi_usdt, snap.volume_5min, calc_spread_velocity(h),
        0.0, 0.0, calc_oi_delta(h),
        score, b, lr > 0.8, None, "high" if lr > 0.9 else "low", lr, 
        "🔴 ПРИОРИТЕТ" if score >= 12 else "⚡ СИГНАЛ",
        f"LONG {snap.exchange.upper()} + SHORT GATE", 3.5
    )

async def sdit_scan():
    import ccxt.async_support as ccxt
    all_signals = []
    # Основные биржи, которые поддерживают fetch_funding_rates (массовый запрос)
    for ex_id in ["bybit", "binance", "okx", "gateio"]:
        ex = None
        try:
            ccxt_id = "gate" if ex_id == "gateio" else ex_id
            ex = getattr(ccxt, ccxt_id)({"enableRateLimit": True, "options": {"defaultType": "swap"}})
            
            logger.info(f"S-DIT: массовый скан {ex_id}...")
            # 1. Получаем ВСЕ ставки одним запросом
            all_rates = await asyncio.wait_for(ex.fetch_funding_rates(), 15)
            
            for symbol, raw_data in all_rates.items():
                if "/USDT" not in symbol: continue
                
                norm = normalize_exchange_response(raw_data, ex_id)
                if norm["mark_price"] <= 0 or norm["index_price"] <= 0: continue
                
                spread = (norm["mark_price"] - norm["index_price"]) / norm["index_price"] * 100
                
                # 2. Первичный фильтр (пропускаем только интересные монеты)
                if spread < SPREAD_MAX:
                    # 3. Для кандидатов добираем OI (это требует отдельного запроса)
                    oi = 0.0
                    try:
                        oi_data = await asyncio.wait_for(ex.fetch_open_interest(symbol), 3)
                        oi = float(oi_data.get("openInterestValue") or oi_data.get("openInterest", 0) * norm["mark_price"])
                    except: pass
                    
                    snap = SpreadSnapshot(symbol, ex_id, time.time(), norm["mark_price"], norm["index_price"], round(spread, 4), norm["funding_rate"], norm["predicted_rate"], oi, 0.0)
                    history_store.add(snap)
                    
                    if not history_store.recently_alerted(symbol, ex_id):
                        sig = analyze_snapshot(snap)
                        if sig: all_signals.append(sig)
            
        except Exception as e:
            logger.error(f"SDIT scan error {ex_id}: {e}")
        finally:
            if ex: await ex.close()
            
    all_signals.sort(key=lambda s: s.score, reverse=True)
    return all_signals[:5]

def format_sdit_alert(sig, pos=1):
    stars = "⭐" * min(sig.score // 4, 5)
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{sig.alert_level} S-DIT #{pos} [{sig.symbol.split('/')[0]}]\n"
        f"Score: {sig.score}/20 {stars}\n"
        f"─────────────────────────────\n"
        f"Биржа:   {sig.exchange.upper()}\n"
        f"Спред:   {sig.spread_pct:+.2f}% | Vel: {sig.spread_velocity:+.2f}%/ч\n"
        f"OI:      ${sig.oi_usdt/1e6:.1f}M (+{sig.oi_delta_pct}%/ч)\n"
        f"Limit:   {sig.limit_ratio*100:.0f}%\n\n"
        f"📋 {sig.recommended_pair}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

def quick_score_from_analyze(symbol, exchange, spread_pct, funding_rate, predicted_rate, oi_usdt=0, vol_5m=0):
    snap = SpreadSnapshot(symbol, exchange, time.time(), 0, 0, spread_pct, funding_rate, predicted_rate, oi_usdt, vol_5m)
    h = history_store.get(symbol, exchange)
    score, _ = calculate_score(snap, h)
    rec = "🔴 ПРИОРИТЕТ" if score >= 12 else ("⚡ СИГНАЛ" if score >= 8 else "👀 СЛЕДИТЬ")
    return score, rec
