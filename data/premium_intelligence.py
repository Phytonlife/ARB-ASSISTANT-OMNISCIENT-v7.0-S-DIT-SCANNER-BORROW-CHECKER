# data/premium_intelligence.py
# PREMIUM INTELLIGENCE MODULE — предсказание фандинга через индекс-цену

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
import ccxt.async_support as ccxt
from loguru import logger
from data.fees import EXCHANGES


@dataclass
class PremiumData:
    exchange: str
    symbol: str
    mark_price: float
    index_price: float
    premium_pct: float          # (mark - index) / index * 100
    current_rate: float
    rate_history: list[float]   # последние 4 выплаты [старый→новый]
    funding_cap: float
    cap_pct: float              # текущий rate / cap * 100
    velocity: float             # изменение premium в % за час
    minutes_to_next: int        # минут до следующей выплаты


@dataclass
class PremiumIntelligence:
    """Полный анализ premium для одного сигнала."""
    symbol: str
    long_ex: str
    short_ex: str
    long_data: Optional[PremiumData]
    short_data: Optional[PremiumData]

    # Выводы
    trend_signal: str           # "РАСХОДИТСЯ" / "СХОДИТСЯ" / "СТАБИЛЕН"
    trend_icon: str
    trend_velocity: float       # %/выплату
    hours_to_zero: float        # сколько часов до rate=0
    predictions: list[dict]     # прогноз 3 выплат
    limit_arb: bool             # одна биржа у лимита, другая нет
    limit_potential: float      # потенциальный diff когда догонит
    fast_slow: str              # описание fast/slow бирж
    countdown_str: str          # "⏰ До выплаты: 3ч 42мин"
    recommendation: str         # итоговая рекомендация с учётом premium


# ═══════════════════════════════════════════════════════════════════════
# ФУНКЦИИ ПОЛУЧЕНИЯ ДАННЫХ (через ccxt, бесплатно)
# ═══════════════════════════════════════════════════════════════════════

async def get_premium_data(exchange_id: str, symbol: str) -> Optional[PremiumData]:
    """
    Получает mark_price, index_price, funding_rate_history через ccxt.
    Работает без API ключей для большинства бирж.
    """
    try:
        ex_class = getattr(ccxt, exchange_id)
        ex = ex_class({"enableRateLimit": True})

        # 1. Текущий premium index
        ticker_symbol = f"{symbol}/USDT:USDT"
        ticker = await ex.fetch_ticker(ticker_symbol)
        mark_price  = ticker.get("markPrice") or ticker.get("last", 0)
        index_price = ticker.get("indexPrice") or ticker.get("info", {}).get("indexPrice")

        if not index_price:
            try:
                pm = await ex.fetch_funding_rate(ticker_symbol)
                index_price = pm.get("indexPrice", mark_price)
            except Exception:
                index_price = mark_price

        premium_pct = 0.0
        if index_price and float(index_price) > 0:
            premium_pct = round((float(mark_price) - float(index_price)) / float(index_price) * 100, 4)

        # 2. Текущий funding rate
        try:
            fr = await ex.fetch_funding_rate(ticker_symbol)
            current_rate = fr.get("fundingRate", 0) * 100
        except Exception:
            current_rate = premium_pct / 8

        # 3. История последних 4 выплат
        rate_history = []
        try:
            history = await ex.fetch_funding_rate_history(
                ticker_symbol, limit=4
            )
            rate_history = [h["fundingRate"] * 100 for h in history]
        except Exception:
            rate_history = [current_rate]

        # 4. Время до следующей выплаты
        minutes_to_next = _minutes_to_next_funding(exchange_id)

        cfg = EXCHANGES.get(exchange_id.lower(), {})
        cap = cfg.get("funding_cap", 0.75)
        cap_pct = abs(current_rate) / cap * 100 if cap > 0 else 0

        # 5. Velocity
        velocity = 0.0
        if len(rate_history) >= 2:
            deltas = [rate_history[i] - rate_history[i-1]
                      for i in range(1, len(rate_history))]
            velocity = sum(deltas) / len(deltas)

        await ex.close()
        return PremiumData(
            exchange=exchange_id,
            symbol=symbol,
            mark_price=float(mark_price),
            index_price=float(index_price),
            premium_pct=premium_pct,
            current_rate=current_rate,
            rate_history=rate_history,
            funding_cap=cap,
            cap_pct=round(cap_pct, 1),
            velocity=round(velocity, 5),
            minutes_to_next=minutes_to_next,
        )
    except Exception as e:
        logger.warning(f"PremiumData {exchange_id} {symbol}: {e}")
        return None


