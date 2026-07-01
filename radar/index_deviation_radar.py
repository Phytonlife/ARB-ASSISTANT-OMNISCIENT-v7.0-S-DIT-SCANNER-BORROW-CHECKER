# radar/index_deviation_radar.py
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional, List
from loguru import logger

# ════════════════════════════════════════════════════════════════
# КОНФИГ
# ════════════════════════════════════════════════════════════════

BIRZHI_TO_SCAN = ["binance", "bybit", "gate", "okx", "kucoin", "coinex", "mexc"]

NOISE_THRESHOLD     = 0.3
FORMING_THRESHOLD   = 0.8
STRONG_THRESHOLD    = 2.0
EXTREME_THRESHOLD   = 5.0

VELOCITY_ALERT      = -0.3
ALERT_COOLDOWN_MIN  = 30

CAPS = {
    "binance": 0.75, "okx": 0.75, "bybit": 0.75,
    "gate": 0.75, "kucoin": 0.75, "mexc": 0.75
}

@dataclass
class DeviationSnap:
    symbol:        str
    exchange:      str
    timestamp:     float
    deviation:     float
    funding_rate:  float
    predicted:     float
    funding_hours: int
    mark_price:    float
    index_price:   float
    funding_timestamp: float = 0

    @property
    def limit_ratio(self) -> float:
        cap = CAPS.get(self.exchange, 0.75)
        return min(abs(self.deviation)/8, cap)/cap if cap > 0 else 0

    @property
    def is_ramp(self) -> bool: return self.limit_ratio > 0.9

@dataclass
class DeviationSignal:
    signal_type:   str
    symbol:        str
    exchange:      str
    snap:          DeviationSnap
    velocity:      float = 0
    acceleration:  float = 0
    alert_text:    str = ""
    dex_info:      List[str] = field(default_factory=list)

class DeviationStore:
    def __init__(self):
        self._hist = defaultdict(lambda: deque(maxlen=48))
        self._alerted = {}

    def add(self, s: DeviationSnap):
        self._hist[(s.symbol, s.exchange)].append(s)

    def get(self, sym: str, ex: str, hours: float = 2) -> list[DeviationSnap]:
        now = time.time()
        return [s for s in self._hist[(sym, ex)] if s.timestamp > now - hours*3600]

    def get_all_latest(self) -> list[DeviationSnap]:
        return [dq[-1] for dq in self._hist.values() if dq]

    def cooldown_ok(self, sym: str, ex: str) -> bool:
        last = self._alerted.get((sym, ex), 0)
        return (time.time() - last)/60 > ALERT_COOLDOWN_MIN

    def mark_alerted(self, sym: str, ex: str):
        self._alerted[(sym, ex)] = time.time()

dev_store = DeviationStore()

async def ensure_deviation_data():
    if not dev_store.get_all_latest():
        await deviation_scan()

def calc_velocity(snaps: list[DeviationSnap]) -> float:
    if len(snaps) < 2: return 0.0
    s1, s2 = snaps[0], snaps[-1]
    dt = (s2.timestamp - s1.timestamp)/3600
    return (s2.deviation - s1.deviation)/dt if dt > 0.05 else 0

async def fetch_deviations_for_exchange(ex_id: str) -> list[DeviationSnap]:
    snaps = []
    try:
        import ccxt.async_support as ccxt
        ex = getattr(ccxt, ex_id)({"enableRateLimit":True})
        try:
            rates = await ex.fetch_funding_rates()
            now = time.time()
            for sym, r in rates.items():
                if "/USDT" not in sym: continue
                s_clean = sym.split("/")[0]
                mark = float(r.get('markPrice') or r.get('last') or 0)
                index = float(r.get('indexPrice') or mark)
                if index == 0: continue
                dev = (mark - index)/index * 100
                fund = float(r.get('fundingRate') or 0) * 100
                snaps.append(DeviationSnap(
                    s_clean, ex_id, now, dev, fund, fund, 8, mark, index
                ))
        finally: await ex.close()
    except Exception as e: logger.debug(f"Dev fetch {ex_id}: {e}")
    return snaps

async def deviation_scan():
    all_snaps = []
    for ex in BIRZHI_TO_SCAN:
        snaps = await fetch_deviations_for_exchange(ex)
        for s in snaps: dev_store.add(s)
        all_snaps.extend(snaps)
    return await detect_signals(all_snaps)

async def detect_signals(snaps: list[DeviationSnap]) -> list[DeviationSignal]:
    from radar.dex_db import dex_db
    signals = []
    for s in snaps:
        if not dev_store.cooldown_ok(s.symbol, s.exchange): continue
        hist = dev_store.get(s.symbol, s.exchange, hours=2)
        vel = calc_velocity(hist)
        sig = None
        if vel < VELOCITY_ALERT and s.deviation < -0.5:
            sig = DeviationSignal("ACCELERATING", s.symbol, s.exchange, s, vel, 0, f"Разгон: {vel:.2f}%/ч")
        elif s.deviation < -STRONG_THRESHOLD:
            sig = DeviationSignal("BIG_NEG", s.symbol, s.exchange, s, vel, 0, f"Критическое отклонение {s.deviation:.2f}%")
        if sig:
            sig.dex_info = dex_db.where_listed(s.symbol)
            signals.append(sig); dev_store.mark_alerted(s.symbol, s.exchange)
    return signals

