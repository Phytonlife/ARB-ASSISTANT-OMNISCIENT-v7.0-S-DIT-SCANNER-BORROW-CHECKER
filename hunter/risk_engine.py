# hunter/risk_engine.py
# 5 лимитов: exposure, per-exchange, daily loss, open count, position size

from dataclasses import dataclass
from core.database import get_open_trades, get_stats
from core.config import settings


@dataclass
class RiskResult:
    ok: bool
    reason: str = ""
    exposure: float = 0.0
    per_ex_a: float = 0.0
    per_ex_b: float = 0.0


async def check_risk(
    size_usd: float,
    ex_a: str,
    ex_b: str,
    daily_loss_limit_pct: float = 5.0,
) -> RiskResult:
    """
    Проверяет 5 риск-лимитов перед открытием позиции:
    1. Max exposure: $300 (60% депозита)
    2. Per-exchange: $150 (30% депозита)
    3. Position size: max_position_usd (10% депозита)
    4. Max open positions: 5
    5. Daily loss stop: -5%
    """
    trades = await get_open_trades()

    # 1. Total exposure
    total_exposure = sum(t.size_usd for t in trades)
    max_exposure = settings.deposit_usd * 0.60  # $300
    if total_exposure + size_usd > max_exposure:
        return RiskResult(
            ok=False,
            reason=f"Exposure: ${total_exposure + size_usd:.0f} > ${max_exposure:.0f}",
            exposure=total_exposure,
        )

    # 2. Per-exchange
    ex_a_exposure = sum(t.size_usd for t in trades if t.ex_a == ex_a or t.ex_b == ex_a)
    ex_b_exposure = sum(t.size_usd for t in trades if t.ex_a == ex_b or t.ex_b == ex_b)
    max_per_ex = settings.deposit_usd * 0.30  # $150

    if ex_a_exposure + size_usd > max_per_ex:
        return RiskResult(
            ok=False,
            reason=f"{ex_a} cap: ${ex_a_exposure + size_usd:.0f} > ${max_per_ex:.0f}",
            per_ex_a=ex_a_exposure,
        )
    if ex_b_exposure + size_usd > max_per_ex:
        return RiskResult(
            ok=False,
            reason=f"{ex_b} cap: ${ex_b_exposure + size_usd:.0f} > ${max_per_ex:.0f}",
            per_ex_b=ex_b_exposure,
        )

    # 3. Position size
    if size_usd > settings.max_position_usd:
        return RiskResult(
            ok=False,
            reason=f"Position ${size_usd} > max ${settings.max_position_usd:.0f}",
        )

    # 4. Open positions
    if len(trades) >= 5:
        return RiskResult(ok=False, reason=f"Max 5 позиций открыто: {len(trades)}")

    # 5. Daily loss
    stats = await get_stats(days=1)
    daily_pnl = stats.get("pnl", 0.0)
    daily_loss_limit = -settings.deposit_usd * daily_loss_limit_pct / 100
    if daily_pnl < daily_loss_limit:
        return RiskResult(
            ok=False,
            reason=f"Daily loss stop: ${daily_pnl:.2f} < ${daily_loss_limit:.2f}",
        )

    return RiskResult(
        ok=True,
        exposure=total_exposure,
        per_ex_a=ex_a_exposure,
        per_ex_b=ex_b_exposure,
    )
