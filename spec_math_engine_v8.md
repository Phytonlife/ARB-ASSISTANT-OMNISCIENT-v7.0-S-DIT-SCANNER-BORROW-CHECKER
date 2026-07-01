# ═══════════════════════════════════════════════════════════════════════
# hunter/math_engine_v8.py
# ARB ASSISTANT v8.0 — ПОЛНАЯ МАТЕМАТИКА
#
# Что нового vs v7:
#   ✅ 25 бирж (BingX, Blofin, CoinEx, Tapbit, Backpack, Paradex, BitUnix...)
#   ✅ Cycle Transition Predictor (когда рампа, переход на 1ч)
#   ✅ Spot-Perp scanner (LONG спот + SHORT перп)
#   ✅ Margin Short + Perp Long (берём в долг на маржинальном споте)
#   ✅ Index vs Mark premium live
#   ✅ Rate history trend + прогноз
# ═══════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field
from typing import Optional
from loguru import logger


# ═══════════════════════════════════════════════════════════════════════
# ПОЛНАЯ БАЗА БИРЖ — 25 штук
# ═══════════════════════════════════════════════════════════════════════

EXCHANGES: dict[str, dict] = {

    # ── TIER-1: надёжные, must-have ──────────────────────────────────

    "binance": {
        "tier": 1,
        "perp_t":   0.05,    # % taker fee перп
        "spot_t":   0.10,    # % taker fee спот
        "wd_usd":   1.00,    # фиксированная комиссия вывода USDT
        "funding_cap": 0.75, # максимальный |rate| за период
        "lag_hours": 1.0,    # скорость реакции на изменение premium (ч)
        "role":     "both",  # long / short / both
        "ok":       True,
        "margin":   True,    # маржинальный спот доступен
        "borrow_rate_token": 0.050,  # %/час занять токен
        "borrow_rate_usdt":  0.020,  # %/час занять USDT
        "cycle_rules": {
            # rate% -> переход на N-часовой цикл
            0.50: 4,
            1.50: 2,
            2.00: 1,
        },
        "note": "Инертен при rate < 3%, рампа при > 1.5%",
    },

    "bybit": {
        "tier": 1,
        "perp_t":   0.055,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 4.00,
        "lag_hours": 0.5,    # быстрый
        "role":     "short",
        "ok":       True,
        "margin":   True,
        "borrow_rate_token": 0.080,
        "borrow_rate_usdt":  0.040,
        "cycle_rules": {2.0: 4, 3.0: 2, 4.0: 1},
        "note": "Лучшая шорт-нога. Unified Account удобен",
    },

    "okx": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.10,
        "wd_usd":   0.50,    # дешевле вывод!
        "funding_cap": 0.50, # жёсткий лимит — быстро у предела
        "lag_hours": 1.5,
        "role":     "both",
        "ok":       True,
        "margin":   True,
        "borrow_rate_token": 0.060,
        "borrow_rate_usdt":  0.030,
        "cycle_rules": {0.35: 4, 0.45: 2, 0.50: 1},
        "note": "Лимит 0.5% — часто у предела, лимит-арб сигнал",
    },

    "gate": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 3.00,
        "lag_hours": 4.0,    # МЕДЛЕННЫЙ — инертный
        "role":     "long",
        "ok":       True,
        "margin":   True,
        "borrow_rate_token": 0.080,
        "borrow_rate_usdt":  0.030,
        "cycle_rules": {1.0: 4, 2.0: 2, 3.0: 1},
        "note": "Инертный Gate — лучшая лонг-нога. ADL риск при расходе фонда",
    },

    "bitget": {
        "tier": 1,
        "perp_t":   0.06,
        "spot_t":   0.10,
        "wd_usd":   0.80,
        "funding_cap": 1.50,
        "lag_hours": 1.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.2: 2, 1.5: 1},
        "note": "Быстрая шорт-нога",
    },

    "kucoin": {
        "tier": 1,
        "perp_t":   0.06,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 3.00,
        "lag_hours": 3.0,
        "role":     "long",
        "ok":       True,
        "margin":   True,
        "borrow_rate_token": 0.070,
        "borrow_rate_usdt":  0.025,
        "cycle_rules": {1.0: 4, 2.0: 2, 3.0: 1},
        "note": "Переходит через 20мин от отсечки — поздний",
    },

    "bingx": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 1.5,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 1.5: 2, 2.0: 1},
        "note": "Tier-1, хорошая ликвидность",
    },

    "blofin": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.00,    # нет спот секции
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 1.5,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 1.5: 2, 2.0: 1},
        "note": "Только перп, хорошая шорт-нога",
    },

    "coinex": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 1.50,
        "lag_hours": 2.0,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.2: 2, 1.5: 1},
        "note": "РАМПА: снимает раз в 8ч с шорта при расходе. "
                "Переход 8ч→1ч при rate≈1.5% — входить заранее!",
    },

    "hyperliquid": {
        "tier": 1,
        "perp_t":   0.035,   # самый дешёвый taker!
        "spot_t":   0.00,
        "wd_usd":   1.00,
        "funding_cap": 4.00,
        "lag_hours": 0.5,    # DEX — мгновенная реакция
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {4.0: 1},   # всегда 1ч
        "note": "DEX перп, всегда 1ч цикл, самые низкие fees",
    },

    "xt": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 1.5: 2, 2.0: 1},
        "note": "Tier-1, нормальная ликвидность",
    },

    "coinw": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.50,
        "funding_cap": 2.00,
        "lag_hours": 3.0,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Медленная биржа — хорошая лонг-нога",
    },

    "tapbit": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.5,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Медленная, хорошая лонг-нога",
    },

    "bitunix": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Tier-1 шорт-нога",
    },

    "paradex": {
        "tier": 1,
        "perp_t":   0.03,    # дешёвый DEX
        "spot_t":   0.00,
        "wd_usd":   0.50,
        "funding_cap": 4.00,
        "lag_hours": 0.5,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {4.0: 1},
        "note": "DEX StarkNet. Первые мин листинга: спред 5-10%. 1ч цикл.",
    },

    "backpack": {
        "tier": 1,
        "perp_t":   0.04,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 3.00,
        "lag_hours": 1.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.5: 4, 2.5: 1},
        "note": "Solana DEX. 1ч цикл для SOL-токенов",
    },

    "pionex": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 1.50,
        "lag_hours": 2.0,
        "role":     "both",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.5: 1},
        "note": "Tier-1, grid-bot биржа",
    },

    # ── TIER-2: есть риски ────────────────────────────────────────────

    "mexc": {
        "tier": 2,
        "perp_t":   0.01,    # 0% maker, 0.01% taker — дешевейший!
        "spot_t":   0.00,    # spot БЕСПЛАТНО
        "wd_usd":   1.00,
        "funding_cap": 1.50,
        "lag_hours": 1.5,
        "role":     "both",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.5: 1},
        "note": "Tier-2 но 0% спот! Лучшая для спот-лонг ноги",
    },

    "weex": {
        "tier": 2,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Tier-2, нормальная ликвидность",
    },

    "phemex": {
        "tier": 2,
        "perp_t":   0.06,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Tier-2",
    },

    "poloniex": {
        "tier": 2,
        "perp_t":   0.05,
        "spot_t":   0.15,
        "wd_usd":   1.50,
        "funding_cap": 1.50,
        "lag_hours": 3.0,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.5: 1},
        "note": "Tier-2 медленная лонг-нога",
    },

    "deepcoin": {
        "tier": 2,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 3.0,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Tier-2 медленная",
    },

    "bitmart": {
        "tier": 2,
        "perp_t":   0.06,
        "spot_t":   0.25,
        "wd_usd":   1.50,
        "funding_cap": 1.50,
        "lag_hours": 3.5,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.5: 1},
        "note": "Tier-2, медленная, высокие спот-fees",
    },

    # ── BLACKLIST ─────────────────────────────────────────────────────
    "ourbit": {
        "ok": False,
        "note": "BLACKLIST: тонкий стакан, нельзя выйти",
    },
    "htx": {
        "ok": False,
        "note": "BLACKLIST: берёт лимитки по рынку, непредсказуем",
    },
}

