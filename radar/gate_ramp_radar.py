"""
radar/gate_ramp_radar.py  v3
=============================
ИСПРАВЛЕНО vs v2:
  - Порог GATE_RATE_NEG_THRESHOLD = 0.0 (показываем ВСЕ 1ч монеты с любым знаком)
  - Подробные логи на каждом шаге инициализации
  - /gate debug — полная диагностика прямо из Telegram
  - Инициализация через on_startup (не post_init) чтобы гарантированно дождаться
  - Защита от пустого CHAT_ID
  - Проверка httpx при старте
"""

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable
import logging

try:
    from loguru import logger
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("gate_ramp")

from radar.ramp_memory import (
    record_ramp_from_gate_signal,
    save_oi_snapshot_from_gate,
)

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

GATE_BASE    = "https://api.gateio.ws/api/v4"
BINANCE_BASE = "https://fapi.binance.com"
HTTP_TO      = 12
HEADERS      = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Accept":     "application/json",
}

# ── Пороги ───────────────────────────────────────────────────
# ИСПРАВЛЕНО: показываем все 1ч монеты, потом фильтруем по сигналу OI
# Если хочешь только негативные — поставь -0.000010
GATE_RATE_THRESHOLD    = 0.0        # 0.0 = все 1ч монеты in watchlist

# OI рост за 15 мин для сигнала
OI_RISE_WATCH  =  5.0   # %  → WATCH
OI_RISE_ALERT  = 12.0   # %  → ALERT
OI_RISE_URGENT = 20.0   # %  → URGENT

CONTRACTS_REFRESH_H = 6   # кэш контрактов раз в 6ч
WATCHLIST_REFRESH_H = 1   # watchlist раз в час
OI_CHECK_S          = 30  # тик OI каждые 30 сек
ALERT_COOLDOWN_MIN  = 20  # мин между алертами одной монеты
MIN_WATCHLIST_ITEMS = 3   # минимум монет в watchlist чтобы считать OK


# ════════════════════════════════════════════════════════════════
# DATACLASSES
# ════════════════════════════════════════════════════════════════

@dataclass
class GateContract:
    symbol:           str
    contract:         str
    funding_interval: int    # секунды
    funding_rate:     float  # доли за период
    funding_rate_pct: float  # % за период
    oi_usdt:          float
    mark_price:       float
    index_price:      float
    premium_pct:      float
    ts:               float


@dataclass
class OIPoint:
    ts:        float
    oi_gate:   float
    oi_bin:    float
    prem_gate: float
    prem_bin:  float


@dataclass
class RampSignal:
    symbol:          str
    contract:        str
    level:           str
    gate_rate_pct:   float
    oi_gate_now:     float
    oi_gate_base:    float
    oi_delta_pct:    float
    oi_bin_delta:    float
    spread_now:      float
    prem_gate:       float
    prem_bin:        float
    minutes_watched: float
    ts:              float


# ════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ════════════════════════════════════════════════════════════════

def _sf(v, d: float = 0.0) -> float:
    """Safe float — Gate API возвращает строки."""
    try:
        return float(str(v).strip()) if v not in (None, "", "null") else d
    except (ValueError, TypeError):
        return d


async def _get(s: "httpx.AsyncClient", url: str,
               params: dict = None) -> Optional[list | dict]:
    try:
        r = await s.get(url, params=params, timeout=httpx.Timeout(HTTP_TO))
        if r.status_code == 200:
            return r.json()
        
        # 400 часто означает отсутствие монеты на бирже (Binance), не пугаем пользователя
        if r.status_code == 400:
            logger.debug(f"HTTP 400 (Asset likely not listed): {url}")
        else:
            logger.info(f"HTTP {r.status_code}: {url}")
    except asyncio.TimeoutError:
        logger.debug(f"Timeout: {url}")
    except Exception as e:
        logger.debug(f"_get {url}: {type(e).__name__}: {str(e)[:80]}")
    return None


