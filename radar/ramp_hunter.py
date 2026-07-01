# radar/ramp_hunter.py
# СТРАТЕГИЯ: INDEX MOMENTUM + RAMP HUNTER
# Ловит монеты, которые "разгоняются" (premium уходит в минус)

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict, deque
from loguru import logger
import ccxt.async_support as ccxt

from data.fees import EXCHANGES, BLACKLIST
from data.exchanges import _make_exchange, get_btc_ohlcv

# ── Пороги стратегии ────────────────────────────────────────────────────
ENTRY_PREMIUM_MIN  = -0.15   # начинаем следить при premium < -0.15%
VELOCITY_THRESHOLD = -0.30   # %/ч — монета разгоняется в минус
CAP_PCT_ENTER      = 50      # % от лимита (на быстрой бирже) для входа
SLOW_EX_MAX_CAP    = 30      # % от лимита на медленной ноге (чтобы не платить)

@dataclass
class RampHunterSignal:
    symbol:             str
    current_premium:    float
    premium_velocity:   float    # %/ч
    eta_ramp_hours:     float    # часов до 90% лимита
    ramp_exchange:      str      # быстрая (LONG)
    hedge_exchange:     str      # медленная (SHORT)
    entry_now:          bool
    reason:             str
    is_at_high:         bool     # находится ли на 24ч хаях
    price_change_24h:   float
    expected_net_8h:    float
    action:             str

class PremiumHistoryStore:
    """Хранит историю среднего premium для расчета velocity."""
    def __init__(self):
        self._history = defaultdict(lambda: deque(maxlen=12))

    def add(self, symbol: str, premium: float):
        self._history[symbol].append((time.time(), premium))

    def get_velocity(self, symbol: str) -> float:
        h = list(self._history[symbol])
        if len(h) < 2: return 0.0
        dt = (h[-1][0] - h[0][0]) / 3600
        dp = h[-1][1] - h[0][1]
        return round(dp / dt, 4) if dt > 0.01 else 0.0

    def get_latest(self, symbol: str) -> float:
        return self._history[symbol][-1][1] if self._history[symbol] else 0.0

premium_store = PremiumHistoryStore()

# ── Инструменты анализа ────────────────────────────────────────────────

async def check_price_context(symbol: str) -> dict:
    try:
        ex = ccxt.binance({"enableRateLimit": True})
        ohlcv = await asyncio.wait_for(ex.fetch_ohlcv(f"{symbol}/USDT", timeframe='1h', limit=24), 10)
        await ex.close()
        
        if not ohlcv: return {"is_high": False, "change": 0.0}
        closes = [x[4] for x in ohlcv]
        highs  = [x[2] for x in ohlcv]
        current_price = closes[-1]
        max_24h = max(highs)
        min_24h = min(closes)
        change = ((current_price - min_24h) / min_24h * 100) if min_24h > 0 else 0
        is_high = current_price >= max_24h * 0.98
        return {"is_high": is_high, "change": round(change, 2)}
    except:
        return {"is_high": False, "change": 0.0}

async def fetch_premium(exchange_id: str, symbol: str) -> Optional[dict]:
    """Оптимизированный сбор данных (один коннект, один запрос)."""
    ex = _make_exchange(exchange_id)
    if not ex: return None
    try:
        # fetch_funding_rate обычно возвращает и ставку, и mark/index цены
        data = await asyncio.wait_for(ex.fetch_funding_rate(f"{symbol}/USDT:USDT"), 7)
        mark = data.get("markPrice")
        index = data.get("indexPrice")
        rate = data.get("fundingRate")

        # Если цен нет в funding_rate, пробуем тикер (но это доп запрос)
        if mark is None or index is None:
            ticker = await asyncio.wait_for(ex.fetch_ticker(f"{symbol}/USDT:USDT"), 5)
            mark = mark or ticker.get("markPrice") or ticker.get("last")
            index = index or ticker.get("indexPrice")

        if not mark or not index or index == 0:
            return None

        premium = round((float(mark) - float(index)) / float(index) * 100, 4)
        return {"premium": premium, "rate": (rate or 0) * 100}
    except:
        return None
    finally:
        await ex.close()

async def fetch_all_premiums(symbol: str, exchanges: list[str]) -> dict:
    tasks = [fetch_premium(ex, symbol) for ex in exchanges]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    data = {}
    for ex, res in zip(exchanges, results):
        if res and isinstance(res, dict):
            data[ex] = res
    return data