BLACKLIST = {ex for ex, cfg in EXCHANGES.items() if not cfg.get("ok", True)}


# ═══════════════════════════════════════════════════════════════════════
# ИСПРАВЛЕННАЯ ФОРМУЛА КОМИССИЙ (с 3 типами связок)
# ═══════════════════════════════════════════════════════════════════════

def calc_net_spread(
    gross_pct:       float,
    ex_a:            str,
    ex_b:            str,
    size_usd:        float,
    leg_type_a:      str = "perp",    # "perp" / "spot"
    leg_type_b:      str = "perp",
    has_withdrawal:  bool = False,
    hold_hours:      float = 0.0,     # для margin borrow fee
    is_margin_short: bool = False,    # нога B — margin short
) -> dict:
    """
    Универсальный расчёт чистого спреда для всех 3 типов связок.

    ТИПЫ СВЯЗОК:
      perp-perp:   leg_type_a="perp", leg_type_b="perp", has_withdrawal=False
      spot-perp:   leg_type_a="spot", leg_type_b="perp", has_withdrawal=True
      margin-perp: leg_type_a="perp", leg_type_b="spot", is_margin_short=True

    КЛЮЧЕВОЕ:
      has_withdrawal=True → wd_pct = wd_usd / size_usd * 100
      При $50: wd_pct = $1/$50*100 = 2.0% (!!! не 0.02%)
    """
    # Guard: size_usd > 0
    if size_usd <= 0:
        return {"error": f"size_usd должен быть > 0, получили {size_usd}"}

    # Blacklist проверка — ПЕРВОЙ
    if ex_a.lower() in BLACKLIST:
        return {"error": f"{ex_a}: BLACKLIST — {EXCHANGES[ex_a.lower()]['note']}"}
    if ex_b.lower() in BLACKLIST:
        return {"error": f"{ex_b}: BLACKLIST — {EXCHANGES[ex_b.lower()]['note']}"}

    cfg_a = EXCHANGES.get(ex_a.lower())
    cfg_b = EXCHANGES.get(ex_b.lower())

    if not cfg_a or not cfg_a.get("ok"):
        return {"error": f"{ex_a}: не поддерживается"}
    if not cfg_b or not cfg_b.get("ok"):
        return {"error": f"{ex_b}: не поддерживается"}

    fee_a = cfg_a.get(f"{leg_type_a}_t", 0.10)
    fee_b = cfg_b.get(f"{leg_type_b}_t", 0.05)

    # Withdraw: фиксированный USD → процент от позиции
    wd_pct = (cfg_a.get("wd_usd", 1.0) / size_usd * 100) if has_withdrawal else 0.0

    # Margin borrow fee (для margin short ноги)
    borrow_pct = 0.0
    if is_margin_short and hold_hours > 0:
        borrow_rate = cfg_b.get("borrow_rate_token", 0.08)
        borrow_pct  = borrow_rate * hold_hours

    total = round(fee_a + fee_b + wd_pct + borrow_pct, 5)
    net   = round(gross_pct - total, 5)

    return {
        "gross":      gross_pct,
        "fee_a":      fee_a,
        "fee_b":      fee_b,
        "wd_pct":     round(wd_pct, 4),
        "borrow_pct": round(borrow_pct, 4),
        "total":      total,
        "net":        net,
        "ok":         net > 0.10,
        "good":       net > 0.30,
        "tier_a":     cfg_a.get("tier", 2),
        "tier_b":     cfg_b.get("tier", 2),
    }