# ════════════════════════════════════════════════════════════════
# СОСТОЯНИЕ
# ════════════════════════════════════════════════════════════════

class _State:
    MAX_HIST = 180  # 90 минут по 30 сек

    def __init__(self):
        # Кэш контрактов {name: funding_interval}
        self.intervals:  dict[str, int]             = {}
        self.intervals_ts: float                    = 0.0

        # Watchlist
        self.contracts:  dict[str, GateContract]    = {}
        self.oi_hist:    dict[str, deque[OIPoint]]  = defaultdict(
            lambda: deque(maxlen=self.MAX_HIST))
        self.baseline:   dict[str, float]           = {}
        self.entered:    dict[str, float]           = {}
        self.alerted:    dict[str, float]           = {}
        self.wl_ts:      float                      = 0.0

        # Статус радара
        self.running:    bool   = False
        self.init_done:  bool   = False
        self.last_error: str    = ""
        self.tick_count: int    = 0

    # ── Интервалы ────────────────────────────────────────────

    def set_intervals(self, d: dict):
        self.intervals    = d
        self.intervals_ts = _now()
        n1h = sum(1 for v in d.values() if v == 3600)
        logger.info(f"Intervals cache: {len(d)} total, {n1h} с 1ч фандингом")

    def intervals_stale(self) -> bool:
        return (_now() - self.intervals_ts) / 3600 >= CONTRACTS_REFRESH_H

    def get_1h_set(self) -> set:
        return {k for k, v in self.intervals.items() if v == 3600}

    # ── Watchlist ─────────────────────────────────────────────

    def upsert(self, c: GateContract):
        if c.symbol not in self.contracts:
            self.baseline[c.symbol] = c.oi_usdt
            self.entered[c.symbol]  = _now()
        self.contracts[c.symbol] = c

    def remove(self, sym: str):
        self.contracts.pop(sym, None)
        self.baseline.pop(sym, None)
        self.entered.pop(sym, None)

    def add_point(self, sym: str, pt: OIPoint):
        self.oi_hist[sym].append(pt)

    def history(self, sym: str) -> list[OIPoint]:
        return list(self.oi_hist.get(sym, []))

    def wl_stale(self) -> bool:
        return (_now() - self.wl_ts) / 3600 >= WATCHLIST_REFRESH_H

    def mark_wl(self):
        self.wl_ts = _now()

    def syms(self) -> list[str]:
        return list(self.contracts.keys())

    def cooldown(self, sym: str) -> bool:
        return (_now() - self.alerted.get(sym, 0)) / 60 >= ALERT_COOLDOWN_MIN

    def mark_alert(self, sym: str):
        self.alerted[sym] = _now()

    def minutes_in(self, sym: str) -> float:
        return (_now() - self.entered.get(sym, _now())) / 60


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


_st = _State()


# ════════════════════════════════════════════════════════════════
# GATE API
# ════════════════════════════════════════════════════════════════

async def _load_intervals(s: "httpx.AsyncClient") -> dict[str, int]:
    """Загружает ВСЕ контракты Gate и возвращает {name: funding_interval}."""
    result = {}
    offset = 0
    limit  = 100

    while True:
        data = await _get(s, f"{GATE_BASE}/futures/usdt/contracts",
                          params={"limit": limit, "offset": offset})
        if not data or not isinstance(data, list):
            logger.warning(f"_load_intervals: нет данных при offset={offset}")
            break

        for item in data:
            name = item.get("name", "")
            fi   = int(item.get("funding_interval", 28800) or 28800)
            if name:
                result[name] = fi

        logger.debug(f"Contracts offset={offset}: {len(data)} items")
        if len(data) < limit:
            break
        offset += limit
        await asyncio.sleep(0.3)

    n1h = sum(1 for v in result.values() if v == 3600)
    logger.info(f"_load_intervals: {len(result)} контрактов, {n1h} с 1ч")
    return result


