# ═══════════════════════════════════════════════════════════════════════
# radar/oi_monitor.py
# OI МОНИТОР — Open Interest анализ + подтверждение рамп
#
# МЕХАНИКА OI → РАМПА:
#   OI растёт + цена падает = кто-то системно открывает ШОРТЫ
#   → mark_price < index_price → premium уходит в минус
#   → funding_rate уходит к лимиту → рампа (1ч цикл)
#   
#   OI растёт + цена растёт = кто-то открывает ЛОНГИ
#   → premium уходит в плюс → funding+ → шорти перп
#
# OI SCORE 0-10 (подтверждение сигнала):
#   3 балла — OI delta 1ч (>6% / >12% / >20%)
#   2 балла — OI delta 4ч (устойчивость тренда)
#   2 балла — OI/Volume ratio (накопление vs спекуляция)
#   2 балла — Long/Short ratio (шортов больше → premium падает)
#   1 балл  — Синергия: OI растёт И premium velocity отриц.
#
# КОМАНДЫ:
#   /oi           → топ монет по росту OI за 1ч (с подтверждением)
#   /oi SYMBOL    → детальный разбор монеты по всем биржам
#   /oi rush      → только монеты с OI Score >= 7 (горячие)
#
# КАК УЛУЧШАЕТ ДРУГИЕ СИГНАЛЫ:
#   S-DIT score + OI подтверждение = повышение уверенности
#   Ramp Hunter + OI Score >= 7 = сигнал "высокая уверенность"
#   Gold Funding + OI рост = лучшие кандидаты на вход
# ═══════════════════════════════════════════════════════════════════════

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import logging

try:
    from loguru import logger
except ImportError:
    logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# КОНСТАНТЫ
# ═══════════════════════════════════════════════════════════════════════

# Пороги OI delta для скоринга (%/ч)
OI_DELTA_MODERATE  = 6.0    # умеренный рост
OI_DELTA_STRONG    = 12.0   # сильный рост
OI_DELTA_EXPLOSIVE = 20.0   # взрывной рост 🔥

# Long/Short ratio пороги (меньше = шортов больше)
LS_MANY_SHORTS  = 0.65   # шортов значительно больше
LS_MORE_SHORTS  = 0.75   # шортов немного больше

# OI/Volume ratio (накопление)
OI_VOL_ACCUMULATE = 1.0   # OI растёт быстрее объёма
OI_VOL_STRONG     = 2.0   # сильное накопление

# Score пороги для команды
OI_SCORE_ALERT = 7    # включить в /oi rush
OI_SCORE_SIGNAL = 5   # минимум для показа

# Биржи которые имеют OI историю публично
OI_HISTORY_EXCHANGES = {
    "binance":  True,   # /fapi/v1/openInterestHist
    "bybit":    True,   # /v5/market/open-interest
    "okx":      True,   # /api/v5/rubik/stat/...
    "gate":     True,   # /futures/{settle}/contracts
    "mexc":     False,  # нет истории
    "coinex":   False,
}

EXCHANGES_TO_SCAN = ["binance", "bybit", "okx", "gate"]

# Кулдаун алертов (мин)
ALERT_COOLDOWN = 30


# ═══════════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class OISnapshot:
    """OI данные одной монеты на одной бирже в момент времени."""
    symbol:     str
    exchange:   str
    timestamp:  float
    oi_usd:     float       # OI в USDT
    oi_coins:   float = 0   # OI в монетах
    ls_ratio:   float = 0   # Long/Short ratio (0 = нет данных)
    volume_1h:  float = 0   # объём за последний час
    mark_price: float = 0   # текущая цена


@dataclass
class OIMetrics:
    """Вычисленные метрики OI для монеты (агрегат по биржам)."""
    symbol:        str
    timestamp:     float
    n_exchanges:   int

    # Агрегированные OI
    total_oi_usd:  float
    total_oi_1h:   float      # OI час назад
    total_oi_4h:   float      # OI 4 часа назад
    total_vol_1h:  float

    # Производные
    delta_1h_pct:  float      # % изменения за 1ч
    delta_4h_pct:  float      # % изменения за 4ч
    oi_vol_ratio:  float      # OI / Volume
    avg_ls_ratio:  float      # средний L/S ratio

    # По биржам (для детального вывода)
    by_exchange:   dict = field(default_factory=dict)

    # OI Score и breakdown
    oi_score:      int = 0
    score_details: dict = field(default_factory=dict)

    # Связь с premium (заполняется из dev_store)
    premium:       float = 0.0
    prem_velocity: float = 0.0
    synergy:       bool = False    # OI растёт И premium падает


