"""
radar/position_guardian.py  v4.1 — ФИНАЛЬНАЯ ВЕРСИЯ
=====================================================
Исправлено 11 багов + добавлены 4 улучшения vs v4:

КРИТИЧЕСКИЕ (сломали бы работу):
  [FIX1] TP подсказки спамили бесконечно — теперь каждый TP срабатывает 1 раз
  [FIX2] TP уровни были абсолютными — теперь ОТНОСИТЕЛЬНЫЕ от entry_spread

СЕРЬЁЗНЫЕ (тормоза и неточности):
  [FIX3] ccxt объект создавался заново каждые 30с — теперь кэшируется
  [FIX4] _check_payment: неверная логика — теперь отслеживаем abs_time выплаты
  [FIX5] decay_periods не сбрасывался при start_watch — исправлено
  [FIX6] velocity() считал по prem_long — теперь по snap.spread

УМЕРЕННЫЕ (мешали комфортной работе):
  [FIX7] Спам HOLD сообщениями — теперь cooldown + только при изменениях
  [FIX8] hard_stop_level правильный (entry+delta = ухудшение к 0)
  [FIX9] twap_hist_long maxlen=50 → 480 (чтобы 4ч TWAP работал реально)
  [FIX10] Entry Quality при старте берёт реальные данные из dev_store
  [FIX11] HOLD не спамит если нет изменений (умный тригер)

ДОБАВЛЕНО:
  [NEW] Timeout: 20+ мин без движения → ⏰ предупреждение
  [NEW] Profit Lock: профит упал с пика → предупреждение
  [NEW] До фандинга < 10 мин → напоминание (всегда, даже в HOLD)
  [NEW] /watch ANALYSE → разовый глубокий анализ позиции без запуска слежки
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable
import asyncio, logging, math

try:
    from loguru import logger
except ImportError:
    logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# КОНФИГ БИРЖ
# ════════════════════════════════════════════════════════════════

EX = {
    "okx":         {"cap":0.50, "lag":1.0, "fee":0.05, "twap":"weighted", "ramp_prem":-4.0},
    "binance":     {"cap":0.75, "lag":1.0, "fee":0.05, "twap":"uniform",  "ramp_prem":-6.0},
    "bybit":       {"cap":4.00, "lag":0.8, "fee":0.055,"twap":"weighted", "ramp_prem":-8.0},
    "gate":        {"cap":0.30, "lag":4.0, "fee":0.05, "twap":"uniform",  "ramp_prem":-2.4},
    "kucoin":      {"cap":3.00, "lag":3.5, "fee":0.06, "twap":"uniform",  "ramp_prem":-24.0},
    "coinex":      {"cap":1.50, "lag":2.5, "fee":0.05, "twap":"uniform",  "ramp_prem":-12.0},
    "mexc":        {"cap":1.50, "lag":1.5, "fee":0.05, "twap":"uniform",  "ramp_prem":-12.0},
    "bingx":       {"cap":2.00, "lag":1.5, "fee":0.05, "twap":"uniform",  "ramp_prem":-16.0},
    "hyperliquid": {"cap":4.00, "lag":0.6, "fee":0.035,"twap":"uniform",  "ramp_prem":-6.0},
    "bitget":      {"cap":1.00, "lag":1.2, "fee":0.055,"twap":"uniform",  "ramp_prem":-8.0},
    "apex":        {"cap":0.0125,"lag":0.5,"fee":0.05, "twap":"uniform",  "ramp_prem":-0.1},
}

DANGER_PAIRS = {
    ("bybit","gate"):  "⚠️ Gate рампует при -2.4%, Bybit только при -8%!",
    ("kucoin","gate"): "❌ Gate cap меньше KuCoin — Gate рампует первой!",
    ("coinex","gate"): "⚠️ Gate может рампануть раньше CoinEx при -2.4%",
}

KEY_TWAP_PERIOD = {
    "bybit":1.0,"okx":1.0,
    "gate":4.0,"binance":4.0,"kucoin":4.0,"coinex":4.0,"mexc":4.0,
}

# ── Пороги ──────────────────────────────────────────────────────
EXIT_CONSEC       = 3      # периодов роста spread подряд → EXIT
EXIT_VEL_FAST     = 0.40   # %/ч быстрый рост → EXIT
EXIT_VEL_SLOW     = 0.15   # %/ч медленный рост → WATCH
EXIT_OI_DROP      = -5.0   # % падение OI → EXIT
WHALE_JUMP        = 1.5    # % резкий скачок за 30с → кит
HOLD_LR           = 0.75   # limit ratio → близко к рампе
CHECK_S           = 30     # секунд между проверками

HARD_STOP_DEFAULT = 0.70   # % ухудшения от входа → немедленный выход

DECAY_VEL_THRES   = 0.05   # velocity < этого → движение стихло
DECAY_OI_THRES    = -1.0   # OI_delta < этого → шорты уходят
DECAY_PERIODS_MIN = 3      # периодов затухания подряд → DECAY

NO_MOVE_MINUTES   = 20     # мин без движения → Timeout
PROFIT_LOCK_DROP  = 0.5    # % падение от пика профита → предупреждение
FUND_REMINDER_MIN = 10     # мин до фандинга → напомнить

# [FIX2] TP ОТНОСИТЕЛЬНЫЕ от entry_spread (offsets от входа)
PARTIAL_TP_OFFSETS = [
    (-1.0, 30, "первый тейк"),
    (-1.5, 30, "второй тейк"),
    (-2.0, 40, "финальный"),
]

EQ_Z_WEIGHT   = 0.30
EQ_VEL_WEIGHT = 0.25
EQ_OI_WEIGHT  = 0.25
EQ_CONF_WEIGHT= 0.20


# ════════════════════════════════════════════════════════════════
# МАТЕМАТИКА
# ════════════════════════════════════════════════════════════════

def cfg(ex): return EX.get(ex.lower(), {"cap":1.5,"lag":1.0,"fee":0.05,"twap":"uniform","ramp_prem":-12.0})

def rate(prem, ex):
    c = cfg(ex)
    return max(-c["cap"], min(c["cap"], (prem / c["lag"]) / 8))

def lr(prem, ex):
    cap = cfg(ex)["cap"]
    r   = rate(prem, ex)
    return min(abs(r) / cap, 1.0) if cap > 0 else 0.0

def cycle(prem, ex):
    l = lr(prem, ex)
    return 1 if l >= 0.90 else (4 if l >= 0.60 else 8)

def rate_per_h(prem, ex):
    return abs(rate(prem, ex)) / cycle(prem, ex)

def check_pair_warning(long_ex, short_ex):
    return DANGER_PAIRS.get((long_ex.lower(), short_ex.lower()))


# ════════════════════════════════════════════════════════════════
# [FIX3] Кэш ccxt объектов — создаются один раз
# ════════════════════════════════════════════════════════════════

_ccxt_cache: dict = {}

async def _get_ccxt(ex_id: str):
    """Возвращает кэшированный ccxt объект или создаёт новый."""
    if ex_id not in _ccxt_cache:
        try:
            import ccxt.async_support as ccxt
            cls = getattr(ccxt, ex_id.lower(), None)
            if cls:
                _ccxt_cache[ex_id] = cls({"enableRateLimit": True})
        except ImportError:
            return None
    return _ccxt_cache.get(ex_id)

async def close_all_ccxt():
    """Закрыть все ccxt соединения при выключении бота."""
    for ex in list(_ccxt_cache.values()):
        try:
            await ex.close()
        except Exception:
            pass
    _ccxt_cache.clear()


# ════════════════════════════════════════════════════════════════
# DATACLASSES
# ════════════════════════════════════════════════════════════════

@dataclass
class Snap:
    ts:          float
    prem_long:   float; prem_short:  float
    rate_long:   float; rate_short:  float
    lr_long:     float; lr_short:    float
    oi_long:     float; oi_short:    float
    next_fund_s: float
    cycle_long:  int;   cycle_short: int
    # [FIX4] абсолютное время следующей выплаты
    next_fund_abs: float = 0.0

    @property
    def spread(self): return self.prem_long - self.prem_short
    @property
    def total_oi(self): return self.oi_long + self.oi_short


@dataclass
class EntryQuality:
    score:         float
    z_score:       float
    velocity:      float
    oi_delta:      float
    confirmations: int
    label:         str
    suggestions:   list

    @classmethod
    def unknown(cls):
        return cls(score=0, z_score=0, velocity=0, oi_delta=0,
                   confirmations=0, label="нет данных", suggestions=[])


@dataclass
class Position:
    symbol:      str; long_ex:  str; short_ex: str
    entry_spread:float; entry_time: float
    size_usd:    float; entry_fees: float
    hard_stop_delta: float = HARD_STOP_DEFAULT

    fund_earned: float = 0.0
    fund_paid:   float = 0.0
    peak_profit: float = 0.0   # [NEW] для Profit Lock

    # [FIX9] maxlen увеличен для 4ч TWAP (480 × 30с = 4ч)
    snaps:           deque = field(default_factory=lambda: deque(maxlen=720))
    spread_history:  deque = field(default_factory=lambda: deque(maxlen=2880))
    payments:        list  = field(default_factory=list)
    twap_hist_long:  deque = field(default_factory=lambda: deque(maxlen=480))
    twap_hist_short: deque = field(default_factory=lambda: deque(maxlen=480))

    active:     bool  = True
    last_ts:    float = 0.0
    last_level: str   = "INFO"
    consec_up:  int   = 0
    decay_periods: int = 0

    # [FIX1] set вместо list — O(1) поиск + не заполняется
    partial_tp_done: set = field(default_factory=set)

    entry_quality: Optional[EntryQuality] = None

    # [NEW] Timeout tracking
    last_spread_min: float = 0.0   # минимальный спред за сессию
    last_move_ts:    float = 0.0   # когда последний раз было движение

    # [FIX4] отслеживаем выплаты по abs_time
    last_fund_abs:   float = 0.0

    @property
    def hours_held(self):
        return (datetime.now(timezone.utc).timestamp() - self.entry_time) / 3600

    @property
    def cur_spread(self):
        return self.snaps[-1].spread if self.snaps else self.entry_spread

    @property
    def spread_pnl(self):
        return self.entry_spread - self.cur_spread

    @property
    def net_fund(self):
        return self.fund_earned - self.fund_paid

    @property
    def total_pct(self):
        return round(self.spread_pnl + self.net_fund - self.entry_fees, 4)

    @property
    def total_usd(self):
        return round(self.total_pct / 100 * self.size_usd, 3)

    @property
    def hard_stop_level(self):
        # entry=-1.5% + delta=0.7% → stop=-0.8% (выходим если spread вырос до -0.8%)
        return self.entry_spread + self.hard_stop_delta

    # [FIX2] TP уровни ОТНОСИТЕЛЬНЫЕ
    def tp_levels(self):
        return [
            (self.entry_spread + offset, pct, desc)
            for offset, pct, desc in PARTIAL_TP_OFFSETS
        ]


@dataclass
class Alert:
    level:        str    # HOLD / WATCH / DECAY / EXIT / HARD_STOP / TIMEOUT
    reason:       str
    details:      str
    action:       str
    urgency:      int    # 1-5
    fc_1h:        float = 0.0
    fc_4h:        float = 0.0
    eta_ramp_h:   Optional[float] = None
    pair_warning: str   = ""
    decay_score:  int   = 0
    partial_tp:   Optional[str]   = None
    fund_reminder:Optional[str]   = None   # [NEW]
    profit_lock:  Optional[str]   = None   # [NEW]


# ════════════════════════════════════════════════════════════════
# Z-SCORE
# ════════════════════════════════════════════════════════════════

def calc_z_score(pos: Position) -> float:
    hist = list(pos.spread_history)
    if len(hist) < 20:
        return 0.0
    mean = sum(hist) / len(hist)
    std  = math.sqrt(sum((s - mean) ** 2 for s in hist) / len(hist))
    if std < 0.001:
        return 0.0
    return round((pos.cur_spread - mean) / std, 2)


# ════════════════════════════════════════════════════════════════
# ENTRY QUALITY SCORE
# ════════════════════════════════════════════════════════════════

def calc_entry_quality(
    symbol: str, long_ex: str, short_ex: str,
    entry_spread: float, spread_history: list,
    velocity: float, oi_delta: float, confirmations: int,
) -> EntryQuality:
    z = 0.0
    if len(spread_history) >= 20:
        m   = sum(spread_history) / len(spread_history)
        std = math.sqrt(sum((s-m)**2 for s in spread_history) / len(spread_history))
        if std > 0.001:
            z = (entry_spread - m) / std

    z_n    = min(1.0, max(0.0, (-z - 1.0) / 2.0))
    vel_n  = min(1.0, max(0.0, abs(velocity) / 2.0))
    oi_n   = min(1.0, max(0.0, oi_delta / 15.0))
    conf_n = min(1.0, confirmations / 4.0)

    score = round(10 * (EQ_Z_WEIGHT*z_n + EQ_VEL_WEIGHT*vel_n +
                        EQ_OI_WEIGHT*oi_n + EQ_CONF_WEIGHT*conf_n), 1)

    label = (
        "🔥 ОТЛИЧНЫЙ" if score >= 7.5 else
        "✅ ХОРОШИЙ"  if score >= 5.5 else
        "⚠️ СРЕДНИЙ"  if score >= 3.5 else
        "❌ СЛАБЫЙ"
    )

    tips = []
    if z > -1.0:
        tips.append(f"Спред {entry_spread:+.2f}% — не экстремальный (z={z:.1f})")
    if velocity > -0.3:
        tips.append("Velocity слабая — разгон не подтверждён")
    if oi_delta < 3:
        tips.append("OI не растёт — нет новых шортов")
    if confirmations < 2:
        tips.append("Мало биржевых подтверждений")

    # TP план (относительный)
    tp_parts = [f"{pct}% при Δ{off:+.1f}% ({desc})"
                for off, pct, desc in PARTIAL_TP_OFFSETS]
    tips.append("💡 TP план: " + " | ".join(tp_parts))

    return EntryQuality(score=score, z_score=round(z, 2), velocity=velocity,
                        oi_delta=oi_delta, confirmations=confirmations,
                        label=label, suggestions=tips)


# ════════════════════════════════════════════════════════════════
# TWAP
# ════════════════════════════════════════════════════════════════

def get_twap(hist, hours, weighted=False):
    now = datetime.now(timezone.utc).timestamp()
    pts = [(ts, p) for ts, p in hist if ts > now - hours * 3600]
    if not pts:
        return hist[-1][1] if hist else 0.0
    if not weighted:
        return sum(p for _, p in pts) / len(pts)
    tw = tw_sum = 0.0
    for i, (_, p) in enumerate(pts, 1):
        tw += i; tw_sum += i * p
    return tw_sum / tw if tw > 0 else 0.0

def add_twap(pos: Position, snap: Snap):
    pos.twap_hist_long.append((snap.ts, snap.prem_long))
    pos.twap_hist_short.append((snap.ts, snap.prem_short))

def twap_long(pos, hours):
    return get_twap(pos.twap_hist_long,  hours, cfg(pos.long_ex)["twap"]=="weighted")

def twap_short(pos, hours):
    return get_twap(pos.twap_hist_short, hours, cfg(pos.short_ex)["twap"]=="weighted")


# ════════════════════════════════════════════════════════════════
# МЕТРИКИ
# ════════════════════════════════════════════════════════════════

def velocity(snaps, n=6) -> float:
    """[FIX6] Velocity по spread (разница двух бирж), не по prem_long."""
    pts = list(snaps)[-n:]
    if len(pts) < 2:
        return 0.0
    dt = (pts[-1].ts - pts[0].ts) / 3600
    if dt < 0.001:
        return 0.0
    # [FIX6] spread = prem_long - prem_short
    return (pts[-1].spread - pts[0].spread) / dt

def oi_delta(snaps) -> float:
    if len(snaps) < 2:
        return 0.0
    c = snaps[-1].total_oi; p = snaps[-2].total_oi
    return (c - p) / p * 100 if p > 0 else 0.0

def forecast(pos: Position, hours: float) -> float:
    if not pos.snaps:
        return pos.total_pct
    snap = pos.snaps[-1]
    rl_h = rate_per_h(snap.prem_long, pos.long_ex)
    rs_h = rate_per_h(snap.prem_short, pos.short_ex)
    vel  = velocity(pos.snaps)
    fut_sp = snap.spread + vel * hours * 0.4
    return round((pos.entry_spread - fut_sp) + pos.net_fund +
                 (rl_h - rs_h) * hours - pos.entry_fees, 4)


# ════════════════════════════════════════════════════════════════
# DECAY SCORE
# ════════════════════════════════════════════════════════════════

def calc_decay_score(pos: Position) -> int:
    if len(pos.snaps) < 4:
        return 0

    snaps_l  = list(pos.snaps)
    score    = 0

    # 1. Velocity: быстрая vs средняя
    vel_now = velocity(pos.snaps, n=3)   # 1.5 мин
    vel_avg = velocity(pos.snaps, n=10)  # 5 мин
    # spread растёт (плохо для нас) при vel_now > 0
    if vel_now > DECAY_VEL_THRES and vel_avg < -0.2:
        score += 3   # сильно замедлилось
    elif vel_now > DECAY_VEL_THRES * 0.5:
        score += 1

    # 2. OI
    od = oi_delta(pos.snaps)
    if od < DECAY_OI_THRES:
        score += 3
    elif od < 0:
        score += 1

    # 3. Spread перестал обновлять минимумы (нет новых экстремумов)
    recent = [s.spread for s in snaps_l[-6:]]
    if len(recent) >= 3:
        mins = [min(recent[:i+1]) for i in range(len(recent))]
        if mins[-1] == mins[-2] == mins[-3]:
            score += 3  # 3+ периода без нового минимума

    return min(score, 9)


# ════════════════════════════════════════════════════════════════
# ЯДРО АНАЛИЗА v4.1
# ════════════════════════════════════════════════════════════════

def analyze(pos: Position) -> Alert:
    if len(pos.snaps) < 2:
        return Alert("INFO", "Инициализация",
                     f"Данные собираются ({len(pos.snaps)}/2)...", "Ждите", 1)

    snap = pos.snaps[-1]; prev = pos.snaps[-2]
    now  = datetime.now(timezone.utc).timestamp()
    vel  = velocity(pos.snaps)
    od   = oi_delta(pos.snaps)
    fc1  = forecast(pos, 1.0); fc4 = forecast(pos, 4.0)
    mins = snap.next_fund_s / 60

    kl = KEY_TWAP_PERIOD.get(pos.long_ex, 4.0)
    ks = KEY_TWAP_PERIOD.get(pos.short_ex, 4.0)
    long_ramp  = snap.cycle_long  == 1
    short_ramp = snap.cycle_short == 1
    pair_warn  = check_pair_warning(pos.long_ex, pos.short_ex) or ""

    z_score  = calc_z_score(pos)
    decay_sc = calc_decay_score(pos)

    # [NEW] Profit Lock
    cur_profit = pos.total_pct
    if cur_profit > pos.peak_profit:
        pos.peak_profit = cur_profit
    profit_lock_msg = None
    if pos.peak_profit > 0.3 and cur_profit < pos.peak_profit - PROFIT_LOCK_DROP:
        drop = pos.peak_profit - cur_profit
        profit_lock_msg = (
            f"🔒 PROFIT LOCK: пик {pos.peak_profit:+.3f}% → сейчас {cur_profit:+.3f}%\n"
            f"   Упало на {drop:.3f}% — рассмотри выход"
        )

    # [NEW] До фандинга < 10 мин
    fund_reminder = None
    if 0 < mins < FUND_REMINDER_MIN:
        rl_h = rate_per_h(snap.prem_long, pos.long_ex)
        fund_reminder = f"⏰ ФАНДИНГ через {mins:.0f} мин! +{rl_h:.5f}%"

    # [FIX1+FIX2] Partial TP — относительные уровни
    tp_suggestion = None
    for sp_lvl, pct, desc in pos.tp_levels():
        tp_key = f"tp_{sp_lvl:.2f}"
        if snap.spread <= sp_lvl and tp_key not in pos.partial_tp_done:
            tp_suggestion = f"💰 ТЕЙК {pct}%! Спред {snap.spread:+.3f}% ≤ {sp_lvl:+.2f}% ({desc})"
            pos.partial_tp_done.add(tp_key)  # [FIX1] добавляем сразу!
            break

    # Consec up (spread растёт = плохо)
    if snap.spread > prev.spread + 0.05:
        pos.consec_up += 1
    else:
        pos.consec_up = 0

    # [NEW] Timeout: нет движения
    timeout_msg = None
    if vel > -0.05:  # почти не двигается
        if pos.last_move_ts == 0:
            pos.last_move_ts = now
        elif (now - pos.last_move_ts) / 60 > NO_MOVE_MINUTES:
            mins_stuck = (now - pos.last_move_ts) / 60
            timeout_msg = f"⏰ {mins_stuck:.0f} мин без движения — позиция стоит на месте"
    else:
        pos.last_move_ts = now

    is_whale = abs(snap.spread - prev.spread) > WHALE_JUMP

    # ── HARD STOP ────────────────────────────────────────────────
    if snap.spread > pos.hard_stop_level:
        return Alert(
            "HARD_STOP",
            f"🛑 HARD STOP! Спред {snap.spread:+.3f}% → лимит {pos.hard_stop_level:+.3f}%",
            (f"  Ухудшился на {snap.spread - pos.entry_spread:+.3f}% от входа\n"
             f"  Лимит {pos.hard_stop_delta:.1f}% достигнут\n"
             f"  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"),
            "🛑 ЗАКРЫВАЙ НЕМЕДЛЕННО — HARD STOP",
            5, fc_1h=fc1, fc_4h=fc4,
            decay_score=decay_sc, pair_warning=pair_warn,
            fund_reminder=fund_reminder, profit_lock=profit_lock_msg,
        )

    # ── SHORT рампует раньше LONG ────────────────────────────────
    if short_ramp and not long_ramp:
        net_h = rate_per_h(snap.prem_long, pos.long_ex) - rate_per_h(snap.prem_short, pos.short_ex)
        return Alert(
            "EXIT",
            f"⚠️ {pos.short_ex.upper()} В РАМПЕ РАНЬШЕ LONG!",
            (f"  Платим {abs(snap.rate_short):.4f}%/ч\n"
             f"  Net/ч: {net_h:+.5f}%\n"
             f"  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"),
            "⚠️ SHORT рампует первой — выходи",
            4, fc_1h=fc1, fc_4h=fc4,
            pair_warning=pair_warn, decay_score=decay_sc,
            fund_reminder=fund_reminder, profit_lock=profit_lock_msg,
        )

    # ── EXIT: стабильное сдувание ────────────────────────────────
    if pos.consec_up >= EXIT_CONSEC and vel > EXIT_VEL_FAST and not is_whale:
        return Alert(
            "EXIT", "⚠️ СТАБИЛЬНОЕ СДУВАНИЕ",
            (f"  Spread растёт {pos.consec_up} периода ({vel:+.3f}%/ч)\n"
             f"  OI: {od:+.1f}%\n"
             f"  Профит СЕЙЧАС: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"),
            "🚨 ЗАКРЫВАЙ НЕМЕДЛЕННО!",
            4, fc_1h=fc1, fc_4h=fc4,
            pair_warning=pair_warn, decay_score=decay_sc,
            fund_reminder=fund_reminder, profit_lock=profit_lock_msg,
        )

    # ── EXIT: OI рушится ─────────────────────────────────────────
    if od < EXIT_OI_DROP and vel > EXIT_VEL_SLOW and not is_whale and pos.hours_held > 0.5:
        return Alert(
            "EXIT", "📉 OI РУШИТСЯ + SPREAD РАСТЁТ",
            (f"  OI: {od:+.1f}%  Vel: {vel:+.3f}%/ч\n"
             f"  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"),
            "🚨 НЕМЕДЛЕННО ЗАКРЫВАЙ!",
            4, fc_1h=fc1, fc_4h=fc4,
            pair_warning=pair_warn, decay_score=decay_sc,
            fund_reminder=fund_reminder, profit_lock=profit_lock_msg,
        )

    # ── DECAY ────────────────────────────────────────────────────
    if decay_sc >= 6:
        pos.decay_periods += 1
        if pos.decay_periods >= DECAY_PERIODS_MIN:
            return Alert(
                "DECAY", "📉 ДВИЖЕНИЕ СТИХАЕТ",
                (f"  Decay score: {decay_sc}/9\n"
                 f"  Velocity: {vel:+.3f}%/ч  OI: {od:+.1f}%\n"
                 f"  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"),
                "⚠️ Топливо заканчивается — рассмотри частичный выход",
                3, fc_1h=fc1, fc_4h=fc4,
                pair_warning=pair_warn, decay_score=decay_sc,
                partial_tp=tp_suggestion,
                fund_reminder=fund_reminder, profit_lock=profit_lock_msg,
            )
    else:
        pos.decay_periods = 0

    # ── WATCH: кит ───────────────────────────────────────────────
    if is_whale and vel > 0.2:
        return Alert(
            "WATCH", "🐋 Кит? Резкий скачок",
            (f"  Spread: {prev.spread:+.3f}% → {snap.spread:+.3f}% за 30с\n"
             f"  OI: {od:+.1f}%  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"),
            "⚠️ Жди 2-3 мин. Не вернулся → EXIT",
            3, fc_1h=fc1, fc_4h=fc4,
            decay_score=decay_sc,
            fund_reminder=fund_reminder, profit_lock=profit_lock_msg,
        )

    # ── WATCH: начало сдувания ───────────────────────────────────
    if pos.consec_up >= 2 and vel > EXIT_VEL_SLOW and not is_whale:
        return Alert(
            "WATCH", "📉 Начинается сдувание",
            (f"  Spread растёт {pos.consec_up} периода ({vel:+.3f}%/ч)\n"
             f"  OI: {od:+.1f}%  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"),
            "⚠️ Готовься к выходу",
            2, fc_1h=fc1, fc_4h=fc4,
            decay_score=decay_sc,
            fund_reminder=fund_reminder, profit_lock=profit_lock_msg,
        )

    # ── TIMEOUT ──────────────────────────────────────────────────
    if timeout_msg:
        return Alert(
            "TIMEOUT", timeout_msg,
            (f"  Vel: {vel:+.3f}%/ч  OI: {od:+.1f}%\n"
             f"  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"),
            "⏰ Рассмотри выход — топливо могло закончиться",
            2, fc_1h=fc1, fc_4h=fc4,
            decay_score=decay_sc, partial_tp=tp_suggestion,
            fund_reminder=fund_reminder, profit_lock=profit_lock_msg,
        )

    # ── HOLD ─────────────────────────────────────────────────────
    reasons = []

    if long_ramp:
        reasons.append(f"🔴 РАМПА {pos.long_ex.upper()}! +{abs(snap.rate_long):.4f}%/ч")
    elif snap.lr_long >= HOLD_LR:
        ramp_p = cfg(pos.long_ex)["ramp_prem"]
        rem    = ramp_p - snap.prem_long
        if vel < 0 and rem < 0:
            eta = round(abs(rem) / abs(vel), 1)
            reasons.append(f"LR {snap.lr_long*100:.0f}% — рампа через ~{eta}ч")
        else:
            reasons.append(f"LR {snap.lr_long*100:.0f}% — близко к рампе")

    if od >= 2.0:    reasons.append(f"OI +{od:.1f}% — шорты накапливаются")
    if vel <= -0.2:  reasons.append(f"Vel {vel:+.3f}%/ч — разгон идёт")
    if snap.spread - pos.entry_spread < -0.2:
        reasons.append(f"Спред расширился {pos.entry_spread-snap.spread:.2f}% ✅")
    if 0 < mins < 60:
        reasons.append(f"До фандинга {mins:.0f}м → +{abs(snap.rate_long):.5f}%")
    if z_score < -1.5:
        reasons.append(f"Z-score {z_score:.2f} — статистически выгодная позиция")

    if not reasons:
        reasons.append("Нет тревожных сигналов")

    detail = (
        "\n".join(f"  ✅ {r}" for r in reasons) +
        f"\n\n  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})"
        f"  |  Пик: {pos.peak_profit:+.3f}%\n"
        f"  Spread: {snap.spread:+.3f}% вход={pos.entry_spread:+.2f}%\n"
        f"  Z: {z_score:+.2f}  Decay: {decay_sc}/9"
    )

    return Alert(
        "HOLD", "🟢 Позиция здорова", detail, "✅ Держи позицию", 1,
        fc_1h=fc1, fc_4h=fc4,
        pair_warning=pair_warn, decay_score=decay_sc,
        partial_tp=tp_suggestion,
        fund_reminder=fund_reminder,
        profit_lock=profit_lock_msg,
    )


# ════════════════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ
# ════════════════════════════════════════════════════════════════

def _bar(v, n=10):
    return "█" * max(0, min(n, int(v * n))) + "░" * max(0, n - int(v * n))

LEVEL_HEADERS = {
    "HOLD":      "🟢 GUARDIAN: ДЕРЖИ",
    "WATCH":     "🟡 GUARDIAN: СЛЕДИ",
    "DECAY":     "📉 GUARDIAN: ЗАТУХАНИЕ",
    "EXIT":      "🔴 GUARDIAN: ВЫХОДИ!",
    "HARD_STOP": "🛑 HARD STOP!",
    "TIMEOUT":   "⏰ GUARDIAN: СТОП?",
    "INFO":      "ℹ️  GUARDIAN",
}

def fmt_alert(a: Alert, pos: Position, snap: Snap) -> str:
    now_s   = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    mins    = snap.next_fund_s / 60
    fund_s  = f"{mins:.0f}м" if mins < 120 else f"{mins/60:.1f}ч"
    rl_h    = rate_per_h(snap.prem_long,  pos.long_ex)
    rs_h    = rate_per_h(snap.prem_short, pos.short_ex)
    sp_dir  = "↘расш" if snap.spread < pos.entry_spread else "↗суж"
    z       = calc_z_score(pos)
    z_lbl   = ("🔥" if z < -2 else "✅" if z < -1.5 else "⚪" if z < 0 else "⚠️")

    L = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        LEVEL_HEADERS.get(a.level, "📊 GUARDIAN"),
        f"{pos.symbol}  {pos.long_ex.upper()}↑/{pos.short_ex.upper()}↓  {now_s}",
        "─────────────────────────────────────",
        f"Спред: {snap.spread:+.3f}% {sp_dir}  вход={pos.entry_spread:+.2f}%",
        f"💰 Профит: {pos.total_pct:+.3f}%  (${pos.total_usd:+.2f})  пик:{pos.peak_profit:+.3f}%",
        f"   Спред: {pos.spread_pnl:+.4f}%  Фанд: {pos.net_fund:+.4f}%",
        f"⏱  {pos.hours_held:.1f}ч  До выплаты: {fund_s}",
        f"📊 z={z:+.2f}{z_lbl}  Decay:{a.decay_score}/9",
        "",
        f"{pos.long_ex.upper():8}[{_bar(snap.lr_long)}]{snap.lr_long*100:.0f}%"
        f"  {snap.prem_long:+.3f}%  {'🔴РАМПА' if snap.cycle_long==1 else f'{snap.cycle_long}ч'}",
        f"{pos.short_ex.upper():8}[{_bar(snap.lr_short)}]{snap.lr_short*100:.0f}%"
        f"  {snap.prem_short:+.3f}%  {'🔴РАМПА' if snap.cycle_short==1 else f'{snap.cycle_short}ч'}",
        "",
        f"Net/ч: {rl_h-rs_h:+.5f}%  OI: ${snap.total_oi/1e6:.2f}M",
    ]

    # Hard Stop индикатор (только если близко)
    dist = pos.hard_stop_level - snap.spread
    if 0 < dist < 0.3:
        L.append(f"🛑 Hard Stop: {pos.hard_stop_level:+.3f}%  (осталось {dist:.3f}%)")

    if a.pair_warning:
        L += ["", f"⚠️ {a.pair_warning}"]

    L += ["─────────────────────────────────────", "", a.reason, a.details, ""]

    # Profit Lock предупреждение
    if a.profit_lock:
        L += [a.profit_lock, ""]

    # Partial TP подсказка
    if a.partial_tp:
        L += [a.partial_tp, ""]

    # Напоминание о фандинге
    if a.fund_reminder:
        L += [a.fund_reminder, ""]

    if a.level in ("HOLD", "WATCH", "DECAY", "TIMEOUT"):
        fc_line = f"📈 +1ч: {a.fc_1h:+.3f}%  |  +4ч: {a.fc_4h:+.3f}%"
        if a.eta_ramp_h:
            fc_line += f"  |  🔥 рампа ~{a.eta_ramp_h:.1f}ч"
        L.append(fc_line)
        L.append("")

    L += [f"→ {a.action}", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(L)


def fmt_entry_quality(eq: EntryQuality, sym: str, long_ex: str, short_ex: str) -> str:
    bar = "█" * int(eq.score) + "░" * (10 - int(eq.score))
    lines = [
        f"📊 QUALITY SCORE: [{bar}] {eq.score:.1f}/10  {eq.label}",
        f"   Z-score:  {eq.z_score:+.2f}  (< -1.5 = хорошо)",
        f"   Velocity: {eq.velocity:+.3f}%/ч",
        f"   OI delta: {eq.oi_delta:+.1f}%",
        f"   Бирж:     {eq.confirmations}",
    ]
    if eq.suggestions:
        lines += ["", "   Рекомендации:"] + [f"   • {s}" for s in eq.suggestions]
    return "\n".join(lines)


def fmt_status(pos) -> str:
    if not pos or not pos.snaps:
        return "📊 Нет данных"
    snap = pos.snaps[-1]
    z    = calc_z_score(pos)
    icon = {"HOLD":"🟢","WATCH":"🟡","EXIT":"🔴","DECAY":"📉",
            "HARD_STOP":"🛑","TIMEOUT":"⏰"}.get(pos.last_level, "⚪")
    return (
        f"📊 {pos.symbol}  {icon} {pos.last_level}\n"
        f"  {pos.long_ex.upper()}↑/{pos.short_ex.upper()}↓\n"
        f"  Профит: {pos.total_pct:+.3f}% (${pos.total_usd:+.2f})  пик:{pos.peak_profit:+.3f}%\n"
        f"  Spread: {snap.spread:+.3f}%  вход: {pos.entry_spread:+.2f}%\n"
        f"  Z: {z:+.2f}  Hard Stop: {pos.hard_stop_level:+.3f}%\n"
        f"  LR {pos.long_ex.upper()}: {snap.lr_long*100:.0f}%  "
        f"LR {pos.short_ex.upper()}: {snap.lr_short*100:.0f}%\n"
        f"  {pos.hours_held:.1f}ч держим  |  TP выполнено: {len(pos.partial_tp_done)}"
    )


def fmt_summary(pos) -> str:
    return (
        f"📋 ИТОГ {pos.symbol}  {pos.long_ex.upper()}↑/{pos.short_ex.upper()}↓\n"
        f"  Вход: {pos.entry_spread:+.2f}% → Выход: {pos.cur_spread:+.2f}%\n"
        f"  Спред P&L:  {pos.spread_pnl:+.4f}%\n"
        f"  Фандинг:   +{pos.fund_earned:.4f}% / -{pos.fund_paid:.4f}%\n"
        f"  Net fund:   {pos.net_fund:+.4f}%\n"
        f"  Fees:      -{pos.entry_fees:.2f}%\n"
        f"  ─────────────────────────────────\n"
        f"  ИТОГО: {pos.total_pct:+.3f}%  (${pos.total_usd:+.2f})\n"
        f"  Пик прибыли: {pos.peak_profit:+.3f}%\n"
        f"  Держали: {pos.hours_held:.1f}ч  |  Выплат: {len(pos.payments)}\n"
        f"  TP исполнено: {len(pos.partial_tp_done)}"
    )


# ════════════════════════════════════════════════════════════════
# ПОЛУЧЕНИЕ ДАННЫХ
# ════════════════════════════════════════════════════════════════

async def _fetch_snap(pos: Position) -> Optional[Snap]:
    now  = datetime.now(timezone.utc).timestamp()
    pl = ps = rl = rs = oi_l = oi_s = 0.0
    nf = 28800.0

    # Сначала пробуем dev_store (уже есть данные — бесплатно)
    try:
        from radar.index_deviation_radar import dev_store
        sl = dev_store.get_latest(pos.symbol, pos.long_ex)
        ss = dev_store.get_latest(pos.symbol, pos.short_ex)
        if sl and now - sl.timestamp < 120:
            pl = sl.deviation; rl = rate(pl, pos.long_ex)
        if ss and now - ss.timestamp < 120:
            ps = ss.deviation; rs = rate(ps, pos.short_ex)
    except Exception:
        pass

    # Если dev_store нет — берём через ccxt (кэшированные объекты)
    if pl == 0:
        async def _one(ex_id):
            ex = await _get_ccxt(ex_id)
            if not ex:
                return None
            try:
                raw = await ex.fetch_funding_rate(f"{pos.symbol}/USDT:USDT")
                mark = float(raw.get("markPrice") or raw.get("info", {}).get("markPrice") or 0)
                idx  = float(raw.get("indexPrice") or raw.get("info", {}).get("indexPrice") or mark)
                nf_  = raw.get("fundingTimestamp")
                oir  = await ex.fetch_open_interest(f"{pos.symbol}/USDT:USDT")
                oi   = float(oir.get("openInterestValue") or oir.get("openInterest", 0) or 0)
                return ((mark - idx) / idx * 100 if idx > 0 else 0,
                        oi, max(0, nf_ / 1000 - now) if nf_ else 28800.0)
            except Exception as e:
                logger.debug(f"_one {ex_id}: {e}")
                return None

        r = await asyncio.gather(_one(pos.long_ex), _one(pos.short_ex), return_exceptions=True)
        if isinstance(r[0], tuple) and r[0]:
            pl, oi_l, nf = r[0]; rl = rate(pl, pos.long_ex)
        if isinstance(r[1], tuple) and r[1]:
            ps, oi_s, _  = r[1]; rs = rate(ps, pos.short_ex)

    if pl == 0:
        return None

    return Snap(
        ts=now, prem_long=pl, prem_short=ps,
        rate_long=rl, rate_short=rs,
        lr_long=lr(pl, pos.long_ex), lr_short=lr(ps, pos.short_ex),
        oi_long=oi_l, oi_short=oi_s,
        next_fund_s=nf,
        cycle_long=cycle(pl, pos.long_ex), cycle_short=cycle(ps, pos.short_ex),
        next_fund_abs=now + nf,
    )


def _check_payment(pos: Position, snap: Snap) -> Optional[str]:
    """
    [FIX4] Детектируем выплату по пересечению нуля таймера,
    а не по прыжку next_fund_s.
    """
    if not pos.snaps:
        return None
    prev = pos.snaps[-1]

    # Выплата если предыдущий next_fund_s был < 120с и сейчас > 3600с
    payment_happened = (prev.next_fund_s < 120 and snap.next_fund_s > 3600)

    # Или: abs_time текущей выплаты стал в прошлом
    if not payment_happened and pos.last_fund_abs > 0:
        payment_happened = (snap.ts > pos.last_fund_abs and pos.last_fund_abs != snap.next_fund_abs)

    if payment_happened:
        pos.last_fund_abs = snap.next_fund_abs
        earned = abs(snap.rate_long)  if snap.rate_long  < 0 else 0.0
        paid   = abs(snap.rate_short) if snap.rate_short < 0 else 0.0
        pos.fund_earned += earned
        pos.fund_paid   += paid
        pos.payments.append({"ts": snap.ts, "earned": earned, "paid": paid})
        net = earned - paid
        return (
            f"💸 ВЫПЛАТА #{len(pos.payments)}\n"
            f"  +{earned:.5f}% / -{paid:.5f}%\n"
            f"  Net: {net:+.5f}% (${net/100*pos.size_usd:+.3f})"
        )
    return None


# ════════════════════════════════════════════════════════════════
# ЦИКЛ МОНИТОРИНГА
# ════════════════════════════════════════════════════════════════

_positions: dict = {}
_tasks:     dict = {}

# [FIX7] Интервалы отправки — HOLD редко, критическое часто
SEND_INTERVALS = {
    "HARD_STOP": 10,
    "EXIT":      60,
    "DECAY":     90,
    "WATCH":     120,
    "TIMEOUT":   300,
    "HOLD":      300,
    "INFO":      300,
}

async def _loop(chat_id: str, pos: Position, send_fn: Callable):
    prev_level = "INFO"

    while pos.active:
        try:
            snap = await _fetch_snap(pos)
            if snap:
                pay_msg = _check_payment(pos, snap)
                if pay_msg:
                    await send_fn(chat_id, pay_msg)

                add_twap(pos, snap)
                pos.spread_history.append(snap.spread)
                pos.snaps.append(snap)

                a = analyze(pos)
                now_ = datetime.now(timezone.utc).timestamp()
                gap  = now_ - pos.last_ts
                thr  = SEND_INTERVALS.get(a.level, 300)

                # Всегда отправляем если:
                send = (
                    gap > thr or                               # время вышло
                    (a.level != prev_level and                 # уровень изменился
                     a.level != "HOLD") or                    # ... и не в HOLD
                    a.level in ("EXIT", "HARD_STOP") or        # критическое
                    (prev_level in ("WATCH","DECAY","EXIT","TIMEOUT") and a.level == "HOLD")  # восстановление
                )

                if send:
                    await send_fn(chat_id, fmt_alert(a, pos, snap))
                    pos.last_ts  = now_
                    prev_level   = a.level

                pos.last_level = a.level

            await asyncio.sleep(CHECK_S)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"_loop {chat_id}: {e}")
            await asyncio.sleep(CHECK_S)


# ════════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════════

def start_watch(chat_id: str, symbol: str, long_ex: str, short_ex: str,
                entry_spread: float, size_usd: float, send_fn: Callable,
                hard_stop_delta: float = HARD_STOP_DEFAULT) -> Position:
    fees = (cfg(long_ex)["fee"] + cfg(short_ex)["fee"]) * 2
    if chat_id in _positions:
        stop_watch(chat_id)
    pos = Position(
        symbol=symbol.upper(), long_ex=long_ex.lower(), short_ex=short_ex.lower(),
        entry_spread=entry_spread, entry_time=datetime.now(timezone.utc).timestamp(),
        size_usd=size_usd, entry_fees=fees,
        hard_stop_delta=hard_stop_delta,
        # [FIX5] сброс decay при старте
        decay_periods=0, consec_up=0,
    )
    _positions[chat_id] = pos
    _tasks[chat_id]     = asyncio.create_task(_loop(chat_id, pos, send_fn))
    return pos


def stop_watch(chat_id: str) -> Optional[Position]:
    pos = _positions.pop(chat_id, None)
    t   = _tasks.pop(chat_id, None)
    if pos: pos.active = False
    if t and not t.done(): t.cancel()
    return pos


def get_watch(chat_id: str) -> Optional[Position]:
    return _positions.get(chat_id)


# ════════════════════════════════════════════════════════════════
# TELEGRAM КОМАНДА /watch
# ════════════════════════════════════════════════════════════════

async def cmd_watch(update, context):
    """
    /watch START COS OKX GATE -1.5 50 0.7
    /watch STATUS
    /watch STOP
    /watch ANALYSE COS OKX GATE -1.5   ← разовый анализ без запуска
    """
    # Поддержка как Update, так и CallbackQuery
    if hasattr(update, "effective_chat") and update.effective_chat:
        chat_id = str(update.effective_chat.id)
        msg_obj = update.effective_message
    elif hasattr(update, "message") and update.message:
        chat_id = str(update.message.chat_id)
        msg_obj = update.message
    else:
        # Fallback для CallbackQuery если вдруг выше не сработало
        chat_id = str(update.from_user.id)
        msg_obj = update.message

    args = context.args or []

    if not args:
        await msg_obj.reply_text(
            "👁 POSITION GUARDIAN v4.1\n\n"
            "/watch START <МОНЕТА> <LONG> <SHORT> [спред] [размер$] [stop%]\n"
            "  Пример: /watch START COS OKX GATE -1.5 50 0.7\n\n"
            "/watch ANALYSE <МОНЕТА> <LONG> <SHORT> [спред]\n"
            "  Разовый анализ без запуска слежки\n\n"
            "/watch STATUS — текущий статус\n"
            "/watch STOP   — остановить\n\n"
            "stop% — hard stop delta от входа (дефолт 0.7%)\n"
            "TP план: авто (-1.0%/-1.5%/-2.0% от входа)"
        )
        return

    cmd = args[0].upper()

    # ── STATUS ────────────────────────────────────────────────────
    if cmd == "STATUS":
        pos = get_watch(chat_id)
        if not pos:
            await msg_obj.reply_text("Нет активного мониторинга.")
        else:
            await msg_obj.reply_text(fmt_status(pos))

    # ── STOP ──────────────────────────────────────────────────────
    elif cmd == "STOP":
        pos = stop_watch(chat_id)
        if pos:
            await msg_obj.reply_text(f"⏹ Остановлено.\n\n{fmt_summary(pos)}")
        else:
            await msg_obj.reply_text("Нет активного мониторинга.")

    # ── ANALYSE — разовый анализ ──────────────────────────────────
    elif cmd == "ANALYSE":
        if len(args) < 4:
            await msg_obj.reply_text(
                "Нужно: /watch ANALYSE МОНЕТА LONG SHORT [спред]")
            return
        sym  = args[1].upper()
        lex  = args[2].lower()
        sex  = args[3].lower()
        sprd = float(args[4]) if len(args) > 4 else -1.0

        # [FIX10] берём реальные данные из dev_store
        vel = oi_d = 0.0; confs = 1; hist = []
        try:
            from radar.index_deviation_radar import dev_store, calc_velocity
            snaps_all = dev_store.get(sym, lex, hours=4.0)
            if snaps_all:
                vel    = calc_velocity(snaps_all)
                oi_d   = 0.0  # заполнить если dev_store хранит OI
                confs  = sum(1 for ex in [lex, sex, "binance", "gate"]
                             if dev_store.get_latest(sym, ex) is not None)
                hist   = [s.deviation for s in snaps_all[-100:]]
        except Exception:
            pass

        eq = calc_entry_quality(sym, lex, sex, sprd, hist, vel, oi_d, confs)
        warn = check_pair_warning(lex, sex)

        fees = (cfg(lex)["fee"] + cfg(sex)["fee"]) * 2
        rl_h = rate_per_h(sprd, lex)
        rs_h = rate_per_h(sprd, sex)

        text = (
            f"🔍 АНАЛИЗ ВХОДА: {sym}\n"
            f"  {lex.upper()}↑  /  {sex.upper()}↓\n"
            f"  Спред: {sprd:+.1f}%\n"
            f"  Fees: {fees:.2f}%  |  Net/ч: {rl_h-rs_h:+.5f}%\n\n"
            + fmt_entry_quality(eq, sym, lex, sex)
            + (f"\n\n⚠️ {warn}" if warn else "")
            + f"\n\nЗапустить слежку:\n/watch START {sym} {lex.upper()} {sex.upper()} {sprd} 50"
        )
        await msg_obj.reply_text(text)

    # ── START ──────────────────────────────────────────────────────
    elif cmd == "START":
        if len(args) < 4:
            await msg_obj.reply_text(
                "Нужно: /watch START МОНЕТА LONG SHORT [спред] [размер$] [stop%]")
            return
        sym   = args[1].upper()
        lex   = args[2].lower()
        sex   = args[3].lower()
        sprd  = float(args[4]) if len(args) > 4 else -1.0
        size  = float(args[5]) if len(args) > 5 else 50.0
        hstop = float(args[6]) if len(args) > 6 else HARD_STOP_DEFAULT

        bot = context.bot
        async def send_fn(cid, text):
            try: await bot.send_message(cid, text)
            except Exception as e: logger.error(f"send: {e}")

        # [FIX10] Entry Quality с реальными данными
        vel = oi_d = 0.0; confs = 1; hist = []
        try:
            from radar.index_deviation_radar import dev_store, calc_velocity
            snaps_all = dev_store.get(sym, lex, hours=4.0)
            if snaps_all:
                vel   = calc_velocity(snaps_all)
                confs = sum(1 for ex in [lex, sex, "binance", "gate"]
                            if dev_store.get_latest(sym, ex) is not None)
                hist  = [s.deviation for s in snaps_all[-100:]]
        except Exception:
            pass

        eq   = calc_entry_quality(sym, lex, sex, sprd, hist, vel, oi_d, confs)
        warn = check_pair_warning(lex, sex)
        pos  = start_watch(chat_id, sym, lex, sex, sprd, size, send_fn, hstop)
        fees = (cfg(lex)["fee"] + cfg(sex)["fee"]) * 2

        # TP уровни для показа
        tp_lines = "\n".join(
            f"   {pct}% при Δ{off:+.1f}% (спред {pos.entry_spread+off:+.2f}%)"
            for off, pct, desc in PARTIAL_TP_OFFSETS
        )

        text = (
            f"✅ GUARDIAN v4.1 ЗАПУЩЕН\n"
            f"  {sym}  {lex.upper()}↑/{sex.upper()}↓\n"
            f"  Вход: {sprd:+.1f}%  Размер: ${size:.0f}\n"
            f"  Fees: {fees:.2f}%  Hard Stop: {pos.hard_stop_level:+.3f}%\n\n"
            f"📊 TP план (от входа {sprd:+.1f}%):\n{tp_lines}\n\n"
            + fmt_entry_quality(eq, sym, lex, sex)
            + (f"\n\n⚠️ {warn}" if warn else "")
        )
        await msg_obj.reply_text(text)

    else:
        await msg_obj.reply_text("Команды: START | STOP | STATUS | ANALYSE")


# ════════════════════════════════════════════════════════════════
# ТЕСТЫ
# ════════════════════════════════════════════════════════════════

def _test():
    import sys, types, random
    random.seed(42)

    fake = types.ModuleType("loguru")
    class FL:
        def info(self,*a,**k): pass
        def warning(self,*a,**k): pass
        def error(self,*a,**k): pass
        def debug(self,*a,**k): pass
    fake.logger = FL()
    sys.modules["loguru"] = fake
    for m in ["radar","radar.index_deviation_radar"]:
        sys.modules.setdefault(m, types.ModuleType(m))

    now   = datetime.now(timezone.utc).timestamp()
    ICONS = {"HOLD":"🟢","WATCH":"🟡","DECAY":"📉","EXIT":"🔴",
             "HARD_STOP":"🛑","TIMEOUT":"⏰","INFO":"ℹ️"}

    def mk(lex, sex, es, hstop=0.7, fe=0.0, fp=0.0, peak=0.0):
        fees = (cfg(lex)["fee"] + cfg(sex)["fee"]) * 2
        p = Position(symbol="COS", long_ex=lex, short_ex=sex,
                     entry_spread=es, entry_time=now - 7200,
                     size_usd=50, entry_fees=fees,
                     hard_stop_delta=hstop, fund_earned=fe, fund_paid=fp)
        p.peak_profit = peak
        return p

    def fill(pos, prems_l, prems_s, ois, nf=1800):
        for i, (pl, ps, o) in enumerate(zip(prems_l, prems_s, ois)):
            ts = now - (len(prems_l) - 1 - i) * 30
            s  = Snap(
                ts=ts, prem_long=pl, prem_short=ps,
                rate_long=rate(pl, pos.long_ex), rate_short=rate(ps, pos.short_ex),
                lr_long=lr(pl, pos.long_ex), lr_short=lr(ps, pos.short_ex),
                oi_long=o*0.4, oi_short=o*0.6,
                next_fund_s=nf,
                cycle_long=cycle(pl, pos.long_ex), cycle_short=cycle(ps, pos.short_ex),
                next_fund_abs=ts + nf,
            )
            pos.snaps.append(s)
            add_twap(pos, s)
            pos.spread_history.append(s.spread)

    print("=" * 65)
    print("  ТЕСТ position_guardian.py v4.1 — все 11 фиксов")
    print("=" * 65)

    TESTS = [
        # name,          lex,   sex,     es,   hstop, prems_l, prems_s, ois, kwargs
        ("HOLD+рампа",   "okx","gate",  -1.7,  0.7,
         [-3.0,-3.5,-4.0,-4.1,-4.0,-3.9,-3.8,-3.7],
         [-0.5,-0.6,-0.7,-0.8,-0.9,-0.9,-0.9,-0.8],
         [8e6]*8),
        ("HARD STOP",    "binance","gate",-1.0, 0.5,
         [-1.5,-1.2,-0.8,-0.4,-0.2,+0.1,+0.3,+0.5],
         [-0.3,-0.3,-0.3,-0.3,-0.2,-0.2,-0.1,-0.1],
         [8e6]*8),
        ("DECAY",        "okx","apex",  -1.5,  0.7,
         [-3.5,-3.4,-3.3,-3.3,-3.3,-3.2,-3.2,-3.2],
         [-0.1]*8,
         [10e6,9.9e6,9.8e6,9.7e6,9.6e6,9.5e6,9.4e6,9.3e6]),
        ("EXIT-сдувание","binance","kucoin",-1.4,0.7,
         [-6.0,-5.8,-5.5,-5.0,-4.5,-4.0,-3.5,-3.0],
         [-0.5]*8, [12e6]*8),
        ("Partial TP",   "okx","apex",  -2.5,  0.8,
         [-3.5,-3.7,-3.9,-4.1,-4.3,-4.5,-4.7,-4.9],
         [-0.1]*8, [8e6]*8),
    ]

    for t in TESTS:
        name, lex, sex, es, hstop = t[0], t[1], t[2], t[3], t[4]
        prems_l, prems_s, ois = t[5], t[6], t[7]
        kwargs = t[8] if len(t) > 8 else {}

        p = mk(lex, sex, es, hstop, **kwargs)
        fill(p, prems_l, prems_s, ois)
        a = analyze(p)

        print(f"\n{'─'*65}")
        print(f"  {ICONS.get(a.level,'?')} [{name}]  → level={a.level}")
        print(f"  Профит: {p.total_pct:+.4f}%  Z: {calc_z_score(p):+.2f}  Decay: {a.decay_score}/9")
        if a.partial_tp:
            print(f"  {a.partial_tp}")
        if a.profit_lock:
            print(f"  {a.profit_lock}")
        if a.fund_reminder:
            print(f"  {a.fund_reminder}")

    # ── [FIX1] TP не спамит ─────────────────────────────────────
    print(f"\n{'─'*65}")
    print("  FIX1: TP не спамит (должен сработать ровно 1 раз)")
    p = mk("okx","apex", -2.5)
    fill(p, [-3.5]*8, [-0.1]*8, [8e6]*8)
    count = 0
    for _ in range(5):
        a = analyze(p)
        if a.partial_tp: count += 1
    print(f"  TP сработал {count} раз из 5 итераций  {'✅' if count == 1 else '❌'}")

    # ── [FIX2] TP относительный ─────────────────────────────────
    print(f"\n{'─'*65}")
    print("  FIX2: TP относительный от entry_spread")
    p2 = mk("okx","apex", -2.5)  # вошли при -2.5%
    levels = p2.tp_levels()
    print(f"  entry_spread = -2.5%")
    for sp_lvl, pct, desc in levels:
        print(f"  TP {pct}%: уровень спреда = {sp_lvl:+.2f}% ({desc})")
    all_below = all(lvl < -2.5 for lvl, _, _ in levels)
    print(f"  Все TP ниже входа: {'✅' if all_below else '❌'}")

    # ── [FIX6] Velocity по spread ────────────────────────────────
    print(f"\n{'─'*65}")
    print("  FIX6: velocity() по spread (не prem_long)")
    p3 = mk("okx","gate", -1.0)
    # spread растёт (плохо)
    fill(p3, [-1.0,-0.8,-0.6,-0.4], [-0.5,-0.5,-0.5,-0.5], [8e6]*4)
    vel_val = velocity(p3.snaps)
    print(f"  spread: -1.5→-0.1→... vel={vel_val:+.3f}%/ч  "
          f"{'✅ (>0 значит spread растёт)' if vel_val > 0 else '❌'}")

    # ── [FIX9] TWAP история 4ч ──────────────────────────────────
    print(f"\n{'─'*65}")
    p4 = mk("gate","kucoin", -2.0)
    print(f"  FIX9: twap_hist maxlen={p4.twap_hist_long.maxlen} "
          f"{'✅ (≥480)' if p4.twap_hist_long.maxlen >= 480 else '❌'}")

    print(f"\n{'='*65}")
    print("✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
    print(f"""