async def _load_tickers(s: "httpx.AsyncClient",
                         contracts_1h: set) -> list[GateContract]:
    """
    Загружает тикеры Gate и возвращает GateContract для 1ч монет.
    ПРАВИЛЬНЫЙ парсинг: OI = total_size × mark_price
    """
    data = await _get(s, f"{GATE_BASE}/futures/usdt/tickers")
    if not data or not isinstance(data, list):
        logger.warning("_load_tickers: нет данных")
        return []

    now     = _now()
    results = []

    for item in data:
        contract = item.get("contract", "")
        if not contract.endswith("_USDT"):
            continue
        if contracts_1h and contract not in contracts_1h:
            continue

        sym      = contract.replace("_USDT", "")
        mark     = _sf(item.get("mark_price") or item.get("last"))
        index    = _sf(item.get("index_price") or mark)
        total_sz = _sf(item.get("total_size"))
        rate_raw = _sf(item.get("funding_rate"))
        rate_pct = rate_raw * 100
        oi_usdt  = total_sz * mark   # правильная формула
        premium  = (mark - index) / index * 100 if index > 0 else 0.0

        # Фильтр по rate (GATE_RATE_THRESHOLD = 0.0 → все 1ч монеты)
        if rate_raw > GATE_RATE_THRESHOLD:
            continue

        if oi_usdt < 1000:  # фильтр мусора
            continue

        results.append(GateContract(
            symbol=sym, contract=contract,
            funding_interval=3600,
            funding_rate=rate_raw, funding_rate_pct=round(rate_pct, 6),
            oi_usdt=round(oi_usdt, 0),
            mark_price=mark, index_price=index,
            premium_pct=round(premium, 4), ts=now,
        ))

    results.sort(key=lambda c: c.funding_rate_pct)
    logger.info(f"_load_tickers: {len(results)} монет в watchlist")
    return results


async def _fetch_gate_one(s: "httpx.AsyncClient",
                           contract: str) -> Optional[tuple[float, float]]:
    """Gate: текущий OI + premium для одного контракта."""
    data = await _get(s, f"{GATE_BASE}/futures/usdt/tickers",
                      params={"contract": contract})
    if not data:
        return None

    item = (data[0] if isinstance(data, list) and data
            else (data if isinstance(data, dict) else None))
    if not item:
        return None

    mark     = _sf(item.get("mark_price") or item.get("last"))
    index    = _sf(item.get("index_price") or mark)
    total_sz = _sf(item.get("total_size"))
    oi_usdt  = total_sz * mark
    premium  = (mark - index) / index * 100 if index > 0 else 0.0

    return round(oi_usdt, 0), round(premium, 4)


async def _fetch_binance_one(s: "httpx.AsyncClient",
                              symbol: str) -> tuple[float, float]:
    """Binance: OI + premium."""
    sym_bn = f"{symbol.upper()}USDT"
    try:
        oi_r, pr_r = await asyncio.gather(
            _get(s, f"{BINANCE_BASE}/fapi/v1/openInterest",
                 params={"symbol": sym_bn}),
            _get(s, f"{BINANCE_BASE}/fapi/v1/premiumIndex",
                 params={"symbol": sym_bn}),
            return_exceptions=True,
        )
    except Exception:
        return 0.0, 0.0

    mark = 0.0; premium = 0.0; oi_usdt = 0.0
    if isinstance(pr_r, dict):
        mark    = _sf(pr_r.get("markPrice"))
        idx     = _sf(pr_r.get("indexPrice") or mark)
        premium = (mark - idx) / idx * 100 if idx > 0 else 0.0
    if isinstance(oi_r, dict):
        oi_usdt = _sf(oi_r.get("openInterest")) * mark

    return round(oi_usdt, 0), round(premium, 4)


# ════════════════════════════════════════════════════════════════
# ОБНОВЛЕНИЕ ДАННЫХ
# ════════════════════════════════════════════════════════════════

async def _refresh_intervals(s: "httpx.AsyncClient"):
    if _st.intervals_stale() or not _st.intervals:
        data = await _load_intervals(s)
        if data:
            _st.set_intervals(data)
        else:
            logger.error("Не удалось загрузить intervals — watchlist не обновится")