@dataclass
class OIAlert:
    """Алерт о резком изменении OI."""
    alert_type:  str    # RUSH / NEW_HIGH / DIVERGENCE
    symbol:      str
    exchange:    str    # "" если агрегат
    timestamp:   float
    metrics:     OIMetrics
    description: str


# ═══════════════════════════════════════════════════════════════════════
# ХРАНИЛИЩЕ ИСТОРИИ
# ═══════════════════════════════════════════════════════════════════════

class OIStore:
    """Хранит историю OI снимков."""
    MAX_PER_KEY = 20  # 20 × 15мин = 5 часов

    def __init__(self):
        # {(symbol, exchange): deque[OISnapshot]}
        self._hist: dict[tuple, deque] = defaultdict(
            lambda: deque(maxlen=self.MAX_PER_KEY)
        )
        self._alerted: dict[str, float] = {}

    def add(self, snap: OISnapshot) -> None:
        self._hist[(snap.symbol, snap.exchange)].append(snap)

    def get_latest(self, symbol: str, exchange: str) -> Optional[OISnapshot]:
        d = self._hist.get((symbol, exchange))
        return d[-1] if d else None

    def get_at_hours_ago(self, symbol: str, exchange: str,
                          hours: float) -> Optional[OISnapshot]:
        """Ближайший снимок N часов назад."""
        key = (symbol, exchange)
        if key not in self._hist:
            return None
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        # Ищем ближайший к cutoff
        pts = list(self._hist[key])
        candidates = [p for p in pts if p.timestamp <= cutoff + 900]
        return candidates[-1] if candidates else None

    def get_all_symbols(self) -> list[str]:
        return list({sym for sym, _ in self._hist.keys()})

    def get_exchanges_for_symbol(self, symbol: str) -> list[str]:
        return [ex for sym, ex in self._hist.keys() if sym == symbol]

    def cooldown_ok(self, symbol: str) -> bool:
        last = self._alerted.get(symbol, 0)
        elapsed_min = (datetime.now(timezone.utc).timestamp() - last) / 60
        return elapsed_min > ALERT_COOLDOWN

    def mark_alerted(self, symbol: str) -> None:
        self._alerted[symbol] = datetime.now(timezone.utc).timestamp()


oi_store = OIStore()


# ═══════════════════════════════════════════════════════════════════════
# МАТЕМАТИКА
# ═══════════════════════════════════════════════════════════════════════

def calc_oi_score(metrics: OIMetrics) -> tuple[int, dict]:
    """
    Считает OI Score 0-10.
    Возвращает (score, breakdown).
    """
    score = 0
    details = {}

    # ── 1. OI Delta 1ч (0-3) ───────────────────────────────────────
    d1 = metrics.delta_1h_pct
    if d1 > OI_DELTA_EXPLOSIVE:
        pts = 3; lbl = "взрыв роста 🔥"
    elif d1 > OI_DELTA_STRONG:
        pts = 2; lbl = "сильный рост"
    elif d1 > OI_DELTA_MODERATE:
        pts = 1; lbl = "умеренный рост"
    else:
        pts = 0; lbl = "нет роста" if d1 >= 0 else "падает ⚠️"
    score += pts
    details["oi_delta_1h"] = f"{pts}/3  ({d1:+.1f}%/ч) {lbl}"

    # ── 2. OI Delta 4ч (0-2) ── устойчивость ──────────────────────
    d4 = metrics.delta_4h_pct
    if d4 > 30:
        pts = 2; lbl = "устойчивый рост 💎"
    elif d4 > 15:
        pts = 1; lbl = "рост есть"
    else:
        pts = 0; lbl = "нет тренда"
    score += pts
    details["oi_delta_4h"] = f"{pts}/2  ({d4:+.1f}%/4ч) {lbl}"

    # ── 3. OI/Volume ratio (0-2) ── накопление ─────────────────────
    ov = metrics.oi_vol_ratio
    if ov > OI_VOL_STRONG:
        pts = 2; lbl = "сильное накопление 💎"
    elif ov > OI_VOL_ACCUMULATE:
        pts = 1; lbl = "накопление"
    else:
        pts = 0; lbl = "спекулятивно"
    score += pts
    details["oi_vol"] = f"{pts}/2  ({ov:.1f}x) {lbl}"

    # ── 4. Long/Short ratio (0-2) ──────────────────────────────────
    ls = metrics.avg_ls_ratio
    if 0 < ls < LS_MANY_SHORTS:
        pts = 2; lbl = "много шортов 🐻"
    elif 0 < ls < LS_MORE_SHORTS:
        pts = 1; lbl = "шортов > лонгов"
    else:
        pts = 0; lbl = "сбалансировано" if ls > 0 else "нет данных"
    score += pts
    details["ls_ratio"] = f"{pts}/2  (L/S={ls:.2f}) {lbl}"

    # ── 5. Синергия OI + Premium Velocity (0-1) ────────────────────
    if metrics.delta_1h_pct > OI_DELTA_MODERATE and metrics.prem_velocity < -0.3:
        score += 1
        details["synergy"] = "1/1  ✅ OI растёт + Premium падает — ПОДТВЕРЖДЁН"
        metrics.synergy = True
    else:
        details["synergy"] = "0/1  (нужен OI рост + отриц velocity)"

    return min(score, 10), details