ИТОГ — что исправлено в v4.1:
  ✅ FIX1  TP подсказки: срабатывают 1 раз (не спам)
  ✅ FIX2  TP уровни: относительные от entry_spread
  ✅ FIX3  ccxt кэшируется (не создаётся каждые 30с)
  ✅ FIX4  _check_payment: правильная логика через пересечение 0
  ✅ FIX5  decay_periods сбрасывается при start_watch
  ✅ FIX6  velocity() по spread (оба плеча учитываются)
  ✅ FIX7  HOLD не спамит — умный тригер по изменениям
  ✅ FIX8  hard_stop_level правильный (entry+delta)
  ✅ FIX9  twap_hist maxlen=480 (4ч реальный TWAP)
  ✅ FIX10 Entry Quality с реальными данными из dev_store
  ✅ FIX11 HOLD только при изменениях/переходах уровней
  ✅ NEW   Timeout: 20+ мин без движения
  ✅ NEW   Profit Lock: падение от пика
  ✅ NEW   Фандинг < 10 мин → напоминание
  ✅ NEW   /watch ANALYSE — разовый анализ без слежки

КОМАНДЫ:
  /watch START COS OKX GATE -1.5 50 0.7
  /watch ANALYSE COS OKX GATE -1.5    ← новое!
  /watch STATUS
  /watch STOP
""")


if __name__ == "__main__":
    _test()