# ═══════════════════════════════════════════════════════════════════════
# CYCLE TRANSITION PREDICTOR
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class CycleTransitionSignal:
    exchange:         str
    current_rate:     float
    funding_cap:      float
    cap_pct:          float        # % использования лимита
    current_cycle_h:  int          # текущий цикл в часах
    next_cycle_h:     Optional[int]   # следующий цикл (None если нет)
    probability:      str          # LOW / MEDIUM / HIGH / RAMP_ACTIVE
    payments_to_limit: float       # выплат до лимита
    hours_to_limit:   float
    action:           str          # что делать
    entry_window:     str          # когда входить
    daily_yield_now:  float        # % в сутки при текущем цикле
    daily_yield_after: float       # % в сутки после перехода


def predict_cycle_transition(
    exchange:        str,
    current_rate:    float,
    rate_history:    list[float],  # [старый → новый], последние 4
    current_cycle_h: int = 8,
) -> CycleTransitionSignal:
    """
    Предсказывает переход на ускоренный цикл фандинга.

    Тактика из arb_guide:
      CoinEx лимит -1.5% → рампа → переход на 1ч
      Входить за 2 выплаты ДО перехода, фармить каждый час
    """
    cfg = EXCHANGES.get(exchange.lower(), {})
    cap  = cfg.get("funding_cap", 0.75)
    rules = cfg.get("cycle_rules", {})
    abs_rate = abs(current_rate)
    cap_pct  = abs_rate / cap * 100 if cap > 0 else 0

    # Тренд из истории
    avg_delta = 0.0
    converging = False
    if len(rate_history) >= 2:
        deltas = [rate_history[i] - rate_history[i-1]
                  for i in range(1, len(rate_history))]
        avg_delta = sum(deltas) / len(deltas)
        diverging_to_cap = (
            (avg_delta < 0 and current_rate < 0) or
            (avg_delta > 0 and current_rate > 0)
        )
        converging = not diverging_to_cap
    else:
        diverging_to_cap = False

    # Через сколько выплат достигнет лимита
    if abs(avg_delta) > 0.001 and not converging:
        payments_to_limit = (cap - abs_rate) / abs(avg_delta)
        hours_to_limit    = payments_to_limit * current_cycle_h
    else:
        payments_to_limit = 999
        hours_to_limit    = 999

    # Следующий порог цикла
    next_cycle_h = None
    thresholds = sorted(rules.keys())
    for threshold in thresholds:
        if abs_rate >= threshold * 0.90:  # 90% от порога
            next_cycle_h = rules[threshold]

    # Вероятность перехода
    if cap_pct >= 90:
        prob = "🔴 РАМПА АКТИВНА"
        action = (
            f"ВХОДИТЬ НЕМЕДЛЕННО!\n"
            f"  LONG {exchange.upper()} (медленная) + SHORT быстрая биржа\n"
            f"  Цикл сменится — каждый час будет выплата"
        )
        entry_window = "⚡ ПРЯМО СЕЙЧАС"
    elif cap_pct >= 75 and not converging:
        prob = "🟡 ВЫСОКАЯ"
        action = (
            f"Готовься к входу.\n"
            f"  Переход через ~{hours_to_limit:.0f}ч ({payments_to_limit:.1f} выплат)\n"
            f"  Следи каждые 30 минут"
        )
        entry_window = f"За {min(2, int(payments_to_limit)+1)} выплаты ДО перехода"
    elif cap_pct >= 50 and not converging:
        prob = "🟡 УМЕРЕННАЯ"
        action = "Добавь в watchlist. Следи за трендом rate."
        entry_window = f"За 2 выплаты ДО достижения 90% лимита"
    elif converging:
        prob = "⚪ НЕТ (rate сходится)"
        action = "Не входить — rate идёт к нулю, не к лимиту"
        entry_window = "Ждать разворота"
    else:
        prob = "⚪ НИЗКАЯ"
        action = "Обычная ситуация, нет рампы"
        entry_window = "Ждать"

    # Доходность
    perp_fee = cfg.get("perp_t", 0.05)
    daily_now   = abs_rate * (24 / current_cycle_h) - perp_fee * 6
    daily_after = abs_rate * 24 - perp_fee * 24  # если 1ч цикл
    # (при 1ч цикле платишь fee 24 раза — это считать тоже нужно)

    return CycleTransitionSignal(
        exchange         = exchange,
        current_rate     = current_rate,
        funding_cap      = cap,
        cap_pct          = round(cap_pct, 1),
        current_cycle_h  = current_cycle_h,
        next_cycle_h     = next_cycle_h,
        probability      = prob,
        payments_to_limit= round(payments_to_limit, 1),
        hours_to_limit   = round(hours_to_limit, 1),
        action           = action,
        entry_window     = entry_window,
        daily_yield_now  = round(daily_now, 4),
        daily_yield_after= round(daily_after, 4),
    )