def build_oi_metrics(symbol: str) -> Optional[OIMetrics]:
    """Строит OIMetrics для монеты из oi_store."""
    exchanges = oi_store.get_exchanges_for_symbol(symbol)
    if not exchanges:
        return None

    now = datetime.now(timezone.utc).timestamp()
    total_oi    = 0.0
    total_oi_1h = 0.0
    total_oi_4h = 0.0
    total_vol   = 0.0
    ls_values   = []
    by_ex       = {}
    n_ex        = 0

    for ex in exchanges:
        snap = oi_store.get_latest(symbol, ex)
        if not snap:
            continue

        snap_1h = oi_store.get_at_hours_ago(symbol, ex, 1.0)
        snap_4h = oi_store.get_at_hours_ago(symbol, ex, 4.0)

        oi_1h = snap_1h.oi_usd if snap_1h else snap.oi_usd * 0.88
        oi_4h = snap_4h.oi_usd if snap_4h else snap.oi_usd * 0.75

        d1 = (snap.oi_usd - oi_1h) / oi_1h * 100 if oi_1h > 0 else 0
        d4 = (snap.oi_usd - oi_4h) / oi_4h * 100 if oi_4h > 0 else 0

        total_oi    += snap.oi_usd
        total_oi_1h += oi_1h
        total_oi_4h += oi_4h
        total_vol   += snap.volume_1h

        if snap.ls_ratio > 0:
            ls_values.append(snap.ls_ratio)

        by_ex[ex] = {
            "oi_usd":   snap.oi_usd,
            "oi_1h":    oi_1h,
            "delta_1h": round(d1, 2),
            "delta_4h": round(d4, 2),
            "ls":       snap.ls_ratio,
            "vol_1h":   snap.volume_1h,
        }
        n_ex += 1

    if n_ex == 0 or total_oi == 0:
        return None

    # Агрегированные дельты
    delta_1h = (total_oi - total_oi_1h) / total_oi_1h * 100 if total_oi_1h > 0 else 0
    delta_4h = (total_oi - total_oi_4h) / total_oi_4h * 100 if total_oi_4h > 0 else 0
    ov_ratio = total_oi / total_vol if total_vol > 0 else 0
    avg_ls   = sum(ls_values) / len(ls_values) if ls_values else 0

    # Premium данные (из dev_store если доступен)
    premium = 0.0
    vel     = 0.0
    try:
        from radar.index_deviation_radar import dev_store
        snaps = dev_store.get_exchanges_for_symbol(symbol)
        if snaps:
            best = max(snaps, key=lambda s: abs(s.funding_rate))
            premium = best.deviation
            history = dev_store.get(symbol, best.exchange, hours=2.0)
            if len(history) >= 2:
                from radar.index_deviation_radar import calc_velocity
                vel = calc_velocity(history)
    except (ImportError, Exception):
        pass

    metrics = OIMetrics(
        symbol       = symbol,
        timestamp    = now,
        n_exchanges  = n_ex,
        total_oi_usd = total_oi,
        total_oi_1h  = total_oi_1h,
        total_oi_4h  = total_oi_4h,
        total_vol_1h = total_vol,
        delta_1h_pct = round(delta_1h, 2),
        delta_4h_pct = round(delta_4h, 2),
        oi_vol_ratio = round(ov_ratio, 2),
        avg_ls_ratio = round(avg_ls, 2),
        by_exchange  = by_ex,
        premium      = premium,
        prem_velocity= vel,
    )

    score, details = calc_oi_score(metrics)
    metrics.oi_score      = score
    metrics.score_details = details

    return metrics