def detect_ramp_hunter(symbol: str, rates: dict[str, float], velocity: float, latest_premium: float) -> Optional[RampHunterSignal]:
    if not rates: return None
    best_fast, best_fast_score = None, -1.0
    best_slow, best_slow_cap = None, 999.0

    for ex, rate in rates.items():
        if ex in BLACKLIST: continue
        cfg = EXCHANGES.get(ex.lower())
        if not cfg: continue
        cap = cfg.get("funding_cap", 0.75)
        lag = cfg.get("lag_hours", 2.0)
        cap_pct = abs(rate) / cap * 100
        if lag <= 1.5:
            score = cap_pct / lag
            if score > best_fast_score:
                best_fast, best_fast_score = ex, score
        if lag >= 3.0:
            if cap_pct < best_slow_cap:
                best_slow, best_slow_cap = ex, cap_pct

    if not best_fast or not best_slow: return None
    cfg_f = EXCHANGES[best_fast.lower()]
    cap_f, lag_f = cfg_f["funding_cap"], cfg_f["lag_hours"]
    rate_vel = abs(velocity) / lag_f / 8
    gap = (cap_f * 0.9) - abs(rates[best_fast])
    eta = round(gap / rate_vel, 1) if rate_vel > 0.001 else 99.0
    if gap <= 0: eta = 0.0

    entry_now, reason = False, "Наблюдение"
    cap_pct_f = abs(rates[best_fast]) / cap_f * 100
    if cap_pct_f >= 90:
        entry_now, reason = True, f"🔴 РАМПА АКТИВНА ({cap_pct_f:.0f}%)"
    elif cap_pct_f >= CAP_PCT_ENTER and velocity <= VELOCITY_THRESHOLD:
        entry_now, reason = True, f"⚡ ИМПУЛЬС: Velocity {velocity:+.2f}%/ч, Рампа через ~{eta}ч"
    elif velocity < -0.6:
        entry_now, reason = True, f"🚀 ВЗРЫВНОЙ MOMENTUM: Velocity {velocity:+.2f}%/ч"

    net_8h = (cap_f * 8) - abs(rates[best_slow]) - 0.2
    return RampHunterSignal(
        symbol=symbol, current_premium=latest_premium, premium_velocity=velocity,
        eta_ramp_hours=eta, ramp_exchange=best_fast, hedge_exchange=best_slow,
        entry_now=entry_now, reason=reason, is_at_high=False, price_change_24h=0.0,
        expected_net_8h=round(net_8h, 3), action=f"LONG {best_fast.upper()} + SHORT {best_slow.upper()}"
    )

async def ramp_scan() -> list[RampHunterSignal]:
    """Сканирует аномалии премиума (рампы) по списку монет."""
    from core.config import settings
    symbols = settings.watch_symbols.split(",")
    exchanges = ["binance", "bybit", "okx", "gate"]
    
    signals = []
    for sym in symbols:
        try:
            premiums = await fetch_all_premiums(sym, exchanges)
            if not premiums: continue
            
            # Считаем средний премиум и velocity
            avg_prem = sum(p["premium"] for p in premiums.values()) / len(premiums)
            premium_store.add(sym, avg_prem)
            velocity = premium_store.get_velocity(sym)
            
            # Ставки фандинга для анализа рампы
            rates = {ex: p["rate"] for ex, p in premiums.items()}
            
            sig = detect_ramp_hunter(sym, rates, velocity, avg_prem)
            if sig and (sig.entry_now or abs(sig.premium_velocity) > 0.1):
                signals.append(sig)
        except Exception as e:
            logger.error(f"Error scanning {sym} for ramps: {e}")
            
    return signals

def format_ramp_alert(sig: RampHunterSignal) -> tuple[str, any]:
    """Форматирует алерт с клавиатурой."""
    from bot.keyboards import get_analyze_keyboard
    icon = "🚀" if sig.entry_now else "👀"
    high_warn = "\n⚠️ *МОНЕТА НА ХАЯХ (Риск коррекции)*" if sig.is_at_high else ""
    text = (
        f"{icon} *RAMP HUNTER: {sig.symbol}*\n"
        f"{'─'*30}\n"
        f"Premium: `{sig.current_premium:+.3f}%` | Vel: `{sig.premium_velocity:+.3f}%/ч`\n"
        f"До рампы: `{sig.eta_ramp_hours if sig.eta_ramp_hours < 24 else '>24'}ч`\n\n"
        f"🔥 *LONG*: {sig.ramp_exchange.upper()} ({abs(sig.expected_net_8h):.2f}% potential)\n"
        f"🛡 *SHORT*: {sig.hedge_exchange.upper()} (медленная)\n\n"
        f"📈 Рост 24ч: `+{sig.price_change_24h}%`{high_warn}\n"
        f"📍 *Статус*: {sig.reason}\n"
        f"✅ *Действие*: `{sig.action}`"
    )
    return text, get_analyze_keyboard(sig.symbol)