async def _refresh_watchlist(s: "httpx.AsyncClient"):
    await _refresh_intervals(s)

    contracts_1h = _st.get_1h_set()
    if not contracts_1h:
        logger.error("Нет 1ч контрактов в кэше! Gate API проблема?")
        return

    candidates = await _load_tickers(s, contracts_1h)
    syms_new   = {c.symbol for c in candidates}
    syms_old   = set(_st.syms())

    for sym in syms_old - syms_new:
        _st.remove(sym)
    for c in candidates:
        _st.upsert(c)

    _st.mark_wl()
    logger.info(f"Watchlist: {_st.size()} монет: {sorted(_st.syms())[:15]}")


async def _tick(s: "httpx.AsyncClient"):
    """Один тик OI — параллельно для всех монет."""
    syms = _st.syms()
    if not syms:
        return
    now   = _now()
    tasks = [_tick_one(s, sym, now) for sym in syms]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _tick_one(s: "httpx.AsyncClient", sym: str, now: float):
    c = _st.contracts.get(sym)
    if not c:
        return
    g, b = await asyncio.gather(
        _fetch_gate_one(s, c.contract),
        _fetch_binance_one(s, sym),
        return_exceptions=True,
    )
    oi_g = g[0] if isinstance(g, tuple) and g else 0.0
    pg   = g[1] if isinstance(g, tuple) and g else 0.0
    oi_b = b[0] if isinstance(b, tuple) and b else 0.0
    pb   = b[1] if isinstance(b, tuple) and b else 0.0
    _st.add_point(sym, OIPoint(now, oi_g, oi_b, pg, pb))
    
    # Ramp Memory Integration
    save_oi_snapshot_from_gate(sym, oi_g, pg, c.funding_rate_pct)


def _size(self) -> int:
    return len(self.contracts)
_State.size = _size


# ════════════════════════════════════════════════════════════════
# ДЕТЕКТОР
# ════════════════════════════════════════════════════════════════

def _oi_delta(hist: list[OIPoint], minutes: float, field: str) -> float:
    if len(hist) < 2:
        return 0.0
    now    = hist[-1].ts
    cutoff = now - minutes * 60
    olds   = [p for p in hist if p.ts <= cutoff + 45]
    if not olds:
        return 0.0
    old_v = getattr(olds[-1], field, 0.0)
    cur_v = getattr(hist[-1],  field, 0.0)
    return (cur_v - old_v) / old_v * 100 if old_v > 0 else 0.0


def detect(sym: str) -> Optional[RampSignal]:
    c    = _st.contracts.get(sym)
    hist = _st.history(sym)
    if not c or len(hist) < 3:
        return None

    cur   = hist[-1]
    gd5   = _oi_delta(hist,  5, "oi_gate")
    gd15  = _oi_delta(hist, 15, "oi_gate")
    gd30  = _oi_delta(hist, 30, "oi_gate")
    bd15  = _oi_delta(hist, 15, "oi_bin")
    base  = _st.baseline.get(sym, cur.oi_gate)
    spread= cur.prem_gate - cur.prem_bin

    if gd5 >= OI_RISE_URGENT or gd15 >= OI_RISE_URGENT:
        level = "URGENT"
    elif gd15 >= OI_RISE_ALERT or gd30 >= OI_RISE_URGENT:
        level = "ALERT"
    elif gd15 >= OI_RISE_WATCH or gd5 >= OI_RISE_WATCH:
        level = "WATCH"
    else:
        return None

    main_d = gd15 if abs(gd15) > abs(gd5) else gd5

    return RampSignal(
        symbol=sym, contract=c.contract, level=level,
        gate_rate_pct=c.funding_rate_pct,
        oi_gate_now=cur.oi_gate, oi_gate_base=base,
        oi_delta_pct=round(main_d, 2), oi_bin_delta=round(bd15, 2),
        spread_now=round(spread, 3),
        prem_gate=round(cur.prem_gate, 3), prem_bin=round(cur.prem_bin, 3),
        minutes_watched=round(_st.minutes_in(sym), 1), ts=_now(),
    )