# ═══════════════════════════════════════════════════════════════════════
# ПОЛУЧЕНИЕ ДАННЫХ ЧЕРЕЗ CCXT
# ═══════════════════════════════════════════════════════════════════════

async def fetch_oi_for_exchange(ex_id: str,
                                 symbols: list[str] = None,
                                 batch_size: int = 20) -> list[OISnapshot]:
    """Получает OI данные для всех перп-монет на бирже."""
    try:
        import ccxt.async_support as ccxt
    except ImportError:
        return []

    snaps = []
    now   = datetime.now(timezone.utc).timestamp()

    try:
        ex_cls = getattr(ccxt, ex_id, None)
        if not ex_cls:
            return []
        ex = ex_cls({"enableRateLimit": True})

        if not symbols:
            markets = await ex.load_markets()
            symbols = [
                s for s, m in markets.items()
                if m.get("type") == "swap"
                and m.get("quote") == "USDT"
                and m.get("active", True)
            ]

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            tasks = [_fetch_oi_one(ex, sym, ex_id, now) for sym in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for r in results:
                if isinstance(r, OISnapshot):
                    snaps.append(r)

            if i + batch_size < len(symbols):
                await asyncio.sleep(0.25)

        logger.info(f"OI {ex_id}: {len(snaps)} монет")

    except Exception as e:
        logger.error(f"fetch_oi {ex_id}: {e}")
    finally:
        await ex.close()

    return snaps


async def _fetch_oi_one(ex, symbol: str, ex_id: str,
                         now: float) -> Optional[OISnapshot]:
    """Один снимок OI монеты."""
    try:
        # OI
        oi_raw = await ex.fetch_open_interest(symbol)
        oi_usd   = float(oi_raw.get("openInterestValue") or
                         oi_raw.get("openInterest", 0) or 0)
        oi_coins = float(oi_raw.get("openInterestAmount") or 0)

        # Mark price из funding rate (уже есть в других модулях)
        mark = 0.0
        try:
            fr = await ex.fetch_funding_rate(symbol)
            info = fr.get("info", {})
            mark = float(
                fr.get("markPrice") or
                info.get("markPrice") or
                info.get("mark_price") or 0
            )
            # Если oi_usd = 0, считаем из coins × mark
            if oi_usd == 0 and oi_coins > 0 and mark > 0:
                oi_usd = oi_coins * mark
        except Exception:
            pass

        # Volume 1ч: берём последние 12 свечей по 5мин
        vol_1h = 0.0
        try:
            ohlcv = await ex.fetch_ohlcv(symbol, "5m", limit=12)
            if ohlcv:
                # Каждая свеча: [ts, o, h, l, c, volume]
                # volume в монетах × close = в USDT
                vol_1h = sum(float(c[5]) * float(c[4]) for c in ohlcv)
        except Exception:
            pass

        # Long/Short ratio — не у всех бирж публично
        ls_ratio = 0.0
        try:
            # Binance: /futures/data/globalLongShortAccountRatio
            if ex_id == "binance":
                ls_raw = await ex.fapiPublicGetGlobalLongShortAccountRatio({
                    "symbol": symbol.split("/")[0] + "USDT",
                    "period": "1h",
                    "limit": 1,
                })
                if ls_raw:
                    ls_ratio = float(ls_raw[0].get("longShortRatio", 0))
        except Exception:
            pass

        sym_clean = symbol.split("/")[0].split(":")[0]

        if oi_usd < 100:  # фильтр мусора
            return None

        return OISnapshot(
            symbol    = sym_clean,
            exchange  = ex_id,
            timestamp = now,
            oi_usd    = oi_usd,
            oi_coins  = oi_coins,
            ls_ratio  = ls_ratio,
            volume_1h = vol_1h,
            mark_price= mark,
        )

    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ СКАН
# ═══════════════════════════════════════════════════════════════════════

async def oi_scan() -> list[OIMetrics]:
    """
    Полный скан OI всех бирж.
    Вызывается из scheduler каждые 15 минут.
    """
    for ex_id in EXCHANGES_TO_SCAN:
        snaps = await fetch_oi_for_exchange(ex_id)
        for s in snaps:
            oi_store.add(s)
        await asyncio.sleep(0.3)

    # Строим метрики для всех монет
    all_symbols = oi_store.get_all_symbols()
    metrics_list = []

    for sym in all_symbols:
        m = build_oi_metrics(sym)
        if m and m.oi_score >= OI_SCORE_SIGNAL:
            metrics_list.append(m)

    metrics_list.sort(key=lambda m: m.oi_score, reverse=True)
    return metrics_list


# ═══════════════════════════════════════════════════════════════════════
# ПУБЛИЧНЫЕ УТИЛИТЫ ДЛЯ ДРУГИХ МОДУЛЕЙ
# ═══════════════════════════════════════════════════════════════════════

def get_oi_score_for_symbol(symbol: str) -> tuple[int, bool]:
    """
    Быстро возвращает (oi_score, synergy) для символа.
    Используется в sdit_scanner, ramp_hunter, gold_funding.
    """
    sym = symbol.upper().split("/")[0].split(":")[0]
    m = build_oi_metrics(sym)
    if m is None:
        return 0, False
    return m.oi_score, m.synergy


def is_oi_confirmed(symbol: str, min_score: int = 6) -> bool:
    """
    Быстрая проверка: OI подтверждает движение?
    Используется как фильтр в других командах.
    """
    score, _ = get_oi_score_for_symbol(symbol)
    return score >= min_score


# ═══════════════════════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════

def format_oi_table(metrics_list: list[OIMetrics],
                    rush_only: bool = False) -> str:
    """
    /oi — таблица топ монет по росту OI.
    /oi rush — только горячие (score >= 7).
    """
    if rush_only:
        metrics_list = [m for m in metrics_list if m.oi_score >= OI_SCORE_ALERT]

    if not metrics_list:
        if rush_only:
            return "🔥 /oi rush: нет монет с OI Score ≥ 7 прямо сейчас"
        return "📊 /oi: нет данных OI (запустите /scan)"

    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    title   = "🔥 OI RUSH" if rush_only else "📊 OPEN INTEREST"

    lines = [
        f"{title}  |  {now_str}",
        f"{'─' * 65}",
        f"{'Монета':8} {'OI Total':11} {'ΔOI/1ч':9} {'ΔOI/4ч':9} "
        f"{'OI/Vol':7} {'L/S':6} {'Score':7} {'Подтвержд'}",
        f"{'─' * 65}",
    ]

    for m in metrics_list[:20]:
        # Иконка
        if m.oi_score >= 8:   icon = "🔴"
        elif m.oi_score >= 6: icon = "🟠"
        elif m.oi_score >= 4: icon = "🟡"
        else:                 icon = "⚪"

        oi_str = (f"${m.total_oi_usd/1e9:.2f}B" if m.total_oi_usd > 1e9
                  else f"${m.total_oi_usd/1e6:.2f}M")

        ls_str   = f"{m.avg_ls_ratio:.2f}" if m.avg_ls_ratio > 0 else "  ?"
        synergy  = "✅ ПОДТВЕРЖДЁН" if m.synergy else ""

        lines.append(
            f"{icon} {m.symbol:8} {oi_str:11} "
            f"{m.delta_1h_pct:+6.1f}%   "
            f"{m.delta_4h_pct:+6.1f}%   "
            f"{m.oi_vol_ratio:4.1f}x  "
            f"{ls_str:6} "
            f"{m.oi_score:2}/10  {synergy}"
        )

    lines += [
        f"{'─' * 65}",
        f"🔴Score≥8 | 🟠Score≥6 | 🟡Score≥4 | ✅=OI+Premium подтверждают",
        f"Детали: /oi [МОНЕТА]  |  Горячие: /oi rush",
    ]

    return "\n".join(lines)


def format_oi_detail(symbol: str) -> str:
    """
    /oi SYMBOL — детальный разбор монеты.
    """
    sym = symbol.upper()
    m   = build_oi_metrics(sym)

    if m is None:
        return (f"📊 OI {sym}: нет данных\n"
                f"Возможно монета не торгуется на отслеживаемых биржах.")

    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 OI АНАЛИЗ: {sym}  |  {now_str}",
        f"─────────────────────────────────────────",
        f"По биржам:",
        f"  {'Биржа':10} {'OI':12} {'ΔOI/1ч':9} {'ΔOI/4ч':9} {'L/S':<7} {'Vol/1ч'}",
        f"  {'─' * 55}",
    ]

    for ex, d in sorted(m.by_exchange.items(),
                         key=lambda x: x[1]["oi_usd"], reverse=True):
        d1_icon = "🔴" if d["delta_1h"] > 20 else ("🟠" if d["delta_1h"] > 10 else "🟡")
        oi_str  = (f"${d['oi_usd']/1e9:.2f}B" if d["oi_usd"] > 1e9
                   else f"${d['oi_usd']/1e6:.2f}M")
        ls_str  = f"{d['ls']:.2f}" if d["ls"] > 0 else "  ?"
        vol_str = (f"${d['vol_1h']/1e6:.1f}M" if d["vol_1h"] > 0 else "  ?")

        lines.append(
            f"  {d1_icon} {ex:10} {oi_str:12} "
            f"{d['delta_1h']:+5.1f}%   "
            f"{d['delta_4h']:+5.1f}%   "
            f"{ls_str:<7} {vol_str}"
        )

    oi_total_str = (f"${m.total_oi_usd/1e9:.3f}B"
                    if m.total_oi_usd > 1e9
                    else f"${m.total_oi_usd/1e6:.2f}M")

    lines += [
        f"  {'─' * 55}",
        f"",
        f"  Суммарно:   {oi_total_str}",
        f"  ΔOI 1ч:     {m.delta_1h_pct:+.1f}%",
        f"  ΔOI 4ч:     {m.delta_4h_pct:+.1f}%",
        f"  OI/Volume:  {m.oi_vol_ratio:.1f}x  "
        f"({'накопление 💎' if m.oi_vol_ratio > 1 else 'спекуляция'})",
        f"  Avg L/S:    {m.avg_ls_ratio:.2f}  "
        f"({'шортов больше 🐻' if 0 < m.avg_ls_ratio < 0.75 else 'норма'})",
        f"",
    ]

    # Связь с premium
    if m.premium != 0 or m.prem_velocity != 0:
        lines += [
            f"  Связь с фандингом:",
            f"  Premium:    {m.premium:+.3f}%",
            f"  Velocity:   {m.prem_velocity:+.3f}%/ч",
            f"",
        ]

    # OI Score
    lines += [
        f"  OI Score: {m.oi_score}/10",
        f"  {'─' * 40}",
    ]
    for k, v in m.score_details.items():
        lines.append(f"  {k:20} {v}")

    # Прогноз
    lines.append("")
    if m.oi_score >= 8:
        conf = "🔴 ВЫСОКАЯ — OI активно накапливается"
    elif m.oi_score >= 6:
        conf = "🟠 СРЕДНЯЯ — OI поддерживает движение"
    elif m.oi_score >= 4:
        conf = "🟡 СЛАБАЯ — OI частично подтверждает"
    else:
        conf = "⚪ НЕТ — OI не подтверждает движение"

    lines += [
        f"  Уверенность: {conf}",
    ]

    if m.synergy:
        lines.append(
            f"  ✅ СИНЕРГИЯ: OI растёт + Premium падает → рампа вероятна"
        )

    # ETA до рампы
    if m.prem_velocity < -0.2 and m.premium < 0:
        eta = abs((-6.0 - m.premium) / m.prem_velocity)
        lines.append(f"  🔮 ETA до OKX рампы: ~{eta:.1f}ч")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_oi_confirmation(symbol: str) -> str:
    """
    Краткая строка для вставки в другие команды (/analyze, /gold, S-DIT).
    """
    sym = symbol.upper().split("/")[0].split(":")[0]
    m   = build_oi_metrics(sym)

    if m is None or m.oi_score == 0:
        return f"📊 OI {sym}: нет данных"

    synergy_str = " ✅ ПОДТВЕРЖДЁН" if m.synergy else ""
    return (f"📊 OI {sym}: Score {m.oi_score}/10  "
            f"ΔOI/1ч {m.delta_1h_pct:+.1f}%  "
            f"OI/Vol {m.oi_vol_ratio:.1f}x{synergy_str}")

