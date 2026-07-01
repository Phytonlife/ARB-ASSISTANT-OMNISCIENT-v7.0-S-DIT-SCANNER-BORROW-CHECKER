# hunter/math_engine.py
# ARB ASSISTANT v8.0 — ПОЛНАЯ МАТЕМАТИКА

from dataclasses import dataclass
from typing import Optional
from loguru import logger
from data.fees import EXCHANGES, BLACKLIST


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
        return {"error": f"size_usd должен быть > 0, получили {size_usd}", "ok": False}

    # Blacklist проверка — ПЕРВОЙ
    if ex_a.lower() in BLACKLIST:
        note = EXCHANGES.get(ex_a.lower(), {}).get("note", "BLACKLIST")
        return {"error": f"{ex_a}: {note}", "ok": False}
    if ex_b.lower() in BLACKLIST:
        note = EXCHANGES.get(ex_b.lower(), {}).get("note", "BLACKLIST")
        return {"error": f"{ex_b}: {note}", "ok": False}

    cfg_a = EXCHANGES.get(ex_a.lower())
    cfg_b = EXCHANGES.get(ex_b.lower())

    if not cfg_a or not cfg_a.get("ok"):
        return {"error": f"{ex_a}: не поддерживается", "ok": False}
    if not cfg_b or not cfg_b.get("ok"):
        return {"error": f"{ex_b}: не поддерживается", "ok": False}

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
    """
    if perp_price <= 0 or spot_price <= 0:
        return SpotPerpSignal(symbol, spot_ex, perp_ex, spot_price,
                              perp_price, 0, "UNKNOWN", size_usd, 0, 0, False)

    premium_pct = (perp_price - spot_price) / spot_price * 100

    cfg_spot = EXCHANGES.get(spot_ex.lower(), {})
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
# OFI + SLIPPAGE
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
    """
    cfg_a = EXCHANGES.get(ex_a.lower(), {})
    cfg_b = EXCHANGES.get(ex_b.lower(), {})
    fee_a = cfg_a.get(f"{leg_type_a}_t", 0.10)
    fee_b = cfg_b.get(f"{leg_type_b}_t", 0.05)
    wd    = (cfg_a.get("wd_usd", 1.0) / size_usd * 100) if has_withdrawal else 0.0
    return round(fee_a + fee_b + wd + min_net, 4)


def score_signal(diff: float, net: float, ofi_long: float,
                 slip_a: float, slip_b: float) -> int:
    """Простой скоринг сигнала 0-10."""
    score = 0
    if diff >= 0.50:
        score += 3
    elif diff >= 0.30:
        score += 2
    else:
        score += 1
    if net >= 0.30:
        score += 3
    elif net >= 0.10:
        score += 2
    if ofi_long > 0.3:
        score += 2
    if slip_a < 0.3 and slip_b < 0.3:
        score += 2
    return min(score, 10)


def check_ticker_trap(diff_pct: float) -> dict:
    """
    Проверка ловушки разных тикеров.
    """
    if diff_pct > 40.0:
        return {
            "trap": True,
            "warning": f"⚠️ ТИКЕР-ЛОВУШКА: diff {diff_pct:.1f}% > 40% — "
                       f"ПРОВЕРЬ ТИКЕР КОНТРАКТА ВРУЧНУЮ на обеих биржах!"
        }
    return {"trap": False, "warning": ""}


# ═══════════════════════════════════════════════════════════════════════
# QUICK-REFERENCE: минимальные пороги
# ═══════════════════════════════════════════════════════════════════════

THRESHOLDS = {
    "perp_perp": {
        "min_diff":   0.20,
        "good_diff":  0.30,
        "great_diff": 0.50,
    },
    "spot_perp_50": {
        "min_gross":  2.155,
        "good_gross": 3.0,
    },
    "spot_perp_100": {
        "min_gross":  1.155,
        "good_gross": 2.0,
    },
    "margin_short": {
        "min_premium": 2.3,
        "good_premium": 3.5,
    },
    "cycle_ramp_entry": {
        "cap_pct_watch":  75,
        "cap_pct_enter":  90,
    },
    "slippage": {
        "warn":  0.30,
        "block": 0.50,
    },
}