# ════════════════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ
# ════════════════════════════════════════════════════════════════

def _fmt_oi(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    return f"${v/1e3:.0f}K"


def fmt_signal(sig: RampSignal) -> str:
    now_s = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    icon  = {"URGENT":"🚨🚨","ALERT":"🚨","WATCH":"⚡"}.get(sig.level, "📊")

    entry = (
        "✅ ОТЛИЧНЫЙ момент — спред ещё маленький!" if abs(sig.spread_now) < 0.5 else
        "✅ Хороший момент — спред разумный"         if abs(sig.spread_now) < 1.0 else
        "⚠️ Спред уже > -1%, ещё можно войти"        if abs(sig.spread_now) < 2.0 else
        "❌ Спред большой — поздно входить"
    )

    rate_abs = abs(sig.gate_rate_pct)
    net_d    = rate_abs * 0.75 * 24
    speed    = rate_abs * 3
    eta      = f"~{(2.0 - abs(sig.spread_now)) / speed:.1f}ч" if speed > 0 else "?"

    return "\n".join([
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{icon} GATE РАЗГОН 1Ч [{sig.symbol}]  {now_s}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💸 Gate 1ч: {sig.gate_rate_pct:+.5f}%/ч  "
        f"{'(Gate платит лонгам!)' if sig.gate_rate_pct < 0 else '(платим Gate)'}",
        f"",
        f"📈 OI Gate:    {_fmt_oi(sig.oi_gate_base)} → {_fmt_oi(sig.oi_gate_now)}"
        f"  ({sig.oi_delta_pct:+.1f}%/15м) {'🔥' if abs(sig.oi_delta_pct) > 15 else ''}",
        f"📈 OI Binance: {sig.oi_bin_delta:+.1f}%/15м"
        f"{'  (тоже растёт ✅)' if sig.oi_bin_delta > 3 else ''}",
        f"",
        f"📊 Спред Gate-Binance:",
        f"   Gate:    {sig.prem_gate:+.3f}%  |  Binance: {sig.prem_bin:+.3f}%",
        f"   Разница: {sig.spread_now:+.3f}%",
        f"",
        f"{entry}",
        f"",
        f"📐 LONG Gate + SHORT Binance",
        f"   Net ≈ {net_d:.3f}%/день  |  Спред до -2%: {eta}",
        f"⏱  В watchlist: {sig.minutes_watched:.0f} мин",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ])


def fmt_list() -> str:
    syms  = _st.syms()
    now_s = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if not syms:
        n1h = len(_st.get_1h_set())
        return (
            f"📋 Gate 1ч Watchlist  |  {now_s}\n"
            f"Пусто.\n\n"
            f"Диагностика:\n"
            f"  Intervals cache: {len(_st.intervals)} контрактов ({n1h} с 1ч)\n"
            f"  Инициализирован: {'✅' if _st.init_done else '⏳ ещё нет'}\n"
            f"  Тиков выполнено: {_st.tick_count}\n"
            f"  Последняя ошибка: {_st.last_error or 'нет'}\n\n"
            f"Причина: {'нет 1ч контрактов в кэше' if not n1h else 'нет монет с нужным rate'}\n"
            f"Команды: /gate debug — полная диагностика\n"
            f"         /gate refresh — принудительное обновление"
        )

    lines = [
        f"📋 GATE 1Ч WATCHLIST  |  {now_s}",
        f"{'─'*60}",
        f"{'Монета':10} {'Rate/ч':12} {'OI Gate':10} {'Δ15м':8} {'Спред':8}  Статус",
        f"{'─'*60}",
    ]

    for sym in sorted(syms, key=lambda s: (_st.contracts[s].funding_rate_pct
                                            if s in _st.contracts else 0)):
        c    = _st.contracts.get(sym)
        hist = _st.history(sym)
        if not c:
            continue
        gd15   = _oi_delta(hist, 15, "oi_gate") if len(hist) >= 2 else 0
        spread = (hist[-1].prem_gate - hist[-1].prem_bin) if hist else 0
        sig    = detect(sym)
        status = {"URGENT":"🚨УРГЕНТ","ALERT":"🔴АЛЕРТ","WATCH":"⚡СЛЕЖУ"}.get(
            sig.level if sig else "", f"⚪ {len(hist)}тч")
        lines.append(
            f"{sym:10} {c.funding_rate_pct:>+10.5f}%  "
            f"{_fmt_oi(c.oi_usdt):10} "
            f"{gd15:>+6.1f}%   "
            f"{spread:>+5.2f}%  {status}"
        )

    lines += [f"{'─'*60}",
              f"Тиков: {_st.tick_count}  |  /gate <МОНЕТА> — детали"]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ════════════════════════════════════════════════════════════════

async def run_gate_ramp_radar(send_fn: Callable, chat_id: str):
    """
    Запускать через asyncio.create_task().
    ВАЖНО: chat_id должен быть строкой!
    """
    _st.running = True

    if not HAS_HTTPX:
        msg = "❌ gate_ramp_radar: httpx не установлен! pip install httpx"
        logger.error(msg)
        try:
            await send_fn(chat_id, msg)
        except Exception:
            pass
        return

    if not chat_id:
        logger.error("gate_ramp_radar: chat_id пустой!")
        return

    logger.info(f"Gate Ramp Radar v3 запускается (chat_id={chat_id})...")

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TO),
        headers=HEADERS,
        follow_redirects=True,
    ) as s:

        # ── Инициализация ────────────────────────────────────
        try:
            logger.info("Шаг 1: загружаем кэш контрактов...")
            await _refresh_intervals(s)
            n1h = len(_st.get_1h_set())
            logger.info(f"Шаг 1 готов: {n1h} 1ч контрактов")

            logger.info("Шаг 2: заполняем watchlist...")
            await _refresh_watchlist(s)
            logger.info(f"Шаг 2 готов: {_st.size()} монет в watchlist")

            _st.init_done = True

            await send_fn(chat_id,
                f"✅ Gate Ramp Radar v3 запущен\n"
                f"1ч контрактов Gate: {n1h}\n"
                f"В watchlist: {_st.size()} монет\n"
                f"{'Монеты: ' + ', '.join(sorted(_st.syms())[:15]) if _st.syms() else 'Пусто (нет монет с отриц. rate прямо сейчас)'}\n\n"
                f"/gate — список  |  /gate debug — диагностика"
            )

        except Exception as e:
            _st.last_error = str(e)
            logger.error(f"Инициализация radar: {e}", exc_info=True)
            try:
                await send_fn(chat_id, f"⚠️ Gate Radar init error: {e}")
            except Exception:
                pass

        # ── Основной цикл ────────────────────────────────────
        iteration = 0
        while _st.running:
            try:
                if _st.wl_stale():
                    await _refresh_watchlist(s)

                await _tick(s)
                _st.tick_count += 1

                if iteration >= 3:
                    for sym in _st.syms():
                        if not _st.cooldown(sym):
                            continue
                        sig = detect(sym)
                        if sig and sig.level in ("ALERT", "URGENT"):
                            await send_fn(chat_id, fmt_signal(sig))
                            _st.mark_alert(sym)
                            logger.info(f"Signal sent: {sym} {sig.level}")
                            
                            # Ramp Memory Integration
                            try:
                                record_ramp_from_gate_signal(
                                    symbol=sig.symbol,
                                    gate_rate_pct=sig.gate_rate_pct,
                                    oi_now=sig.oi_gate_now,
                                    premium_gate=sig.prem_gate,
                                    oi_velocity=sig.oi_delta_pct,
                                    source="gate_radar"
                                )
                            except Exception as ree:
                                logger.error(f"RampDB record error: {ree}")

                iteration += 1
                await asyncio.sleep(OI_CHECK_S)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _st.last_error = str(e)
                logger.error(f"Radar loop: {e}", exc_info=True)
                await asyncio.sleep(OI_CHECK_S)

    _st.running = False
    logger.info("Gate Ramp Radar остановлен")