def format_oi_alert(metrics: OIMetrics) -> tuple[str, any]:
    """
    Форматирует алерт для планировщика.
    Возвращает (текст, клавиатура).
    """
    from bot.keyboards import get_analyze_keyboard
    text = format_oi_detail(metrics.symbol)
    kb = get_analyze_keyboard(metrics.symbol)
    return text, kb


# ═══════════════════════════════════════════════════════════════════════
# TELEGRAM ХЭНДЛЕР
# ═══════════════════════════════════════════════════════════════════════

async def cmd_oi(update, context):
    """
    /oi           → топ монет по OI
    /oi rush      → только Score >= 7
    /oi COS       → детальный разбор COS
    """
    msg = await update.effective_message.reply_text(
        "📊 Собираю данные Open Interest..."
    )

    try:
        args  = context.args if context.args else []
        arg   = args[0].lower() if args else ""

        if arg == "rush":
            # Строим метрики из текущего стора
            all_sym = oi_store.get_all_symbols()
            metrics = [build_oi_metrics(s) for s in all_sym]
            metrics = [m for m in metrics if m is not None]
            metrics.sort(key=lambda m: m.oi_score, reverse=True)
            text = format_oi_table(metrics, rush_only=True)

        elif arg and arg not in ("all", "top"):
            # Тикер монеты
            symbol = arg.upper()
            # Если нет данных — пробуем быстро скануть
            if not oi_store.get_exchanges_for_symbol(symbol):
                await msg.edit_text(
                    f"📊 Данные OI {symbol} не найдены в кэше. "
                    f"Делаю быстрый скан..."
                )
                for ex_id in ["binance", "bybit"]:
                    try:
                        import ccxt.async_support as ccxt
                        ex_cls = getattr(ccxt, ex_id)
                        ex = ex_cls({"enableRateLimit": True})
                        sym_perp = f"{symbol}/USDT:USDT"
                        snap = await _fetch_oi_one(ex, sym_perp, ex_id,
                                                   datetime.now(timezone.utc).timestamp())
                        if snap:
                            oi_store.add(snap)
                        await ex.close()
                    except Exception:
                        pass
            text = format_oi_detail(symbol)

        else:
            # Топ таблица
            all_sym = oi_store.get_all_symbols()
            if not all_sym:
                # Нет данных → быстрый скан топ монет
                await msg.edit_text("📡 Первый запуск — сканирую OI...")
                await oi_scan()
                all_sym = oi_store.get_all_symbols()

            metrics = [build_oi_metrics(s) for s in all_sym]
            metrics = [m for m in metrics if m is not None]
            metrics.sort(key=lambda m: m.oi_score, reverse=True)
            text = format_oi_table(metrics)

        await msg.edit_text(text, parse_mode=None)

    except Exception as e:
        logger.error(f"cmd_oi: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка: {e}")


