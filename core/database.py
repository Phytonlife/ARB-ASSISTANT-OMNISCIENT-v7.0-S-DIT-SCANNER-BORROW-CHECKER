# core/database.py
# SQLite async (v1) / PostgreSQL (v2) через SQLAlchemy

import time
from datetime import datetime
from loguru import logger
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Float, Integer, DateTime, Text, select, func
from core.config import settings

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_start_time = time.time()


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    strategy: Mapped[str] = mapped_column(String(50))
    ex_a: Mapped[str] = mapped_column(String(20))
    ex_b: Mapped[str] = mapped_column(String(20))
    size_usd: Mapped[float] = mapped_column(Float)
    spread_entry: Mapped[float] = mapped_column(Float)
    spread_exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str] = mapped_column(String(20))
    ex_a: Mapped[str] = mapped_column(String(20))
    ex_b: Mapped[str] = mapped_column(String(20))
    diff: Mapped[float] = mapped_column(Float)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingRule(Base):
    __tablename__ = "pending_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(Integer)
    rule_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DexAsset(Base):
    __tablename__ = "dex_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(100), index=True)
    dex_name: Mapped[str] = mapped_column(String(20))  # "apex", "raydium"
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")


# ── DEX helpers ──────────────────────────────────────────────────────

async def update_dex_assets(dex_name: str, symbols: list[str]):
    """Очищает старые монеты для этого DEX и записывает новые."""
    async with AsyncSessionLocal() as s:
        from sqlalchemy import delete
        # Удаляем старые записи для этого DEX
        await s.execute(delete(DexAsset).where(DexAsset.dex_name == dex_name))
        # Добавляем новые
        for sym in set(symbols):
            s.add(DexAsset(symbol=sym.upper(), dex_name=dex_name))
        await s.commit()
    logger.info(f"DEX Registry updated: {dex_name} ({len(symbols)} assets)")


async def is_on_dex(symbol: str) -> list[str]:
    """Возвращает список DEX, на которых есть эта монета (умный поиск)."""
    async with AsyncSessionLocal() as s:
        sym = symbol.upper()
        # Проверяем точное совпадение или совпадение с USDT суффиксом
        search_terms = {sym, f"{sym}USDT", f"{sym}-USDT"}
        if sym.endswith("USDT"):
            search_terms.add(sym.replace("USDT", ""))
        
        # ХАК: если в конце 'U' (пользователь мог иметь в виду USDT), пробуем без неё
        if len(sym) > 2 and sym.endswith("U"):
            base_without_u = sym[:-1]
            search_terms.update({base_without_u, f"{base_without_u}USDT"})
        
        from sqlalchemy import or_
        res = await s.execute(
            select(DexAsset.dex_name)
            .where(or_(DexAsset.symbol.in_(list(search_terms))))
        )
        return list(set(res.scalars().all()))


# ── CRUD helpers ──────────────────────────────────────────────────────

async def add_trade(symbol, strategy, ex_a, ex_b, spread_entry, size_usd) -> int:
    async with AsyncSessionLocal() as s:
        t = Trade(symbol=symbol, strategy=strategy, ex_a=ex_a, ex_b=ex_b,
                  spread_entry=spread_entry, size_usd=size_usd)
        s.add(t)
        await s.commit()
        await s.refresh(t)
        return t.id


async def close_trade(trade_id: int, spread_exit: float, pnl_usd: float):
    async with AsyncSessionLocal() as s:
        t = await s.get(Trade, trade_id)
        if not t:
            return None
        t.spread_exit = spread_exit
        t.pnl_usd = pnl_usd
        t.status = "closed"
        t.closed_at = datetime.utcnow()
        await s.commit()
        return t


async def get_open_trades() -> list[Trade]:
    async with AsyncSessionLocal() as s:
        result = await s.execute(select(Trade).where(Trade.status == "open"))
        return result.scalars().all()


async def get_stats(days: int = 30) -> dict:
    async with AsyncSessionLocal() as s:
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(days=days)
        result = await s.execute(
            select(Trade).where(Trade.status == "closed", Trade.closed_at >= since)
        )
        trades = result.scalars().all()
        if not trades:
            return {"total": 0, "win": 0, "loss": 0, "wr": 0.0, "pnl": 0.0, "pf": 0.0}
        wins = [t for t in trades if (t.pnl_usd or 0) > 0]
        losses = [t for t in trades if (t.pnl_usd or 0) <= 0]
        total_pnl = sum(t.pnl_usd or 0 for t in trades)
        gross_profit = sum(t.pnl_usd for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl_usd for t in losses)) if losses else 0
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 99.0
        return {
            "total": len(trades),
            "win": len(wins),
            "loss": len(losses),
            "wr": round(len(wins) / len(trades) * 100, 1),
            "pnl": round(total_pnl, 2),
            "pf": pf,
        }


async def get_history(n: int = 20) -> list[Trade]:
    async with AsyncSessionLocal() as s:
        result = await s.execute(
            select(Trade).order_by(Trade.id.desc()).limit(n)
        )
        return result.scalars().all()


async def add_alert(alert_type, symbol, ex_a, ex_b, diff, extra=None):
    async with AsyncSessionLocal() as s:
        a = Alert(alert_type=alert_type, symbol=symbol, ex_a=ex_a,
                  ex_b=ex_b, diff=diff, extra=extra)
        s.add(a)
        await s.commit()


async def add_pending_rule(trade_id: int, rule_text: str) -> int:
    async with AsyncSessionLocal() as s:
        r = PendingRule(trade_id=trade_id, rule_text=rule_text)
        s.add(r)
        await s.commit()
        await s.refresh(r)
        return r.id


async def get_pending_rules() -> list[PendingRule]:
    async with AsyncSessionLocal() as s:
        result = await s.execute(
            select(PendingRule).where(PendingRule.status == "pending")
        )
        return result.scalars().all()


async def approve_rule(rule_id: int) -> PendingRule | None:
    async with AsyncSessionLocal() as s:
        r = await s.get(PendingRule, rule_id)
        if not r:
            return None
        r.status = "approved"
        await s.commit()
        return r


def get_chunk_count() -> int:
    """Заглушка для количества чанков в FAISS."""
    return 0


async def get_db_size() -> int:
    """Возвращает количество строк в trades."""
    async with AsyncSessionLocal() as s:
        result = await s.execute(select(func.count()).select_from(Trade))
        return result.scalar() or 0


def get_uptime() -> str:
    secs = int(time.time() - _start_time)
    h, m = divmod(secs // 60, 60)
    return f"{h}ч {m}мин"
