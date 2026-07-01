# radar/funding_monitor.py
# scan_funding: diff + OFI + slippage + risk + regime check
# Режим AUTO: сканирует ВСЕ монеты, не нужен фиксированный список

import asyncio
from dataclasses import dataclass
from data.exchanges import get_all_rates, get_orderbook_depth, scan_all_funding_diffs
from hunter.math_engine import calc_net_spread, calc_ofi, estimate_slippage, check_ticker_trap
from hunter.risk_engine import check_risk
from oracle.regime import detect_regime
from core.database import add_alert
from core.config import settings


@dataclass
class FundingSignal:
    symbol: str
    diff: float
    long_ex: str
    short_ex: str
    long_rate: float
    short_rate: float
    net_spread: float
    ofi_long: float
    ofi_short: float
    slippage_a: float
    slippage_b: float
    urgent: bool
    ramp: bool
    all_rates: dict
    premium_block: str = ""
    ramp_block: str = ""

    def format_alert(self) -> str:
        head = "🚨 СРОЧНО" if self.urgent else "⚡ ВНИМАНИЕ"
        
        ofi_l = ("📈Накоп" if self.ofi_long > 0.3
                 else ("📉Продажа" if self.ofi_long < -0.3 else "➡️Нейтраль"))
        ofi_s = ("📉Продажа" if self.ofi_short < -0.3
                 else ("📈Накоп" if self.ofi_short > 0.3 else "➡️Нейтраль"))

        rates_str = "  ".join(
            f"{ex[:3].upper()}:{r:+.3f}%"
            for ex, r in sorted(self.all_rates.items(), key=lambda x: -abs(x[1]))[:4]
        )

        blocks = [
            f"{head} FUNDING [{self.symbol}]",
            f"{'─' * 35}",
            f"SHORT: {self.short_ex.upper():8} {self.short_rate:+.4f}%",
            f"LONG:  {self.long_ex.upper():8} {self.long_rate:+.4f}%",
            f"DIFF: {self.diff:.4f}% | NET: {self.net_spread:.4f}%",
            f"{rates_str}"
        ]

        if self.ramp_block:
            blocks.append(f"\n{self.ramp_block}")
        
        if self.premium_block:
            blocks.append(f"\n{self.premium_block}")

        blocks.append(
            f"\nOFI: {self.long_ex[:4]} {ofi_l} {self.ofi_long:+.2f} | "
            f"{self.short_ex[:4]} {ofi_s} {self.ofi_short:+.2f}"
        )
        blocks.append(f"Slip: {self.slippage_a:.3f}% | {self.slippage_b:.3f}%")
        blocks.append(f"\nВойти ПОСЛЕ выплаты | Выйти ПЕРЕД 3-й")

        return "\n".join(blocks)


async def scan_funding(symbols: list[str] | None = None) -> list[FundingSignal]:
    """
    Полный pipeline сканирования фандинга.

    symbols=None → АВТО-РЕЖИМ: сканирует ВСЕ доступные монеты (рекомендуется)
    symbols=[...] → ручной список (устаревший режим)

    AUTO-режим находит аномалии везде, не только в заданных монетах.
    Например: ORCA, JTO, PYTH, RENDER — обычно именно там бывают жирные дифы.
    """
    regime = await detect_regime()
    if "funding_arb" not in regime.allowed:
        return []

    # ── АВТО-РЕЖИМ: scan_all_funding_diffs за 1 параллельный запрос ──
    if symbols is None:
        return await _scan_auto(regime)

    # ── РУЧНОЙ РЕЖИМ: фиксированный список символов ──
    return await _scan_manual(symbols, regime)


async def _scan_auto(regime) -> list[FundingSignal]:
    """Авто-скан: один запрос ко всем биржам → топ аномалий."""
    from loguru import logger

    raw_diffs = await scan_all_funding_diffs(
        threshold=settings.funding_alert_threshold,
        top_n=30,
    )

    from data.premium_intelligence import get_premium_intelligence, format_premium_block
    from hunter.math_engine import predict_cycle_transition, format_cycle_alert
    signals = []
    for item in raw_diffs:
        symbol = item["symbol"]
        try:
            min_ex = item["min_ex"]   # long биржа (низкий rate)
            max_ex = item["max_ex"]   # short биржа (высокий rate)
            diff = item["diff"]
            rates = item["rates"]

            # Проверка ловушки тикера (разные контракты)
            trap = check_ticker_trap(diff)
            if trap["trap"]:
                logger.warning(f"Ticker trap: {symbol} diff={diff}% > 40%")
                continue

            r = calc_net_spread(diff, min_ex, max_ex, 50, "perp", "perp", False)
            if "error" in r or not r["ok"]:
                continue

            ob_l, ob_s = await asyncio.gather(
                asyncio.wait_for(get_orderbook_depth(min_ex, symbol), 10),
                asyncio.wait_for(get_orderbook_depth(max_ex, symbol), 10),
            )

            risk = await asyncio.wait_for(check_risk(50, min_ex, max_ex), 10)
            if not risk.ok:
                continue

            # Premium Intelligence
            try:
                intel = await asyncio.wait_for(get_premium_intelligence(symbol, min_ex, max_ex), 30)
                premium_block = format_premium_block(intel)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout gathering premium intel for {symbol}")
                premium_block = "⚠️ ТАЙМАУТ ПРИ ПОЛУЧЕНИИ ДАННЫХ"
                # Создаем пустой объект для совместимости логики ниже
                from data.premium_intelligence import PremiumIntelligence
                intel = PremiumIntelligence(symbol, min_ex, max_ex, None, None, "TIMEOUT", "❓", 0, 999, [], False, 0, "", "", "")

            # Cycle Transition (Ramp) Prediction
            ramp_block = ""
            if intel.long_data and intel.long_data.rate_history:
                # Анализируем обе ноги на предмет рампы
                sig_l = predict_cycle_transition(min_ex, rates[min_ex], intel.long_data.rate_history)
                if sig_l.probability in ["🔴 РАМПА АКТИВНА", "🟡 ВЫСОКАЯ"]:
                    ramp_block += format_cycle_alert(sig_l) + "\n"
            
            if intel.short_data and intel.short_data.rate_history:
                sig_s = predict_cycle_transition(max_ex, rates[max_ex], intel.short_data.rate_history)
                if sig_s.probability in ["🔴 РАМПА АКТИВНА", "🟡 ВЫСОКАЯ"]:
                    ramp_block += format_cycle_alert(sig_s)

            sig = FundingSignal(
                symbol=symbol,
                diff=diff,
                long_ex=min_ex,
                short_ex=max_ex,
                long_rate=rates[min_ex],
                short_rate=rates[max_ex],
                net_spread=r["net"],
                ofi_long=calc_ofi(ob_l.get("bids", []), ob_l.get("asks", [])),
                ofi_short=calc_ofi(ob_s.get("bids", []), ob_s.get("asks", [])),
                slippage_a=estimate_slippage(50, ob_l.get("depth_usd", 9999)),
                slippage_b=estimate_slippage(50, ob_s.get("depth_usd", 9999)),
                urgent=diff >= 0.50,
                ramp=any(abs(v) >= 1.5 for v in rates.values()),
                all_rates=rates,
                premium_block=premium_block,
                ramp_block=ramp_block.strip()
            )
            signals.append(sig)
            await add_alert("funding", symbol, min_ex, max_ex, diff, str(rates))

        except Exception as e:
            logger.warning(f"_scan_auto {symbol}: {e}")

    return sorted(signals, key=lambda s: s.diff, reverse=True)