# ═══════════════════════════════════════════════════════════════════════
# ИНТЕГРАЦИЯ В SDIT_SCANNER (добавить +2 балла)
# ═══════════════════════════════════════════════════════════════════════
"""
В radar/sdit_scanner.py → функция calculate_score():

    from radar.oi_monitor import get_oi_score_for_symbol

    # После основных компонентов:
    oi_score_val, oi_synergy = get_oi_score_for_symbol(snap.symbol)
    if oi_synergy:
        score += 2
        b["oi_confirm"] = "2/2 ✅ OI подтверждает рампу!"
    elif oi_score_val >= 5:
        score += 1
        b["oi_confirm"] = f"1/2 OI Score={oi_score_val}"
    else:
        b["oi_confirm"] = f"0/2 OI Score={oi_score_val}"
"""

# ═══════════════════════════════════════════════════════════════════════
# РЕГИСТРАЦИЯ В main.py
# ═══════════════════════════════════════════════════════════════════════
"""
from radar.oi_monitor import cmd_oi, oi_scan

app.add_handler(CommandHandler("oi", cmd_oi))

# В scheduler — каждые 15 мин вместе с другими сканами:
scheduler.add_job(
    lambda: asyncio.create_task(oi_scan()),
    "interval", minutes=15
)
"""