def _minutes_to_next_funding(exchange_id: str = "binance") -> int:
    """Минут до следующей выплаты фандинга."""
    now = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    next_h = ((h // 8) + 1) * 8 % 24
    mins_left = (next_h - h) * 60 - m
    if mins_left <= 0:
        mins_left += 480
    return mins_left


# ═══════════════════════════════════════════════════════════════════════
# АНАЛИТИКА
# ═══════════════════════════════════════════════════════════════════════

def analyze_trend(rate_history: list[float], current_rate: float) -> dict:
    """Анализирует тренд изменения rate."""
    if len(rate_history) < 2:
        return {"trend": "НЕИЗВЕСТЕН", "icon": "❓", "velocity": 0,
                "hours_to_zero": 999}

    deltas = [rate_history[i] - rate_history[i-1]
              for i in range(1, len(rate_history))]
    velocity = sum(deltas) / len(deltas)

    abs_rate = abs(current_rate)
    abs_vel  = abs(velocity)

    converging = (velocity > 0 and current_rate < 0) or \
                 (velocity < 0 and current_rate > 0)

    if abs_vel < 0.005:
        trend, icon = "СТАБИЛЕН", "➡️"
    elif converging:
        trend, icon = "СХОДИТСЯ к нулю", "⚠️"
    else:
        trend, icon = "РАСХОДИТСЯ", "📈"

    if abs_vel > 0.001 and converging:
        payments = abs_rate / abs_vel
        hours_to_zero = round(payments * 8, 1)
    else:
        hours_to_zero = 999

    return {
        "trend": trend,
        "icon": icon,
        "velocity": round(velocity, 4),
        "hours_to_zero": hours_to_zero,
    }


def predict_next_payments(rate_history: list[float],
                           current_rate: float,
                           funding_cap: float = 0.75,
                           periods: int = 3) -> list[dict]:
    """Прогнозирует следующие N выплат на основе тренда."""
    if len(rate_history) < 2:
        return []

    deltas = [rate_history[i] - rate_history[i-1]
              for i in range(1, len(rate_history))]
    velocity = sum(deltas[-3:]) / min(len(deltas), 3)

    predictions = []
    rate = current_rate
    for n in range(1, periods + 1):
        rate_next = rate + velocity
        rate_next = max(-funding_cap, min(funding_cap, rate_next))

        viable = abs(rate_next) > 0.10
        predictions.append({
            "n": n,
            "hours": n * 8,
            "rate": round(rate_next, 4),
            "viable": viable,
            "icon": "✅" if viable else "❌",
        })
        rate = rate_next

    return predictions


def detect_limit_arb(long_data: PremiumData, short_data: PremiumData) -> dict:
    """Определяет ситуацию лимит-арбитража."""
    la = long_data.cap_pct
    sa = short_data.cap_pct

    at_limit_long  = la > 85
    lagging_short  = sa < 30
    at_limit_short = sa > 85
    lagging_long   = la < 30

    if at_limit_long and lagging_short:
        potential = abs(long_data.current_rate - short_data.current_rate)
        potential += (short_data.funding_cap - abs(short_data.current_rate)) * 0.6
        return {
            "detected": True,
            "message": (
                f"🎯 LIMIT ARB: {long_data.exchange.upper()} у лимита "
                f"({la:.0f}%), {short_data.exchange.upper()} отстаёт "
                f"({sa:.0f}%) — diff вырастет до ~{potential:.3f}%"
            ),
            "potential_diff": round(potential, 3),
        }
    elif at_limit_short and lagging_long:
        potential = abs(long_data.current_rate - short_data.current_rate)
        potential += (long_data.funding_cap - abs(long_data.current_rate)) * 0.6
        return {
            "detected": True,
            "message": (
                f"🎯 LIMIT ARB: {short_data.exchange.upper()} у лимита "
                f"({sa:.0f}%), {long_data.exchange.upper()} отстаёт "
                f"({la:.0f}%) — diff вырастет до ~{potential:.3f}%"
            ),
            "potential_diff": round(potential, 3),
        }
    return {"detected": False}


def classify_fast_slow(long_ex: str, short_ex: str) -> str:
    """Описывает какая биржа быстрая, какая медленная."""
    cfg_l = EXCHANGES.get(long_ex.lower(), {})
    cfg_s = EXCHANGES.get(short_ex.lower(), {})
    
    lag_l = cfg_l.get("lag_hours", 2.0)
    lag_s = cfg_s.get("lag_hours", 2.0)

    if lag_l > lag_s * 1.5:
        return f"🐢 {long_ex.upper()} медленный ({lag_l:.1f}ч лаг) | ⚡ {short_ex.upper()} быстрый"
    elif lag_s > lag_l * 1.5:
        return f"⚡ {long_ex.upper()} быстрый | 🐢 {short_ex.upper()} медленный ({lag_s:.1f}ч лаг)"
    else:
        return f"↔️ Обе биржи схожи по скорости"


async def get_premium_intelligence(
    symbol: str,
    long_ex: str,
    short_ex: str,
) -> PremiumIntelligence:
    """Получает полную premium intelligence для пары бирж."""
    long_data, short_data = await asyncio.gather(
        get_premium_data(long_ex, symbol),
        get_premium_data(short_ex, symbol),
        return_exceptions=True,
    )

    if isinstance(long_data, Exception):
        long_data = None
    if isinstance(short_data, Exception):
        short_data = None

    primary = long_data or short_data
    trend_info = {"trend": "нет данных", "icon": "❓",
                  "velocity": 0, "hours_to_zero": 999}
    predictions = []
    if primary:
        trend_info = analyze_trend(primary.rate_history, primary.current_rate)
        predictions = predict_next_payments(
            primary.rate_history,
            primary.current_rate,
            primary.funding_cap,
        )

    limit_arb_info = {"detected": False}
    if long_data and short_data:
        limit_arb_info = detect_limit_arb(long_data, short_data)

    fast_slow = classify_fast_slow(long_ex, short_ex)

    mins = _minutes_to_next_funding()
    h, m = divmod(mins, 60)
    countdown_str = f"⏰ До выплаты: {h}ч {m:02d}мин"

    recommendation = _build_recommendation(
        trend_info, limit_arb_info, primary, predictions
    )

    return PremiumIntelligence(
        symbol=symbol,
        long_ex=long_ex,
        short_ex=short_ex,
        long_data=long_data,
        short_data=short_data,
        trend_signal=trend_info["trend"],
        trend_icon=trend_info["icon"],
        trend_velocity=trend_info["velocity"],
        hours_to_zero=trend_info["hours_to_zero"],
        predictions=predictions,
        limit_arb=limit_arb_info["detected"],
        limit_potential=limit_arb_info.get("potential_diff", 0),
        fast_slow=fast_slow,
        countdown_str=countdown_str,
        recommendation=recommendation,
    )


def _build_recommendation(trend: dict, limit_arb: dict,
                           data: Optional[PremiumData],
                           predictions: list) -> str:
    """Строит итоговую рекомендацию."""
    issues = []
    good = []

    if trend["hours_to_zero"] < 24:
        issues.append(f"⚠️ Rate сходится к нулю через ~{trend['hours_to_zero']:.0f}ч")
    elif trend["trend"] == "РАСХОДИТСЯ":
        good.append("✅ Rate расходится — позиция улучшается")

    if limit_arb.get("detected"):
        good.append(f"🎯 Limit Arb — потенциал до {limit_arb['potential_diff']:.3f}%")

    if data:
        if data.cap_pct > 90:
            issues.append(
                f"⚠️ {data.exchange.upper()} у {data.cap_pct:.0f}% лимита"
            )

    viable_payments = sum(1 for p in predictions if p.get("viable", False))
    if viable_payments < 2:
        issues.append("⚠️ По прогнозу только 1-2 выплаты прибыльны")

    if issues and not good:
        return "⛔ Premium указывает НЕ ВХОДИТЬ: " + " | ".join(issues)
    elif good and not issues:
        return "✅ Premium подтверждает: " + " | ".join(good)
    elif issues:
        return "⚠️ Осторожно: " + " | ".join(issues)
    else:
        return "➡️ Premium нейтрален"


def format_premium_block(intel: PremiumIntelligence) -> str:
    """Возвращает блок текста для Telegram алерта."""
    lines = [intel.countdown_str]

    if intel.long_data:
        d = intel.long_data
        if d.index_price > 0:
            lines.append(
                f"💹 Mark ${d.mark_price:.4f} | Index ${d.index_price:.4f}\n"
                f"   Premium: {d.premium_pct:+.2f}%"
            )

    if intel.long_data and intel.long_data.rate_history:
        hist = "→".join(f"{r:.3f}%" for r in intel.long_data.rate_history)
        lines.append(
            f"{intel.trend_icon} Тренд: {hist} {intel.trend_signal}"
        )
        if intel.hours_to_zero < 999:
            lines.append(f"   └ До нуля: ~{intel.hours_to_zero:.0f}ч")

    if intel.predictions:
        pred_str = " | ".join(
            f"{p['rate']:+.3f}%{p['icon']}" for p in intel.predictions
        )
        lines.append(f"🔮 Прогноз: {pred_str}")

    if intel.limit_arb:
        lines.append(
            f"🎯 Потенциал Limit Arb: ~{intel.limit_potential:.3f}%"
        )

    lines.append(intel.fast_slow)
    lines.append(intel.recommendation)

    return "\n".join(lines)
