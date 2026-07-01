"""
radar/ramp_memory.py  (Upgraded to v3 Logic)
===========================================
СИСТЕМА ПАМЯТИ РАЗГОНОВ — ИНСТИТУЦИОНАЛЬНЫЙ УРОВЕНЬ

УЛУЧШЕНИЯ:
  1. Z-score premium: Анализ аномалий вместо статических порогов.
  2. EV (Expected Value): Расчет мат. ожидания сделки с учетом комиссий и слиппеджа.
  3. TTF (Time-to-Funding): Бонус к сигналу перед выплатой фандинга Gate.
  4. MFE/MAE: Отслеживание максимальной прибыли и просадки для каждого события.
  5. Реальная статистика: Hit Rate, Profit Factor и Sharpe из истории.

БАЗА ДАННЫХ: SQLite (ramp_memory.db)
"""

import math
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ramp_memory.db"
)

# ── Константы торговли ───────────────────────────────────────
ENTRY_FEE_PCT  = 0.05   # % на открытие (×2 стороны)
EXIT_FEE_PCT   = 0.05   # % на закрытие
SLIPPAGE_PCT   = 0.05   # % реальный slippage на вход+выход
TOTAL_COST_PCT = ENTRY_FEE_PCT + EXIT_FEE_PCT + SLIPPAGE_PCT

TARGET_SPREAD_PCT = 2.0  # целевой спред для расчёта win/loss
MIN_PROFITABLE_SPREAD = TOTAL_COST_PCT * 1.5  # минимум чтобы покрыть costs

# ── Пики активности (UTC часы) ───────────────────────────────
PEAK_HOURS = {0, 1, 7, 8, 15, 16, 23}


# ════════════════════════════════════════════════════════════════
# DATACLASSES
# ════════════════════════════════════════════════════════════════

@dataclass
class RampEvent:
    symbol:            str
    exchange:          str
    ts_start:          float
    ts_end:            float     # 0 если идёт
    oi_at_start:       float
    oi_at_peak:        float
    premium_at_start:  float
    premium_at_peak:   float
    funding_rate_pct:  float     # Gate rate %/ч
    oi_velocity_1h:    float
    max_spread:        float
    duration_h:        float
    source:            str
    notes:             str = ""
    # [NEW] MFE/MAE лейблинг
    mfe_pct:           float = 0.0   # max favorable excursion (%)
    mae_pct:           float = 0.0   # max adverse excursion (%)
    time_to_target_h:  float = 0.0   # когда достиг TARGET_SPREAD_PCT


@dataclass
class OISnapshot:
    symbol:      str
    exchange:    str
    ts:          float
    oi_usdt:     float
    premium:     float
    funding_pct: float


@dataclass
class RampPrediction:
    symbol:           str
    exchange:         str
    # Ключевые метрики
    confidence:       float    # 0-1 итоговый score
    level:            str      # HIGH / MEDIUM / WATCH / SKIP
    # [NEW] EV компоненты
    ev_pct:           float    # Expected Value в %
    win_prob:         float    # P(win) из исторических данных
    avg_win_pct:      float    # средний профит при выигрыше
    avg_loss_pct:     float    # средний убыток при проигрыше
    # Признаки
    premium_z:        float    # [NEW] Z-score premium
    premium_now:      float
    oi_velocity:      float
    gate_rate_pct:    float
    ttf_minutes:      float    # [NEW] time-to-funding в минутах
    # История
    historical_ramps: int
    hit_rate:         float    # [NEW] реальный hit rate из базы
    profit_factor:    float    # [NEW] реальный PF из базы
    avg_max_spread:   float
    avg_duration_h:   float
    # Состояние
    oi_now:           float
    oi_threshold:     float
    premium_zone:     str
    evidence:         list
    is_peak_hour:     bool
    ts:               float


@dataclass
class PerformanceStats:
    """Реальная статистика системы из закрытых разгонов."""
    n_total:       int
    n_wins:        int     # max_spread > TARGET
    n_losses:      int
    hit_rate:      float   # n_wins / n_total
    avg_win:       float   # средний max_spread при выигрыше
    avg_loss:      float   # средний max_spread при проигрыше
    profit_factor: float   # (n_wins × avg_win) / (n_losses × avg_loss)
    sharpe:        float   # упрощённый Sharpe
    avg_mfe:       float   # [NEW] средний MFE
    avg_mae:       float   # [NEW] средний MAE
    avg_duration:  float
    best_spread:   float
    worst_spread:  float