async def _scan_manual(symbols: list[str], regime) -> list[FundingSignal]:
    """Ручной скан по заданному списку символов."""
    from loguru import logger
    from data.premium_intelligence import get_premium_intelligence, format_premium_block
    from hunter.math_engine import predict_cycle_transition, format_cycle_alert
    signals = []
    for symbol in symbols:
        try:
            rates = await get_all_rates(symbol)
            if len(rates) < 2:
                continue

            max_ex = max(rates, key=lambda k: rates[k])
            min_ex = min(rates, key=lambda k: rates[k])
            diff = round(rates[max_ex] - rates[min_ex], 5)

            if diff < settings.funding_alert_threshold:
                continue

            # Проверка ловушки тикера (разные контракты)
            trap = check_ticker_trap(diff)
            if trap["trap"]:
                logger.warning(f"Ticker trap: {symbol} diff={diff}% > 40%")
                continue

            r = calc_net_spread(diff, min_ex, max_ex, 50, "perp", "perp", False)
            if "error" in r or not r["ok"]:
                continue

            ob_l, ob_s = await asyncio.gather(
                asyncio.wait_for(get_orderbook_depth(min_ex, symbol), 10),
                asyncio.wait_for(get_orderbook_depth(max_ex, symbol), 10),
            )
            risk = await asyncio.wait_for(check_risk(50, min_ex, max_ex), 10)
            if not risk.ok:
                continue

            # Premium Intelligence
            try:
                intel = await asyncio.wait_for(get_premium_intelligence(symbol, min_ex, max_ex), 30)
                premium_block = format_premium_block(intel)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout gathering premium intel for {symbol}")
                premium_block = "⚠️ ТАЙМАУТ ПРИ ПОЛУЧЕНИИ ДАННЫХ"
                from data.premium_intelligence import PremiumIntelligence
                intel = PremiumIntelligence(symbol, min_ex, max_ex, None, None, "TIMEOUT", "❓", 0, 999, [], False, 0, "", "", "")

            # Cycle Transition (Ramp) Prediction
            ramp_block = ""
            if intel.long_data and intel.long_data.rate_history:
                sig_l = predict_cycle_transition(min_ex, rates[min_ex], intel.long_data.rate_history)
                if sig_l.probability in ["🔴 РАМПА АКТИВНА", "🟡 ВЫСОКАЯ"]:
                    ramp_block += format_cycle_alert(sig_l) + "\n"
            
            if intel.short_data and intel.short_data.rate_history:
                sig_s = predict_cycle_transition(max_ex, rates[max_ex], intel.short_data.rate_history)
                if sig_s.probability in ["🔴 РАМПА АКТИВНА", "🟡 ВЫСОКАЯ"]:
                    ramp_block += format_cycle_alert(sig_s)

            sig = FundingSignal(
                symbol=symbol, diff=diff,
                long_ex=min_ex, short_ex=max_ex,
                long_rate=rates[min_ex], short_rate=rates[max_ex],
                net_spread=r["net"],
                ofi_long=calc_ofi(ob_l.get("bids", []), ob_l.get("asks", [])),
                ofi_short=calc_ofi(ob_s.get("bids", []), ob_s.get("asks", [])),
                slippage_a=estimate_slippage(50, ob_l.get("depth_usd", 9999)),
                slippage_b=estimate_slippage(50, ob_s.get("depth_usd", 9999)),
                urgent=diff >= 0.50,
                ramp=any(abs(v) >= 1.5 for v in rates.values()),
                all_rates=rates,
                premium_block=premium_block,
                ramp_block=ramp_block.strip()
            )
            signals.append(sig)
            await add_alert("funding", symbol, min_ex, max_ex, diff, str(rates))
        except Exception as e:
            logger.warning(f"scan_funding {symbol}: {e}")

    return sorted(signals, key=lambda s: s.diff, reverse=True)