def format_cycle_alert(sig: CycleTransitionSignal) -> str:
    """Форматирует сигнал рампы для Telegram."""
    next_str = f"→ {sig.next_cycle_h}ч" if sig.next_cycle_h else ""
    return (
        f"🔄 ЦИКЛ [{sig.exchange.upper()}]: "
        f"{sig.current_cycle_h}ч {next_str}\n"
        f"   Rate: {sig.current_rate:+.4f}% | "
        f"Лимит: ±{sig.funding_cap:.2f}% | "
        f"Использовано: {sig.cap_pct:.0f}%\n"
        f"   Вероятность рампы: {sig.probability}\n"
        f"   {sig.action}\n"
        f"   Вход: {sig.entry_window}"
    )


# ═══════════════════════════════════════════════════════════════════════
# SPOT-PERP SCANNER
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class SpotPerpSignal:
    """LONG спот + SHORT перп, или LONG перп + SHORT margin."""
    symbol:       str
    spot_ex:      str   # биржа для спот ноги
    perp_ex:      str   # биржа для перп ноги
    spot_price:   float
    perp_price:   float
    premium_pct:  float  # (perp - spot) / spot * 100
    # Позитивный = фьюч дороже → LONG спот + SHORT перп
    # Негативный = фьюч дешевле → LONG перп + SHORT margin спот
    strategy:     str   # "SPOT_LONG_PERP_SHORT" / "MARGIN_SHORT_PERP_LONG"
    size_usd:     float
    net_spread:   float
    hold_hours:   float
    ok:           bool