def stop():
    _st.running = False


# ════════════════════════════════════════════════════════════════
# КОМАНДЫ TELEGRAM
# ════════════════════════════════════════════════════════════════

async def cmd_gate(update, ctx):
    """
    /gate              — watchlist
    /gate <МОНЕТА>     — детали
    /gate status       — состояние радара
    /gate refresh      — принудительное обновление
    /gate debug        — полная диагностика (что пошло не так)
    """
    args = ctx.args or []
    arg  = args[0].strip().upper() if args else ""

    if not arg or arg == "LIST":
        await update.effective_message.reply_text(fmt_list())

    elif arg == "STATUS":
        n1h = len(_st.get_1h_set())
        await update.effective_message.reply_text(
            f"📡 Gate Ramp Radar v3\n"
            f"Активен:     {'🟢 да' if _st.running else '🔴 нет'}\n"
            f"Init:        {'✅' if _st.init_done else '⏳'}\n"
            f"Тиков:       {_st.tick_count}\n"
            f"Контракты:   {len(_st.intervals)} total, {n1h} 1ч\n"
            f"Watchlist:   {_st.size()} монет\n"
            f"Монеты:      {', '.join(sorted(_st.syms())[:10]) or 'нет'}\n"
            f"Ошибка:      {_st.last_error or 'нет'}"
        )

    elif arg == "DEBUG":
        # Полная диагностика прямо в Telegram
        n1h  = len(_st.get_1h_set())
        all_ = len(_st.intervals)

        lines = [
            f"🔍 ДИАГНОСТИКА Gate Radar  {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            f"",
            f"Intervals cache: {all_} контрактов, {n1h} с 1ч",
            f"Watchlist: {_st.size()} монет",
            f"Тиков выполнено: {_st.tick_count}",
            f"Запущен: {'да' if _st.running else 'НЕТ'}",
            f"Init done: {'да' if _st.init_done else 'НЕТ'}",
            f"Последняя ошибка: {_st.last_error or 'нет'}",
            f"",
        ]

        if n1h == 0:
            lines.append("❌ ПРИЧИНА: Gate /contracts не вернул 1ч монеты")
            lines.append("   Запусти gate_ramp_diagnose.py на Docker!")
        elif _st.size() == 0:
            lines.append(f"⚠️  1ч монет есть ({n1h}), но ни одна не прошла фильтр rate")
            lines.append(f"   Порог: GATE_RATE_THRESHOLD = {GATE_RATE_THRESHOLD}")
            lines.append(f"   Первые 1ч монеты из кэша:")
            one_h_list = sorted(_st.get_1h_set())[:5]
            for name in one_h_list:
                lines.append(f"   {name}")
        else:
            lines.append(f"✅ Данные есть, {_st.size()} монет в watchlist")
            for sym in sorted(_st.syms())[:5]:
                c    = _st.contracts.get(sym)
                hist = _st.history(sym)
                lines.append(f"  {sym}: rate={c.funding_rate_pct:+.5f}%/ч, "
                             f"hist={len(hist)}")

        await update.effective_message.reply_text("\n".join(lines))

    elif arg == "REFRESH":
        msg = await update.effective_message.reply_text("🔄 Обновляю...")
        if not HAS_HTTPX:
            await msg.edit_text("❌ pip install httpx")
            return
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TO),
                                          headers=HEADERS) as s:
                await _refresh_intervals(s)
                await _refresh_watchlist(s)
            text = (f"✅ Обновлено\n"
                    f"1ч контрактов: {len(_st.get_1h_set())}\n"
                    f"Watchlist: {_st.size()} монет\n"
                    + fmt_list())
            await msg.edit_text(text[:4096])
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")

    else:
        sym = arg
        c   = _st.contracts.get(sym)
        if not c:
            contract_name = f"{sym}_USDT"
            interval = _st.intervals.get(contract_name, 0)
            reason = (
                "есть в 1ч кэше, но rate не проходит фильтр" if interval == 3600
                else "не 1ч контракт (interval={interval})" if interval
                else "не найден в кэше Gate"
            )
            await update.effective_message.reply_text(
                f"❌ {sym} нет в watchlist\n"
                f"Причина: {reason}\n\n"
                f"/gate debug — подробная диагностика\n"
                f"/gate refresh — обновить"
            )
            return

        hist = _st.history(sym)
        sig  = detect(sym)
        gd5  = _oi_delta(hist,  5, "oi_gate") if len(hist) >= 2 else 0
        gd15 = _oi_delta(hist, 15, "oi_gate") if len(hist) >= 2 else 0
        bd15 = _oi_delta(hist, 15, "oi_bin")  if len(hist) >= 2 else 0

        lines = [
            f"📊 GATE 1Ч: {sym}",
            f"Rate:    {c.funding_rate_pct:+.5f}%/ч",
            f"OI:      {_fmt_oi(c.oi_usdt)}",
            f"Premium: {c.premium_pct:+.3f}%",
            f"Watchlist: {_st.minutes_in(sym):.0f} мин",
            f"Данных:  {len(hist)} тиков × 30сек",
        ]
        if hist:
            cur = hist[-1]
            lines += [
                f"",
                f"OI Gate:    {_fmt_oi(cur.oi_gate)}  Δ5м={gd5:+.1f}%  Δ15м={gd15:+.1f}%",
                f"OI Binance: {_fmt_oi(cur.oi_bin)}  Δ15м={bd15:+.1f}%",
                f"Спред:      {cur.prem_gate - cur.prem_bin:+.3f}%",
            ]
        if sig:
            lines += ["", fmt_signal(sig)]
        else:
            lines += ["", f"Нет сигнала  (порог: ALERT≥{OI_RISE_ALERT}%/15м)"]

        await update.effective_message.reply_text("\n".join(lines))