def format_deviation_dashboard(direction="both", top_n=30):
    from radar.dex_db import dex_db, DEX_ICONS
    snaps = dev_store.get_all_latest()
    if not snaps: return "❌ Нет данных. Подождите пару минут..."
    filtered = snaps
    if direction == "neg": filtered = [s for s in snaps if s.deviation < 0]
    elif direction == "pos": filtered = [s for s in snaps if s.deviation > 0]
    filtered.sort(key=lambda s: abs(s.deviation), reverse=True)
    now_str = datetime.now(timezone.utc).strftime("%H:%M")
    lines = [
        f"📊 *ОТКЛОНЕНИЯ ОТ ИНДЕКСА*  |  {now_str} UTC",
        f"{'Монета':10} {'DEX':6} {'Биржа':8} {'Откл':9} {'LR'}",
        "─" * 52
    ]
    for s in filtered[:top_n]:
        dexes = dex_db.where_listed(s.symbol)
        dex_icons = "".join([DEX_ICONS.get(d, "") for d in dexes[:3]])
        if not dex_icons: dex_icons = " "
        icon = "🔴" if s.is_ramp else ("🟠" if abs(s.deviation) > 2 else ("🟡" if abs(s.deviation) > 0.8 else "⚪"))
        if s.deviation > 0 and icon == "⚪": icon = "💙"
        lr_str = f"{int(s.limit_ratio*100)}%"
        lr_icon = "🔴" if s.is_ramp else ""
        lines.append(f"{icon} {s.symbol:7} {dex_icons:5} {s.exchange:8} {s.deviation:+.2f}%  {lr_str}{lr_icon}")
    lines.append("─" * 52)
    lines.append("DEX: 🔷Apex 🌊Raydium ⚡Aster 🌀HL 🟣Aevo")
    return "\n".join(lines)

def format_deviation_by_symbol(sym: str) -> str:
    snaps = [s for s in dev_store.get_all_latest() if s.symbol == sym.upper()]
    if not snaps: return f"📊 {sym.upper()}: нет активных отклонений"
    snaps.sort(key=lambda s: s.deviation)
    lines = [f"📊 *{sym.upper()} ANALYSIS*", "─"*30]
    for s in snaps:
        lines.append(f"{s.exchange:10} {s.deviation:+.3f}%  R:{s.funding_rate:+.4f}%")
    return "\n".join(lines)

def format_acceleration_report():
    snaps = dev_store.get_all_latest()
    accels = []
    for s in snaps:
        hist = dev_store.get(s.symbol, s.exchange, hours=2)
        vel = calc_velocity(hist)
        if vel < -0.15: accels.append((s, vel))
    if not accels: return "🚀 Активных разгонов не обнаружено"
    accels.sort(key=lambda x: x[1])
    lines = ["🚀 *TOP ACCELERATION (VELOCITY)*", "─" * 40]
    for s, vel in accels[:15]:
        lines.append(f"🔥 {s.symbol:6} {s.exchange:10} `{vel:+.2f}%/ч`  (Dev: {s.deviation:+.2f}%)")
    return "\n".join(lines)

async def format_deviation_alert(sig: DeviationSignal) -> tuple[str, Optional[object]]:
    s = sig.snap
    icon = "🚀" if sig.signal_type == "ACCELERATING" else ("🔴" if s.is_ramp else "🟠")
    lines = [
        f"{icon} *{sig.signal_type}* [{sig.symbol}]",
        f"Биржа: {sig.exchange.upper()}",
        f"─────────────────────────────",
        f"Отклонение: {s.deviation:+.3f}%",
        f"Фандинг:    {s.funding_rate:+.5f}%",
        f"Velocity:   {sig.velocity:+.3f}%/ч",
    ]
    try:
        from radar.dex_db import dex_db
        dex_line = dex_db.format_dex_line(sig.symbol)
        if dex_line:
            lines += ["─────────────────────────────", dex_line]
            if "ApeX" in dex_line:
                from radar.apex_checker import format_apex_block
                lines += [format_apex_block(sig.symbol, sig.exchange, s.funding_rate)]
        
        # Real-time Borrow Check (Async)
        from radar.borrow_checker import check_all_borrow
        borrow_text = await check_all_borrow(sig.symbol)
        if borrow_text:
            lines += [borrow_text]
    except Exception as e:
        logger.debug(f"DEX/Borrow check error: {e}")
    
    lines += ["", f"💡 {sig.alert_text}"]
    kb = None
    try:
        from bot.ui import make_alert_keyboard
        kb = make_alert_keyboard(sig.symbol, sig.exchange)
    except: pass
    return "\n".join(lines), kb

async def format_alerts_batch(signals):
    res = []
    for s in signals:
        res.append(await format_deviation_alert(s))
    return res