def calc_spot_perp_signal(
    symbol:        str,
    spot_ex:       str,
    perp_ex:       str,
    spot_price:    float,
    perp_price:    float,
    size_usd:      float = 50.0,
    hold_hours:    float = 24.0,
) -> SpotPerpSignal:
    """
    Рассчитывает spot-perp сигнал.

    Два направления:
    1. perp > spot (premium+): LONG спот + SHORT перп
       Зарабатываем на схождении + funding если perp дороже
    2. perp < spot (premium-): LONG перп + SHORT margin спот
       Зарабатываем на схождении + funding если spot дороже
    """
    if perp_price <= 0 or spot_price <= 0:
        return SpotPerpSignal(symbol, spot_ex, perp_ex, spot_price,
                              perp_price, 0, "UNKNOWN", size_usd, 0, 0, False)

    premium_pct = (perp_price - spot_price) / spot_price * 100

    cfg_spot = EXCHANGES.get(spot_ex.lower(), {})
    cfg_perp = EXCHANGES.get(perp_ex.lower(), {})
    margin_available = cfg_spot.get("margin", False)

    if premium_pct > 0:
        # LONG спот + SHORT перп (классика)
        strategy = "SPOT_LONG_PERP_SHORT"
        r = calc_net_spread(
            gross_pct      = premium_pct,
            ex_a           = spot_ex,
            ex_b           = perp_ex,
            size_usd       = size_usd,
            leg_type_a     = "spot",
            leg_type_b     = "perp",
            has_withdrawal = True,
        )
    elif premium_pct < -2.3 and margin_available:
        # LONG перп + SHORT margin спот (нужна маржа)
        strategy = "MARGIN_SHORT_PERP_LONG"
        gross = abs(premium_pct)
        r = calc_net_spread(
            gross_pct       = gross,
            ex_a            = perp_ex,
            ex_b            = spot_ex,
            size_usd        = size_usd,
            leg_type_a      = "perp",
            leg_type_b      = "spot",
            has_withdrawal  = False,
            hold_hours      = hold_hours,
            is_margin_short = True,
        )
    else:
        return SpotPerpSignal(symbol, spot_ex, perp_ex, spot_price,
                              perp_price, premium_pct, "INSUFFICIENT",
                              size_usd, 0, hold_hours, False)

    if "error" in r:
        return SpotPerpSignal(symbol, spot_ex, perp_ex, spot_price,
                              perp_price, premium_pct, f"ERROR:{r['error']}",
                              size_usd, 0, hold_hours, False)

    return SpotPerpSignal(
        symbol      = symbol,
        spot_ex     = spot_ex,
        perp_ex     = perp_ex,
        spot_price  = spot_price,
        perp_price  = perp_price,
        premium_pct = round(premium_pct, 4),
        strategy    = strategy,
        size_usd    = size_usd,
        net_spread  = r["net"],
        hold_hours  = hold_hours,
        ok          = r.get("ok", False),
    )


def format_spot_perp_alert(sig: SpotPerpSignal) -> str:
    """Telegram алерт для spot-perp сигнала."""
    if sig.strategy == "SPOT_LONG_PERP_SHORT":
        legs = f"LONG спот {sig.spot_ex.upper()} + SHORT перп {sig.perp_ex.upper()}"
        explain = "Фьюч дороже спота → купи дешевле на споте, продай дорого на фьюче"
    elif sig.strategy == "MARGIN_SHORT_PERP_LONG":
        legs = f"SHORT margin {sig.spot_ex.upper()} + LONG перп {sig.perp_ex.upper()}"
        explain = "Фьюч дешевле спота → займи токен, продай на споте, откупи на фьюче"
    else:
        return f"⚠️ {sig.symbol}: {sig.strategy}"

    status = "✅" if sig.net_spread > 0.30 else ("⚠️" if sig.ok else "❌")

    return (
        f"{status} SPOT-PERP [{sig.symbol}]\n"
        f"{'─'*35}\n"
        f"Спот {sig.spot_ex.upper()}: ${sig.spot_price:.4f}\n"
        f"Перп {sig.perp_ex.upper()}: ${sig.perp_price:.4f}\n"
        f"Premium: {sig.premium_pct:+.3f}%\n\n"
        f"Стратегия: {legs}\n"
        f"Net спред: {sig.net_spread:.4f}%\n"
        f"{explain}"
    )