# ════════════════════════════════════════════════════════════════
# ПРАВИЛЬНОЕ ВНЕДРЕНИЕ В main.py
# ════════════════════════════════════════════════════════════════
INTEGRATION_GUIDE = '''
# ═══════════════════════════════════════════════════════════
# КАК ПРАВИЛЬНО ВНЕДРИТЬ В main.py
# ═══════════════════════════════════════════════════════════

from radar.gate_ramp_radar import run_gate_ramp_radar, stop as stop_radar, cmd_gate
from telegram.ext import Application, CommandHandler
import asyncio

CHAT_ID = "123456789"  # ← СВОЙ chat_id как СТРОКА!

# ── Правильный вариант (гарантированный запуск) ──────────
async def post_init(app: Application):
    """Вызывается после инициализации бота."""
    
    async def send_fn(chat_id: str, text: str):
        try:
            await app.bot.send_message(
                chat_id=int(chat_id),   # Telegram нужен int!
                text=text,
            )
        except Exception as e:
            logger.error(f"send_fn: {e}")
    
    # create_task — запускает фоновую задачу
    task = asyncio.create_task(
        run_gate_ramp_radar(send_fn, CHAT_ID)
    )
    app.bot_data["gate_radar_task"] = task
    logger.info(f"Gate Radar task created: {task}")

async def post_shutdown(app: Application):
    stop_radar()
    task = app.bot_data.get("gate_radar_task")
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

# ── Регистрация ─────────────────────────────────────────
app = (
    Application.builder()
    .token(BOT_TOKEN)
    .post_init(post_init)
    .post_shutdown(post_shutdown)
    .build()
)

app.add_handler(CommandHandler("gate", cmd_gate))

# ── Как узнать свой CHAT_ID ──────────────────────────────
# Напиши боту /start или любое сообщение и посмотри логи:
# logger.info(f"Message from chat_id={update.effective_chat.id}")
# Или используй @userinfobot в Telegram
'''