# ════════════════════════════════════════════════════════════════
# БД
# ════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS ramp_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    ts_start          REAL NOT NULL,
    ts_end            REAL DEFAULT 0,
    oi_at_start       REAL DEFAULT 0,
    oi_at_peak        REAL DEFAULT 0,
    premium_at_start  REAL DEFAULT 0,
    premium_at_peak   REAL DEFAULT 0,
    funding_rate_pct  REAL DEFAULT 0,
    oi_velocity_1h    REAL DEFAULT 0,
    max_spread        REAL DEFAULT 0,
    duration_h        REAL DEFAULT 0,
    source            TEXT DEFAULT '',
    notes             TEXT DEFAULT '',
    mfe_pct           REAL DEFAULT 0,
    mae_pct           REAL DEFAULT 0,
    time_to_target_h  REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS oi_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    ts          REAL NOT NULL,
    oi_usdt     REAL NOT NULL,
    premium     REAL DEFAULT 0,
    funding_pct REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap ON oi_snapshots(symbol, exchange, ts);
CREATE INDEX IF NOT EXISTS idx_ev   ON ramp_events(symbol, exchange);
CREATE INDEX IF NOT EXISTS idx_ev_ts ON ramp_events(ts_start);
"""


class RampDB:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._c: Optional[sqlite3.Connection] = None

    def connect(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)
        
        # Миграция: добавляем колонки, если их нет
        try:
            self._c.execute("ALTER TABLE ramp_events ADD COLUMN mfe_pct REAL DEFAULT 0")
        except sqlite3.OperationalError: pass
        try:
            self._c.execute("ALTER TABLE ramp_events ADD COLUMN mae_pct REAL DEFAULT 0")
        except sqlite3.OperationalError: pass
        try:
            self._c.execute("ALTER TABLE ramp_events ADD COLUMN time_to_target_h REAL DEFAULT 0")
        except sqlite3.OperationalError: pass
        
        self._c.commit()
        logger.info(f"RampDB connected: {self.path}")

    def _db(self):
        if not self._c:
            self.connect()
        return self._c

    # ── Разгоны ──────────────────────────────────────────────

    def save_ramp(self, e: RampEvent) -> int:
        cur = self._db().execute("""
            INSERT INTO ramp_events
            (symbol,exchange,ts_start,ts_end,
             oi_at_start,oi_at_peak,premium_at_start,premium_at_peak,
             funding_rate_pct,oi_velocity_1h,max_spread,duration_h,
             source,notes,mfe_pct,mae_pct,time_to_target_h)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (e.symbol, e.exchange, e.ts_start, e.ts_end,
              e.oi_at_start, e.oi_at_peak, e.premium_at_start, e.premium_at_peak,
              e.funding_rate_pct, e.oi_velocity_1h, e.max_spread, e.duration_h,
              e.source, e.notes, e.mfe_pct, e.mae_pct, e.time_to_target_h))
        self._db().commit()
        return cur.lastrowid

    def close_ramp(self, event_id: int, ts_end: float,
                   oi_peak: float, prem_peak: float, max_spread: float,
                   mfe_pct: float = 0.0, mae_pct: float = 0.0,
                   time_to_target_h: float = 0.0):
        row = self._db().execute(
            "SELECT ts_start FROM ramp_events WHERE id=?", (event_id,)
        ).fetchone()
        dur = (ts_end - row[0]) / 3600 if row else 0
        self._db().execute("""
            UPDATE ramp_events SET
                ts_end=?, oi_at_peak=?, premium_at_peak=?,
                max_spread=?, duration_h=?,
                mfe_pct=?, mae_pct=?, time_to_target_h=?
            WHERE id=?
        """, (ts_end, oi_peak, prem_peak, max_spread, dur,
              mfe_pct, mae_pct, time_to_target_h, event_id))
        self._db().commit()

    def get_ramps(self, symbol: str, exchange: str = None,
                  closed_only: bool = False) -> list[dict]:
        q    = "SELECT * FROM ramp_events WHERE symbol=?"
        args = [symbol]
        if exchange:
            q += " AND exchange=?"; args.append(exchange)
        if closed_only:
            q += " AND ts_end > 0"
        return [dict(r) for r in self._db().execute(
            q + " ORDER BY ts_start DESC", args)]

    def all_symbols(self) -> list[str]:
        return [r[0] for r in self._db().execute(
            "SELECT DISTINCT symbol FROM ramp_events ORDER BY symbol")]

    # ── OI снимки ────────────────────────────────────────────

    def save_snap(self, s: OISnapshot):
        self._db().execute("""
            INSERT INTO oi_snapshots (symbol,exchange,ts,oi_usdt,premium,funding_pct)
            VALUES (?,?,?,?,?,?)
        """, (s.symbol, s.exchange, s.ts, s.oi_usdt, s.premium, s.funding_pct))
        self._db().commit()

    def save_snaps_bulk(self, snaps: list[OISnapshot]):
        self._db().executemany("""
            INSERT INTO oi_snapshots (symbol,exchange,ts,oi_usdt,premium,funding_pct)
            VALUES (?,?,?,?,?,?)
        """, [(s.symbol, s.exchange, s.ts, s.oi_usdt, s.premium, s.funding_pct)
               for s in snaps])
        self._db().commit()

    def get_snaps(self, symbol: str, exchange: str,
                  hours: float = 24.0) -> list[dict]:
        cutoff = time.time() - hours * 3600
        return [dict(r) for r in self._db().execute("""
            SELECT * FROM oi_snapshots
            WHERE symbol=? AND exchange=? AND ts >= ?
            ORDER BY ts ASC
        """, (symbol, exchange, cutoff))]

    def cleanup(self, keep_h: float = 48.0):
        n = self._db().execute(
            "DELETE FROM oi_snapshots WHERE ts<?",
            (time.time() - keep_h * 3600,)
        ).rowcount
        self._db().commit()
        if n:
            logger.info(f"RampDB cleanup: {n} снимков удалено")

    # ── Статистика ───────────────────────────────────────────

    def symbol_stats(self, symbol: str,
                     exchange: str = "gate") -> Optional[dict]:
        """Статистика по прошлым разгонам для предиктора."""
        ramps = [r for r in self.get_ramps(symbol, exchange)
                 if r["ts_end"] > 0]
        if len(ramps) < 2:
            return None

        def _f(lst): return [x for x in lst if x]
        def avg(lst): return sum(lst) / len(lst) if lst else 0
        def med(lst):
            s = sorted(lst); return s[len(s) // 2] if s else 0
        def std(lst):
            if len(lst) < 2: return 0
            m = avg(lst)
            return math.sqrt(sum((x - m) ** 2 for x in lst) / (len(lst) - 1))
        def pct(lst, p):
            s = sorted(lst)
            return s[max(0, int(len(s) * p / 100) - 1)] if s else 0

        oi_s   = _f([r["oi_at_start"] for r in ramps])
        prems  = [r["premium_at_start"] for r in ramps]
        sprs   = _f([r["max_spread"] for r in ramps])
        durs   = _f([r["duration_h"] for r in ramps])
        vels   = _f([r["oi_velocity_1h"] for r in ramps])
        mfes   = _f([r.get("mfe_pct", 0) for r in ramps])
        maes   = _f([r.get("mae_pct", 0) for r in ramps])
        rates  = _f([r["funding_rate_pct"] for r in ramps])

        # Z-score: среднее и std premium входов
        prem_avg = avg(prems)
        prem_std = std(prems)

        return {
            "n":           len(ramps),
            # OI
            "oi_avg":      avg(oi_s), "oi_med": med(oi_s), "oi_std": std(oi_s),
            "oi_min":      min(oi_s) if oi_s else 0,
            # Premium — для Z-score
            "prem_avg":    prem_avg,  "prem_std": prem_std,
            "prem_min":    min(prems) if prems else 0,
            "prem_max":    max(prems) if prems else 0,
            "prem_p10":    pct(prems, 10),
            "prem_p90":    pct(prems, 90),
            # Результаты
            "spread_avg":  avg(sprs), "spread_max": max(sprs) if sprs else 0,
            "spread_p25":  pct(sprs, 25), "spread_p75": pct(sprs, 75),
            # [NEW] MFE/MAE
            "mfe_avg":     avg(mfes), "mae_avg": avg(maes),
            # Прочее
            "dur_avg":     avg(durs),
            "vel_avg":     avg(vels), "vel_p25": pct(vels, 25),
            "rate_avg":    avg(rates),
        }

    def performance(self, symbol: str = None,
                    exchange: str = "gate") -> Optional[PerformanceStats]:
        """Считает реальные performance метрики из закрытых разгонов."""
        if symbol:
            ramps = self.get_ramps(symbol, exchange, closed_only=True)
        else:
            rows = self._db().execute(
                "SELECT * FROM ramp_events WHERE ts_end > 0 ORDER BY ts_start"
            ).fetchall()
            ramps = [dict(r) for r in rows]

        if len(ramps) < 3:
            return None

        wins   = [r for r in ramps if r["max_spread"] >= TARGET_SPREAD_PCT]
        losses = [r for r in ramps if r["max_spread"] < TARGET_SPREAD_PCT]

        n = len(ramps)
        nw = len(wins); nl = len(losses)
        if n == 0:
            return None

        hit_rate = nw / n
        avg_win  = sum(r["max_spread"] for r in wins)  / nw if nw else 0
        avg_loss = sum(r["max_spread"] for r in losses) / nl if nl else 0

        gross_win  = nw * max(avg_win  - TOTAL_COST_PCT, 0)
        gross_loss = nl * max(TOTAL_COST_PCT - avg_loss, TOTAL_COST_PCT * 0.5)
        pf = gross_win / gross_loss if gross_loss > 0 else 0

        # Упрощённый Sharpe (PnL / std PnL)
        all_pnl = [r["max_spread"] - TOTAL_COST_PCT for r in ramps]
        avg_p   = sum(all_pnl) / n
        std_p   = math.sqrt(sum((x - avg_p) ** 2 for x in all_pnl) / n) if n > 1 else 1
        sharpe  = (avg_p / std_p * math.sqrt(252)) if std_p > 0 else 0

        mfes = [r.get("mfe_pct", r["max_spread"]) for r in ramps]
        maes = [r.get("mae_pct", 0) for r in ramps]

        return PerformanceStats(
            n_total       = n,
            n_wins        = nw,
            n_losses      = nl,
            hit_rate      = round(hit_rate, 3),
            avg_win       = round(avg_win, 3),
            avg_loss      = round(avg_loss, 3),
            profit_factor = round(pf, 2),
            sharpe        = round(sharpe, 2),
            avg_mfe       = round(sum(mfes) / n, 3),
            avg_mae       = round(sum(maes) / n, 3),
            avg_duration  = round(sum(r["duration_h"] for r in ramps) / n, 2),
            best_spread   = round(max(r["max_spread"] for r in ramps), 3),
            worst_spread  = round(min(r["max_spread"] for r in ramps), 3),
        )


db = RampDB()


# ════════════════════════════════════════════════════════════════
# МЕТРИКИ
# ════════════════════════════════════════════════════════════════

def calc_premium_zscore(premium_now: float, stats: dict) -> float:
    """Z-score premium для этой монеты. z = (premium_now - prem_avg) / prem_std"""
    prem_avg = stats.get("prem_avg", -0.3)
    prem_std = stats.get("prem_std", 0.15)
    if prem_std < 0.01:
        prem_std = 0.10
    return round((premium_now - prem_avg) / prem_std, 2)


def calc_ev(win_prob: float, avg_win: float, avg_loss: float,
            trade_size_usd: float = 50.0) -> dict:
    """EV = P(win)*avg_win - P(loss)*avg_loss - costs"""
    loss_prob  = 1 - win_prob
    gross_win  = win_prob  * max(avg_win  - TOTAL_COST_PCT, 0)
    gross_loss = loss_prob * max(TOTAL_COST_PCT - avg_loss, 0)
    ev_pct     = gross_win - gross_loss - TOTAL_COST_PCT / 2
    ev_usd     = ev_pct / 100 * trade_size_usd

    return {
        "ev_pct":       round(ev_pct, 4),
        "ev_usd":       round(ev_usd, 3),
        "win_prob":     round(win_prob, 3),
        "positive":     ev_pct > 0,
    }


# ════════════════════════════════════════════════════════════════
# ПРЕДИКТОР
# ════════════════════════════════════════════════════════════════

class RampPredictor:
    def __init__(self, database: RampDB):
        self.db = database

    def predict(
        self,
        symbol:           str,
        exchange:         str,
        oi_now:           float,
        premium_now:      float,
        oi_velocity:      float,
        funding_rate_pct: float,
        ttf_minutes:      float = 55.0,
        utc_hour:         int   = -1,
    ) -> Optional[RampPrediction]:

        # Фильтры
        if funding_rate_pct >= -0.00001: return None
        if premium_now >= -0.01: return None

        st = self.db.symbol_stats(symbol, exchange)
        if not st or st["n"] < 2: return None

        perf     = self.db.performance(symbol, exchange)
        now_h    = utc_hour if utc_hour >= 0 else datetime.now(timezone.utc).hour
        is_peak  = now_h in PEAK_HOURS
        evidence = []
        score    = 0.0

        # 1. Z-score premium (0.30)
        z = calc_premium_zscore(premium_now, st)
        if z <= -2.0:
            score += 0.30; evidence.append(f"Premium Z={z:.2f} 🔥"); prem_zone = "DEEP_ENTRY"
        elif z <= -1.5:
            score += 0.22; evidence.append(f"Premium Z={z:.2f} ✅"); prem_zone = "ENTRY"
        elif z <= -1.0:
            score += 0.12; evidence.append(f"Premium Z={z:.2f} ⚡"); prem_zone = "EARLY"
        elif z > 0:
            return None
        else:
            score += 0.05; prem_zone = "NEUTRAL"

        # 2. Gate rate (0.28)
        if funding_rate_pct < -0.005:
            score += 0.28; evidence.append(f"Gate rate {funding_rate_pct:+.5f}%/ч 🔥")
        elif funding_rate_pct < -0.001:
            score += 0.20; evidence.append(f"Gate rate {funding_rate_pct:+.5f}%/ч ✅")

        # 3. OI velocity (0.22)
        vel_avg = st.get("vel_avg", 12)
        if 8 <= oi_velocity <= 30:
            score += 0.22 if oi_velocity >= vel_avg * 0.8 else 0.14
            evidence.append(f"OI vel {oi_velocity:+.1f}%/ч ✅")
        elif 4 <= oi_velocity < 8:
            score += 0.08

        # 4. OI level (0.10)
        oi_thr = st["oi_avg"]
        if oi_now >= oi_thr * 0.7:
            score += 0.10; evidence.append(f"OI ${oi_now/1e6:.2f}M ≥ порог ✅")
        elif oi_now >= oi_thr * 0.4:
            score += 0.04

        # 5. TTF Bonus (0.10)
        if ttf_minutes <= 12:
            score += 0.10; evidence.append(f"⏰ До выплаты {ttf_minutes:.0f} мин! 🔥")
        elif ttf_minutes <= 25:
            score += 0.06

        if is_peak: score += 0.04

        # EV расчет
        win_prob = perf.hit_rate if perf and perf.n_total >= 3 else 0.57
        avg_win  = st.get("spread_avg", 2.5)
        avg_loss = TOTAL_COST_PCT * 1.5
        ev_data = calc_ev(win_prob, avg_win, avg_loss)
        
        if not ev_data["positive"] and score < 0.75: return None
        evidence.append(f"EV = {ev_data['ev_pct']:+.4f}% ✅")

        level = "HIGH" if score >= 0.65 else "MEDIUM" if score >= 0.45 else "WATCH" if score >= 0.30 else None
        if not level: return None

        return RampPrediction(
            symbol=symbol, exchange=exchange, confidence=round(score, 3), level=level,
            ev_pct=ev_data["ev_pct"], win_prob=win_prob, avg_win_pct=avg_win, avg_loss_pct=avg_loss,
            premium_z=z, premium_now=premium_now, oi_velocity=oi_velocity,
            gate_rate_pct=funding_rate_pct, ttf_minutes=ttf_minutes,
            historical_ramps=st["n"], hit_rate=win_prob,
            profit_factor=perf.profit_factor if perf else 0.0,
            avg_max_spread=st["spread_avg"], avg_duration_h=st["dur_avg"],
            oi_now=oi_now, oi_threshold=oi_thr, premium_zone=prem_zone,
            evidence=evidence, is_peak_hour=is_peak, ts=time.time(),
        )

    def predict_all(self, current: dict) -> list[RampPrediction]:
        preds = []
        for sym in self.db.all_symbols():
            d = current.get(sym)
            if not d: continue
            p = self.predict(
                sym, d.get("exchange", "gate"), d.get("oi", 0), d.get("premium", 0),
                d.get("velocity", 0), d.get("funding_rate_pct", 0), d.get("ttf_minutes", 55)
            )
            if p: preds.append(p)
        return sorted(preds, key=lambda x: (x.ev_pct, x.confidence), reverse=True)


predictor = RampPredictor(db)


# ════════════════════════════════════════════════════════════════
# ИНТЕГРАЦИЯ (ОБРАТНАЯ СОВМЕСТИМОСТЬ)
# ════════════════════════════════════════════════════════════════

def record_ramp(symbol: str, exchange: str, gate_rate_pct: float,
                oi_now: float, premium: float, oi_velocity: float,
                source: str = "auto", ts_start: float = None) -> int:
    if not db._c: db.connect()
    e = RampEvent(
        symbol=symbol, exchange=exchange, ts_start=ts_start or time.time(), ts_end=0,
        oi_at_start=oi_now, oi_at_peak=0, premium_at_start=premium, premium_at_peak=0,
        funding_rate_pct=gate_rate_pct, oi_velocity_1h=oi_velocity,
        max_spread=0, duration_h=0, source=source,
    )
    return db.save_ramp(e)

def record_ramp_from_gate_signal(symbol: str, gate_rate_pct: float,
                                  oi_now: float, premium_gate: float,
                                  oi_velocity: float,
                                  source: str = "gate_radar") -> int:
    return record_ramp(symbol, "gate", gate_rate_pct, oi_now, premium_gate, oi_velocity, source)

def record_ramp_from_oi_alert(signal, source: str = "oi_alert") -> int:
    ts = getattr(signal, 'ts', time.time())
    return record_ramp(signal.symbol, signal.exchange, 0, signal.oi_now, signal.premium, signal.oi_delta_1h, source, ts_start=ts)

def save_oi_snap(symbol: str, oi_usdt: float, premium: float,
                 funding_pct: float, exchange: str = "gate"):
    if not db._c: db.connect()
    db.save_snap(OISnapshot(symbol, exchange, time.time(), oi_usdt, premium, funding_pct))

def save_oi_snapshot_from_gate(symbol: str, oi_usdt: float,
                                premium: float, funding_pct: float,
                                exchange: str = "gate"):
    save_oi_snap(symbol, oi_usdt, premium, funding_pct, exchange)

def close_ramp(event_id: int, oi_peak: float, prem_peak: float,
               max_spread: float, mfe_pct: float = 0.0,
               mae_pct: float = 0.0, time_to_target_h: float = 0.0):
    if not db._c: db.connect()
    db.close_ramp(event_id, time.time(), oi_peak, prem_peak, max_spread, mfe_pct, mae_pct, time_to_target_h)


# ════════════════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ
# ════════════════════════════════════════════════════════════════

def fmt_prediction(p: RampPrediction) -> str:
    icons = {"HIGH": "🔥🔥", "MEDIUM": "🔥", "WATCH": "⚡"}
    icon  = icons.get(p.level, "📊")
    bar   = "█" * int(p.confidence * 10) + "░" * (10 - int(p.confidence * 10))
    now_s = datetime.now(timezone.utc).strftime("%H:%M UTC")

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{icon} ПРЕДСКАЗАНИЕ [{p.symbol}]  {now_s}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Уверенность: [{bar}] {p.confidence*100:.0f}%  {p.level}",
        f"",
        f"💰 ОЖИДАЕМАЯ ЦЕННОСТЬ (EV):",
        f"   EV = {p.ev_pct:+.4f}%  (${p.ev_pct/100*50:+.2f} на $50)",
        f"   P(win)={p.win_prob*100:.0f}%  avg_win={p.avg_win_pct:+.2f}%",
        f"",
        f"📊 Сигналы:",
        f"   Premium:  {p.premium_now:+.3f}%  Z={p.premium_z:+.2f}",
        f"   Gate:     {p.gate_rate_pct:+.5f}%/ч",
        f"   OI vel:   {p.oi_velocity:+.1f}%/ч",
        f"   TTF:      {p.ttf_minutes:.0f} мин до выплаты",
        f"",
        f"🔍 Доказательства:",
    ]
    for ev in p.evidence: lines.append(f"   • {ev}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)

def fmt_db_stats() -> str:
    if not db._c: db.connect()
    syms = db.all_symbols()
    if not syms: return "📚 База разгонов пуста."
    lines = [
        "📚 БАЗА РАЗГОНОВ", f"{'─'*56}",
        f"{'Монета':8} {'N':4} {'HR%':6} {'PF':6} {'EV%':8} {'Ср.спред':10}",
        f"{'─'*56}",
    ]
    for sym in sorted(syms):
        st = db.symbol_stats(sym); perf = db.performance(sym)
        if not st: continue
        hr = f"{perf.hit_rate*100:.0f}%" if perf else "—"
        pf = f"{perf.profit_factor:.2f}" if perf else "—"
        lines.append(f"{sym:8} {st['n']:4} {hr:6} {pf:6} {st['spread_avg']:>+8.2f}%")
    return "\n".join(lines)

format_prediction = fmt_prediction
format_db_stats = fmt_db_stats


# ════════════════════════════════════════════════════════════════
# TELEGRAM КОМАНДА
# ════════════════════════════════════════════════════════════════

async def cmd_memory(update, ctx):
    if not db._c: db.connect()
    args = ctx.args or []
    arg  = args[0].upper() if args else ""

    if not arg:
        await update.effective_message.reply_text(fmt_db_stats())
    elif arg == "PREDICT":
        current = {}
        try:
            from radar.gate_ramp_radar import _st
            now_ts = time.time()
            for sym in _st.syms():
                c = _st.contracts.get(sym); h = _st.history(sym)
                if not c or not h: continue
                cur = h[-1]; old = next((p for p in reversed(h) if p.ts <= cur.ts - 3600), None)
                vel = ((cur.oi_gate - old.oi_gate) / old.oi_gate * 100 if old and old.oi_gate > 0 else 0)
                ttf = 60 - (now_ts % 3600) / 60
                current[sym] = {"oi":cur.oi_gate, "premium":cur.prem_gate, "velocity":vel, "funding_rate_pct":c.funding_rate_pct, "ttf_minutes":ttf}
        except: pass
        preds = predictor.predict_all(current)
        if not preds:
            await update.effective_message.reply_text("Нет предсказаний с положительным EV.")
            return
        for p in preds[:3]: await update.effective_message.reply_text(fmt_prediction(p))
    elif arg == "ADD":
        if len(args) < 5: return
        sym = args[1].upper(); ex = args[2].lower(); prem = float(args[3]); oi_m = float(args[4]) * 1e6
        eid = record_ramp(sym, ex, -0.003, oi_m, prem, 12.0, "manual")
        close_ramp(eid, oi_m*2, prem*3, abs(prem)*4)
        await update.effective_message.reply_text(f"✅ Записан разгон {sym}")
    else:
        st = db.symbol_stats(arg); ramps = db.get_ramps(arg)
        if not ramps: await update.effective_message.reply_text(f"❌ {arg}: нет данных"); return
        lines = [f"📚 {arg} ({len(ramps)} разгонов)", f"{'─'*30}"]
        if st: lines.append(f"Ср. спред: {st['spread_avg']:+.2f}%")
        await update.effective_message.reply_text("\n".join(lines))

if __name__ == "__main__":
    db.connect()
    print("Ramp Memory Upgraded.")