# ═══════════════════════════════════════════════════════════════════════
# OFI + SLIPPAGE (без изменений, оставляем как в v7)
# ═══════════════════════════════════════════════════════════════════════

def calc_ofi(bids: list, asks: list, depth: int = 5) -> float:
    """Order Flow Imbalance: +0.3=накопление, -0.3=распродажа."""
    bv = sum(b[1] for b in bids[:depth])
    av = sum(a[1] for a in asks[:depth])
    t  = bv + av
    return round((bv - av) / t, 3) if t > 0 else 0.0


def estimate_slippage(position_usd: float, depth_usd: float) -> float:
    """
    Оценка проскальзывания в ПРОЦЕНТАХ (%) для market ордера.

    Формула: (position / depth) × 50
    Калибровка по реальным данным:
      SAHARA Gate: $50 / $2156 × 50 = 1.160%  ← тонкий стакан ❌
      ORCA Binance: $50 / $180000 × 50 = 0.014% ← нормально ✅

    Порог WARN:  > 0.30%
    Порог BLOCK: > 0.50%
    """
    if depth_usd <= 0:
        return 99.0
    return round((position_usd / depth_usd) * 50, 4)


def calc_min_diff(
    ex_a: str,
    ex_b: str,
    size_usd: float = 50.0,
    leg_type_a: str = "perp",
    leg_type_b: str = "perp",
    has_withdrawal: bool = False,
    min_net: float = 0.10,
) -> float:
    """
    Динамически рассчитывает минимальный gross% для окупаемости.
    Вместо статичного THRESHOLDS — учитывает конкретные биржи.

    Примеры:
      okx/binance  perp-perp $50:       min = 0.20%
      mexc/bybit   spot-perp $50:       min = 2.155%
      mexc/bybit   spot-perp $100:      min = 1.155%
      gate/binance spot-perp $50:       min = 2.355%
    """
    cfg_a = EXCHANGES.get(ex_a.lower(), {})
    cfg_b = EXCHANGES.get(ex_b.lower(), {})
    fee_a = cfg_a.get(f"{leg_type_a}_t", 0.10)
    fee_b = cfg_b.get(f"{leg_type_b}_t", 0.05)
    wd    = (cfg_a.get("wd_usd", 1.0) / size_usd * 100) if has_withdrawal else 0.0
    return round(fee_a + fee_b + wd + min_net, 4)


# ═══════════════════════════════════════════════════════════════════════
# QUICK-REFERENCE: минимальные пороги
# ═══════════════════════════════════════════════════════════════════════

THRESHOLDS = {
    # Используй calc_min_diff() для динамического расчёта под конкретную пару!
    # Эти значения — ориентиры для средних бирж (perp_t ~0.05%, spot_t ~0.10%)
    "perp_perp": {
        "min_diff":   0.20,   # gross > 0.20% для net > 0.10% (средние fees 0.10%)
        "good_diff":  0.30,
        "great_diff": 0.50,
    },
    "spot_perp_50": {
        "min_gross":  2.155,  # gross > 2.155% для net > 0.10% при $50 (fees ~2.055%)
        "good_gross": 3.0,
    },
    "spot_perp_100": {
        "min_gross":  1.155,  # gross > 1.155% при $100 (fees ~1.055%)
        "good_gross": 2.0,
    },
    "margin_short": {
        "min_premium": 2.3,   # premium > 2.3% для покрытия borrow+fees за 24ч
        "good_premium": 3.5,
    },
    "cycle_ramp_entry": {
        "cap_pct_watch":  75,  # % от лимита — начинаем мониторить
        "cap_pct_enter":  90,  # % от лимита — входим
    },
    "slippage": {
        "warn":  0.30,         # % — предупреждение
        "block": 0.50,         # % — блокировать вход
    },
}