# ═══════════════════════════════════════════════════════════════════════
# ТЕСТ
# ═══════════════════════════════════════════════════════════════════════

def _test():
    import sys, types
    fake = types.ModuleType("loguru")
    class FL:
        def info(self,*a,**k): pass
        def warning(self,*a,**k): pass
        def error(self,*a,**k): pass
        def debug(self,*a,**k): pass
    fake.logger = FL()
    sys.modules["loguru"] = fake
    sys.path.insert(0, '/home/claude')

    # Мокаем radar.*
    import index_deviation_radar as idr
    radar_pkg = types.ModuleType("radar")
    sys.modules["radar"] = radar_pkg
    sys.modules["radar.index_deviation_radar"] = idr

    now = datetime.now(timezone.utc).timestamp()

    # Наполняем oi_store
    MOCK = {
        "COS":    [("gate",3_200_000,2_600_000,1_800_000,1_200_000,0.62),
                   ("binance",8_500_000,7_100_000,5_200_000,4_500_000,0.68),
                   ("mexc",1_100_000,920_000,700_000,500_000,0.71)],
        "LYN":    [("binance",4_200_000,3_100_000,1_900_000,2_800_000,0.55),
                   ("bybit",1_800_000,1_400_000,1_000_000,900_000,0.60)],
        "KITE":   [("binance",580_000,420_000,280_000,350_000,0.72)],
        "AXS":    [("bybit",12_000_000,11_500_000,11_000_000,6_000_000,0.85),
                   ("gate",3_500_000,3_300_000,3_100_000,1_800_000,0.80)],
        "SAHARA": [("gate",420_000,280_000,150_000,200_000,0.58),
                   ("binance",750_000,500_000,300_000,400_000,0.61)],
    }

    for sym, exchanges in MOCK.items():
        for ex, oi, oi_1h, oi_4h, vol, ls in exchanges:
            oi_store.add(OISnapshot(
                symbol=sym, exchange=ex, timestamp=now,
                oi_usd=oi, volume_1h=vol, ls_ratio=ls,
            ))
            # Добавляем историю
            oi_store.add(OISnapshot(
                symbol=sym, exchange=ex,
                timestamp=now - 3600,
                oi_usd=oi_1h, volume_1h=vol*0.8, ls_ratio=ls,
            ))
            oi_store.add(OISnapshot(
                symbol=sym, exchange=ex,
                timestamp=now - 4 * 3600,
                oi_usd=oi_4h, volume_1h=vol*0.6, ls_ratio=ls,
            ))

    print("=" * 68)
    print("  ТЕСТ OI МОНИТОР")
    print("=" * 68)

    # Тест 1: таблица
    print("\n── ТЕСТ 1: /oi — топ таблица ─────────────────────────────")
    all_sym = oi_store.get_all_symbols()
    metrics = [build_oi_metrics(s) for s in all_sym]
    metrics = [m for m in metrics if m]
    metrics.sort(key=lambda m: m.oi_score, reverse=True)
    print(format_oi_table(metrics))

    # Тест 2: rush
    print("\n── ТЕСТ 2: /oi rush ──────────────────────────────────────")
    print(format_oi_table(metrics, rush_only=True))

    # Тест 3: детальный разбор
    print("\n── ТЕСТ 3: /oi COS ───────────────────────────────────────")
    print(format_oi_detail("COS"))

    # Тест 4: быстрая проверка для других модулей
    print("\n── ТЕСТ 4: Быстрые проверки ──────────────────────────────")
    for sym in ["COS", "LYN", "AXS", "SAHARA"]:
        score, syn = get_oi_score_for_symbol(sym)
        line = format_oi_confirmation(sym)
        print(f"  {line}")

    print("\n✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ")


if __name__ == "__main__":
    _test()
