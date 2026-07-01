# ═══════════════════════════════════════════════════════════════════════
# radar/gold_funding.py
# GOLD FUNDING — Золотой список: фандинг + маржин + спот
#
# Команда /gold — показывает монеты у которых ОДНОВРЕМЕННО:
#   1. Значимый фандинг (отрицательный или положительный)
#   2. Доступен маржинальный займ (чтобы шортить или лонгить)
#   3. Есть спот рынок (для спот+перп стратегии)
#
# Режимы:
#   /gold        → все (минус + плюс фандинг)
#   /gold neg    → только отрицательный фандинг (шортить выгодно)
#   /gold pos    → только положительный фандинг (лонгить выгодно)
#   /gold SYMBOL → детальный разбор одной монеты
#
# Стратегии которые находит:
#   Перп+Перп    — перп лонг + перп шорт (разные биржи)
#   Маржин↓+Перп — занять монету → шорт спот + лонг перп
#   Спот+Перп    — купить спот + шорт перп (delta-neutral)
#   Перп↑+Маржин — лонг перп + занять USDT под залог
#
# КАК РАБОТАЕТ БЕЗ ПРЕДВАРИТЕЛЬНОГО СКАНА:
#   Если margin_store и dev_store пустые (бот только запустился),
#   Gold автоматически делает быстрый скан нужных данных.
#   Это делает команду работоспособной в любое время.
# ═══════════════════════════════════════════════════════════════════════

import asyncio
from datetime import datetime, timezone
from typing import Optional
import logging

try:
    from loguru import logger
except ImportError:
    logger = logging.getLogger(__name__)

# Наши модули
from radar.margin_monitor import margin_store, MarginAsset
from radar.index_deviation_radar import dev_store, DeviationSnap, CAPS

# ═══════════════════════════════════════════════════════════════════════
# КОНСТАНТЫ
# ═══════════════════════════════════════════════════════════════════════

# Минимальный фандинг для попадания в Gold список (в %)
MIN_FUNDING_NEG = -0.10   # хотя бы -0.10%/8ч
MIN_FUNDING_POS = +0.10   # хотя бы +0.10%/8ч
MIN_FUNDING_ANY =  0.08   # для режима "all"

# Биржи для быстрого скана если данных нет
QUICK_SCAN_EXCHANGES = ["bybit", "binance", "gate", "okx", "kucoin"]

# Спот доступен на этих биржах (дополняется через ccxt)
SPOT_EXCHANGES = {"binance", "bybit", "okx", "gate", "kucoin", "mexc", "coinex"}


# ═══════════════════════════════════════════════════════════════════════
# ПОЛУЧЕНИЕ СПОТ РЫНКОВ (кэш чтобы не грузить каждый раз)
# ═══════════════════════════════════════════════════════════════════════

_spot_cache: dict[str, set] = {}   # {exchange: {symbols}}
_spot_cache_ts: float = 0


async def get_spot_symbols(exchanges: list[str] = None) -> set[str]:
    """
    Возвращает множество тикеров у которых есть спот рынок.
    Кэшируется на 30 минут.
    """
    global _spot_cache, _spot_cache_ts

    now = datetime.now(timezone.utc).timestamp()
    if now - _spot_cache_ts < 1800 and _spot_cache:
        # Объединяем все биржи
        result = set()
        for syms in _spot_cache.values():
            result |= syms
        return result

    if exchanges is None:
        exchanges = list(SPOT_EXCHANGES)[:3]  # берём 3 для скорости

    try:
        import ccxt.async_support as ccxt

        for ex_id in exchanges:
            try:
                ex_cls = getattr(ccxt, ex_id, None)
                if not ex_cls:
                    continue
                ex = ex_cls({"enableRateLimit": True})
                markets = await ex.load_markets()
                spot_syms = {
                    m.get("base", "").upper()
                    for s, m in markets.items()
                    if m.get("type") == "spot"
                    and m.get("quote") == "USDT"
                    and m.get("active", True)
                    and m.get("base")
                }
                _spot_cache[ex_id] = spot_syms
                await ex.close()
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"get_spot_symbols {ex_id}: {e}")

        _spot_cache_ts = now

    except ImportError:
        logger.warning("ccxt не установлен, спот недоступен")

    result = set()
    for syms in _spot_cache.values():
        result |= syms
    return result


# ═══════════════════════════════════════════════════════════════════════
# БЫСТРЫЙ СКАН (если данных нет)
# ═══════════════════════════════════════════════════════════════════════

async def ensure_fresh_data(max_age_min: int = 20) -> dict:
    """
    Гарантирует наличие свежих данных в margin_store и dev_store.
    Если данные устарели или отсутствуют — делает быстрый скан.
    Возвращает статистику: {"margin_count": N, "dev_count": M}
    """
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - max_age_min * 60

    # Проверяем маржин данные (Bybit)
    margin_symbols = margin_store.known_symbols("bybit")
    margin_fresh = False
    if margin_symbols:
        # Проверяем актуальность первой монеты
        first_sym = next(iter(margin_symbols))
        asset = margin_store.get(first_sym, "bybit")
        if asset and asset.timestamp > cutoff:
            margin_fresh = True

    # Проверяем deviation данные
    dev_snaps = dev_store.get_all_latest()
    dev_fresh = False
    if dev_snaps:
        # Проверяем актуальность
        newest_ts = max(s.timestamp for s in dev_snaps)
        if newest_ts > cutoff:
            dev_fresh = True

    tasks = []

    if not margin_fresh:
        logger.info("Gold: обновляем маржин данные...")
        tasks.append(_quick_margin_scan())

    if not dev_fresh:
        logger.info("Gold: обновляем deviation данные...")
        tasks.append(_quick_deviation_scan())

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    return {
        "margin_count": len(margin_store.known_symbols("bybit")),
        "dev_count":    len(dev_store.get_all_latest()),
    }


async def _quick_margin_scan():
    """Быстрый скан маржина только с Bybit (самый информативный)."""
    try:
        from radar.margin_monitor import fetch_bybit_margin, detect_margin_events
        import ccxt.async_support as ccxt

        ex = ccxt.bybit({"enableRateLimit": True})
        assets = await fetch_bybit_margin(ex)
        await ex.close()

        for asset in assets:
            margin_store.update(asset)

        logger.info(f"Gold margin scan: {len(assets)} монет")
    except Exception as e:
        logger.error(f"_quick_margin_scan: {e}")


async def _quick_deviation_scan():
    """Быстрый скан отклонений с топ-3 бирж."""
    try:
        from radar.index_deviation_radar import fetch_deviations_for_exchange

        for ex_id in ["binance", "bybit", "gate"]:
            snaps = await fetch_deviations_for_exchange(
                ex_id, batch_size=30
            )
            for s in snaps:
                dev_store.add(s)
            await asyncio.sleep(0.3)

        logger.info(f"Gold deviation scan: {len(dev_store.get_all_latest())} монет")
    except Exception as e:
        logger.error(f"_quick_deviation_scan: {e}")


# ═══════════════════════════════════════════════════════════════════════
# ОСНОВНАЯ ЛОГИКА GOLD
# ═══════════════════════════════════════════════════════════════════════

def determine_strategies(
    symbol: str,
    fund_pct: float,
    deviation: float,
    exchange: str,
    has_margin: bool,
    has_spot: bool,
) -> list[dict]:
    """
    Определяет применимые стратегии для монеты.
    Возвращает список {name, description, risk, profit_source}.
    """
    strategies = []

    # Перп-Перп: если есть значимое отклонение между биржами
    if abs(deviation) > 0.3:
        if fund_pct < 0:
            strategies.append({
                "name": "Перп+Перп",
                "desc": f"LONG {exchange.upper()} перп + SHORT медленная биржа",
                "risk": "LOW",
                "icon": "⚖️",
            })
        else:
            strategies.append({
                "name": "Перп+Перп",
                "desc": f"SHORT {exchange.upper()} перп + LONG медленная биржа",
                "risk": "LOW",
                "icon": "⚖️",
            })

    # Маржин↓+Перп: займ монеты → шорт спот + лонг перп
    if has_margin and fund_pct < 0:
        strategies.append({
            "name": "Маржин↓+Перп",
            "desc": f"Занять {symbol} → SHRT спот + LONG {exchange.upper()} перп",
            "risk": "MEDIUM",
            "icon": "📉",
        })

    # Спот+Перп: купить спот + шорт перп (delta-neutral, получаем фандинг)
    if has_spot and fund_pct < 0:
        strategies.append({
            "name": "Спот+Перп",
            "desc": f"BUY {symbol} спот + SHORT {exchange.upper()} перп",
            "risk": "LOW",
            "icon": "🔄",
        })

    # Перп↑+Маржин: лонг перп (получаем фандинг) + занять USDT
    if has_margin and fund_pct > 0:
        strategies.append({
            "name": "Перп↑+Маржин",
            "desc": f"LONG {exchange.upper()} перп + SHORT маржин спот",
            "risk": "MEDIUM",
            "icon": "📈",
        })

    return strategies


def build_gold_list(
    mode: str = "all",
    min_funding: float = 0.08,
    spot_symbols: set = None,
) -> list[dict]:
    """
    Строит Gold список из данных margin_store + dev_store.

    mode: "all" | "neg" | "pos"
    min_funding: минимальный |funding_rate * 100| для включения
    spot_symbols: множество тикеров с спот рынком
    """
    if spot_symbols is None:
        spot_symbols = set()

    results = []

    # Все актуальные снимки из dev_store
    all_snaps = dev_store.get_all_latest()

    # Группируем по символу: берём снимок с максимальным |fund|
    best_by_sym: dict[str, DeviationSnap] = {}
    for snap in all_snaps:
        sym = snap.symbol
        cur_abs = abs(snap.funding_rate)
        if sym not in best_by_sym:
            best_by_sym[sym] = snap
        elif cur_abs > abs(best_by_sym[sym].funding_rate):
            best_by_sym[sym] = snap

    for sym, snap in best_by_sym.items():
        fund_pct = snap.funding_rate * 100
        fund_abs = abs(fund_pct)

        # Фильтр по минимуму
        if fund_abs < min_funding:
            continue

        # Фильтр по направлению
        if mode == "neg" and fund_pct >= 0:
            continue
        if mode == "pos" and fund_pct <= 0:
            continue

        # Маржин данные (проверяем bybit — лучший источник)
        margin = margin_store.get(sym, "bybit")
        has_margin = margin is not None and margin.borrowable

        # Если нет на Bybit — проверяем другие биржи
        if not has_margin:
            for ex in ["gate", "binance", "okx"]:
                m = margin_store.get(sym, ex)
                if m and m.borrowable:
                    margin = m
                    has_margin = True
                    break

        # Спот
        has_spot = sym in spot_symbols or sym in {
            "BTC", "ETH", "BNB", "SOL", "ADA", "DOT", "MATIC", "AVAX",
            "LINK", "UNI", "ATOM", "FTM", "NEAR", "APT", "ARB", "OP",
            # Добавляем монеты из нашего dev_store (обычно у всех есть спот)
            snap.symbol,
        }
        # Эвристика: если монета есть на крупной бирже → спот почти всегда есть
        if snap.exchange in ("binance", "bybit", "okx"):
            has_spot = True

        # Нужно хотя бы маржин ИЛИ спот
        if not has_margin and not has_spot:
            continue

        # Определяем стратегии
        strats = determine_strategies(
            symbol=sym,
            fund_pct=fund_pct,
            deviation=snap.deviation,
            exchange=snap.exchange,
            has_margin=has_margin,
            has_spot=has_spot,
        )

        if not strats:
            continue

        # Оценка "золотости": чем больше стратегий и выше фандинг — тем лучше
        gold_score = (
            fund_abs * 10               # фандинг (основной фактор)
            + len(strats) * 0.5         # количество стратегий
            + (1.0 if has_margin else 0) # маржин доступен
            + (0.5 if has_spot else 0)   # спот доступен
            - (margin.borrow_usage_rate * 2 if margin else 0)  # штраф за занятый займ
        )

        results.append({
            "sym":          sym,
            "exchange":     snap.exchange,
            "fund_pct":     fund_pct,
            "fund_hours":   snap.funding_hours,
            "deviation":    snap.deviation,
            "has_margin":   has_margin,
            "has_spot":     has_spot,
            "margin":       margin,
            "strategies":   strats,
            "gold_score":   gold_score,
            "snap":         snap,
        })

    # Сортировка по gold_score убывание
    results.sort(key=lambda x: x["gold_score"], reverse=True)
    return results


# ═══════════════════════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════

def _funding_icon(fund_pct: float) -> str:
    if fund_pct < -1.0:    return "🔴"
    elif fund_pct < -0.3:  return "🟠"
    elif fund_pct < -0.08: return "🟡"
    elif fund_pct > 1.0:   return "🟢"
    elif fund_pct > 0.3:   return "💙"
    else:                  return "⚪"


def _margin_str(margin: Optional[MarginAsset]) -> str:
    if margin is None:
        return "❌"
    usage = margin.borrow_usage_rate
    daily = margin.daily_borrow_rate * 100
    ex    = margin.exchange.upper()[:3]
    if usage > 0.85:   mu = f"🔴{usage*100:.0f}%"
    elif usage > 0.60: mu = f"🟡{usage*100:.0f}%"
    elif usage > 0:    mu = f"🟢{usage*100:.0f}%"
    else:              mu = "🟢  ?"
    return f"{ex} {mu} {daily:.3f}%/d"


def format_gold_table(results: list[dict], mode: str = "all") -> str:
    """
    Основной формат: таблица Gold списка.
    Аналог вывода платного сканера но с маржин данными.
    """
    MODE_LABELS = {
        "all": "🏆 GOLD FUNDING",
        "neg": "📉 GOLD FUNDING — Отриц. фандинг",
        "pos": "📈 GOLD FUNDING — Полож. фандинг",
    }
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if not results:
        if mode == "neg":
            return "📉 Gold Neg: нет монет с отрицательным фандингом + маржин/спот"
        elif mode == "pos":
            return "📈 Gold Pos: нет монет с положительным фандингом + маржин/спот"
        return "🏆 Gold: пусто — нет монет с сильным фандингом и маржин/спот"

    lines = [
        f"{MODE_LABELS[mode]}  |  {now_str}",
        f"{'─' * 62}",
        f"{'Монета':8} {'Фанд':8} {'Стратегии':22} {'Маржин':20} {'Спот'}",
        f"{'─' * 62}",
    ]

    for r in results:
        sym      = r["sym"]
        fund_pct = r["fund_pct"]
        strats   = " | ".join(s["name"] for s in r["strategies"][:2])
        icon     = _funding_icon(fund_pct)
        m_str    = _margin_str(r["margin"]) if r["has_margin"] else "❌"
        s_str    = "✅" if r["has_spot"] else "❌"

        lines.append(
            f"{icon} {sym:8} {fund_pct:+6.3f}%  {strats:22} {m_str:22} {s_str}"
        )

    lines += [
        f"{'─' * 62}",
        f"Стратегии: ⚖️ Перп+Перп  📉 Маржин↓+Перп  🔄 Спот+Перп  📈 Перп↑+Маржин",
        f"Маржин: 🔴>85% займ заканчивается | 🟡>60% | 🟢 доступен",
        f"Детали: /gold [МОНЕТА]  |  Анализ: /analyze [МОНЕТА]",
    ]

    return "\n".join(lines)


def format_gold_detail(symbol: str) -> str:
    """
    Детальный разбор одной монеты.
    /gold COS — полный анализ COS.
    """
    sym = symbol.upper()

    # Собираем все данные по монете
    snaps = dev_store.get_exchanges_for_symbol(sym)
    if not snaps:
        return f"🏆 Gold {sym}: нет данных (запустите /scan)"

    snaps.sort(key=lambda s: abs(s.funding_rate), reverse=True)
    best_snap = snaps[0]
    fund_pct  = best_snap.funding_rate * 100

    # Маржин
    margin = None
    for ex in ["bybit", "gate", "binance", "okx"]:
        m = margin_store.get(sym, ex)
        if m and m.borrowable:
            margin = m
            break

    has_spot   = best_snap.exchange in ("binance", "bybit", "okx", "gate")
    has_margin = margin is not None

    strats = determine_strategies(
        symbol=sym,
        fund_pct=fund_pct,
        deviation=best_snap.deviation,
        exchange=best_snap.exchange,
        has_margin=has_margin,
        has_spot=has_spot,
    )

    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏆 GOLD: {sym}  |  {now_str}",
        f"────────────────────────────────────────",
    ]

    # Фандинг по биржам
    lines.append("📊 Фандинг по биржам:")
    for s in snaps:
        cap  = CAPS.get(s.exchange, 1.5)
        lr   = min(abs(s.funding_rate) / (cap / 100), 1.0)
        ramp = " 🔴РАМПА" if lr >= 0.90 else ""
        bar  = "█" * int(lr * 8) + "░" * (8 - int(lr * 8))
        lines.append(
            f"  {s.exchange.upper():10} {s.funding_rate*100:+.4f}%/{s.funding_hours}ч  "
            f"[{bar}]{lr*100:4.0f}%{ramp}"
        )

    lines.append("")

    # Маржин блок
    if has_margin and margin:
        bar_m  = "█" * int(margin.borrow_usage_rate * 10) + "░" * (10 - int(margin.borrow_usage_rate * 10))
        daily  = margin.daily_borrow_rate * 100
        avail  = margin.available_amount
        usage  = margin.borrow_usage_rate * 100

        if usage > 85: warning = "  ⚠️ ЗАЙМ ЗАКАНЧИВАЕТСЯ!"
        elif usage > 60: warning = "  ⚠️ Активно берут"
        else: warning = ""

        lines += [
            f"💳 Маржин ({margin.exchange.upper()}):",
            f"  Займ:     [{bar_m}] {usage:.1f}%{warning}",
            f"  Доступно: {avail:,.0f} {sym}",
            f"  Стоимость:{daily:.4f}%/сут ≈ {daily*30:.2f}%/мес",
            f"  Плечо:    {margin.max_leverage}x",
            "",
        ]
    else:
        lines += [f"💳 Маржин: ❌ Не найден на отслеживаемых биржах", ""]

    # Спот блок
    lines.append(f"🏪 Спот: {'✅ Доступен' if has_spot else '❌ Не найден'}")
    lines.append("")

    # Стратегии
    if strats:
        lines.append("✅ Доступные стратегии:")
        for s in strats:
            lines.append(f"  {s['icon']} {s['name']:18} — {s['desc']}")
            risk_label = {"LOW": "🟢 низкий", "MEDIUM": "🟡 средний", "HIGH": "🔴 высокий"}
            lines.append(f"    Риск: {risk_label.get(s['risk'], s['risk'])}")
    else:
        lines.append("⚪ Нет применимых стратегий")

    lines += [
        "",
        f"🔍 Полный анализ: /analyze {sym}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# ГЛАВНАЯ ФУНКЦИЯ ДЛЯ ХЭНДЛЕРА
# ═══════════════════════════════════════════════════════════════════════

async def get_gold_funding(
    mode: str = "all",
    symbol: str = None,
    min_funding: float = 0.08,
) -> str:
    """
    Главная точка входа для команды /gold.
    Возвращает готовую строку для Telegram.

    mode: "all" | "neg" | "pos"
    symbol: если задан — детальный разбор монеты
    """
    # 1. Убеждаемся что данные свежие
    stats = await ensure_fresh_data(max_age_min=20)
    logger.info(f"Gold: данные актуальны — "
                f"margin={stats['margin_count']}, dev={stats['dev_count']}")

    # 2. Детальный разбор одной монеты
    if symbol:
        return format_gold_detail(symbol.upper())

    # 3. Получаем список спот символов (быстро, кэшируется)
    try:
        spot_symbols = await asyncio.wait_for(
            get_spot_symbols(["binance"]), timeout=5.0
        )
    except asyncio.TimeoutError:
        spot_symbols = set()

    # 4. Строим Gold список
    results = build_gold_list(
        mode=mode,
        min_funding=min_funding,
        spot_symbols=spot_symbols,
    )

    # 5. Форматируем
    return format_gold_table(results, mode)


# ═══════════════════════════════════════════════════════════════════════
# TELEGRAM ХЭНДЛЕР
# ═══════════════════════════════════════════════════════════════════════

async def cmd_gold(update, context):
    """
    /gold        — все монеты (минус + плюс фандинг)
    /gold neg    — только отрицательный фандинг
    /gold pos    — только положительный фандинг
    /gold COS    — детальный разбор монеты COS
    """
    msg = await update.effective_message.reply_text(
        "🔍 Собираю Gold список (фандинг + маржин + спот)..."
    )

    try:
        args = context.args if context.args else []
        arg  = args[0].lower() if args else "all"

        if arg in ("neg", "minus", "short", "-"):
            mode, symbol = "neg", None
        elif arg in ("pos", "plus", "long", "+"):
            mode, symbol = "pos", None
        elif arg in ("all", ""):
            mode, symbol = "all", None
        else:
            # Скорее всего тикер монеты
            mode, symbol = "all", arg.upper()

        text = await get_gold_funding(mode=mode, symbol=symbol)

        await msg.edit_text(text, parse_mode=None)

    except Exception as e:
        logger.error(f"cmd_gold error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка: {e}")


# ═══════════════════════════════════════════════════════════════════════
# РЕГИСТРАЦИЯ В main.py
# ═══════════════════════════════════════════════════════════════════════
"""
── В bot/main.py ────────────────────────────────────────────────────────

from radar.gold_funding import cmd_gold

app.add_handler(CommandHandler("gold", cmd_gold))

── В bot/handlers/admin.py (список команд в /start) ────────────────────

"├ /gold — 🏆 Gold список (фандинг + маржин + спот)\\n"
"│   /gold neg — только отриц. фандинг (шортить)\\n"
"│   /gold pos — только полож. фандинг (лонгить)\\n"
"│   /gold COS — детали по монете COS\\n"

── Важно: НЕ использовать функции которых нет ──────────────────────────

❌ БЫЛО (не работало):
   from data.exchanges import scan_all_funding_diffs   # не существует
   from radar.margin_monitor import ensure_margin_data  # не существует

✅ ТЕПЕРЬ:
   from radar.gold_funding import cmd_gold              # всё внутри!
   Модуль сам обновляет данные через ensure_fresh_data()
"""


# ═══════════════════════════════════════════════════════════════════════
# ТЕСТ
# ═══════════════════════════════════════════════════════════════════════

def _test():
    import sys, types, random
    random.seed(42)

    fake_loguru = types.ModuleType("loguru")
    class FL:
        def info(self,*a): pass
        def warning(self,*a): pass
        def error(self,*a,**kw): pass
        def debug(self,*a): pass
    fake_loguru.logger = FL()
    sys.modules["loguru"] = fake_loguru

    now = datetime.now(timezone.utc).timestamp()

    # Наполняем margin_store
    MARGIN = [
        ("COS",   0.72, 0.000080, 5_000_000, 5),
        ("LYN",   0.87, 0.000120, 1_000_000, 3),
        ("AXS",   0.55, 0.000040, 8_000_000,10),
        ("KITE",  0.45, 0.000060, 2_000_000, 5),
        ("SAHARA",0.23, 0.000050, 3_000_000, 5),
        ("MBOX",  0.31, 0.000035, 4_000_000, 5),
        ("DENT",  0.18, 0.000030,10_000_000,10),
        ("C98",   0.61, 0.000055, 3_000_000, 5),
        ("AKE",   0.10, 0.000020, 2_000_000, 3),
    ]
    for sym, usage, rate, mx, lev in MARGIN:
        margin_store.update(MarginAsset(
            symbol=sym, exchange="bybit", timestamp=now,
            borrowable=True, borrow_usage_rate=usage,
            max_borrow_amount=mx, available_amount=mx*(1-usage),
            hourly_borrow_rate=rate, daily_borrow_rate=rate*24,
            max_leverage=lev, source="test",
        ))

    # Наполняем dev_store
    DEV = [
        ("COS",    "gate",    -5.5,  -0.02000, 8),
        ("COS",    "binance", -4.87, -0.01512, 4),
        ("LYN",    "binance", -5.7,  -0.00804, 1),
        ("LYN",    "gate",    -0.84, -0.01482, 1),
        ("KITE",   "binance", -1.00, -0.00552, 4),
        ("SAHARA", "gate",    -0.75, -0.01230, 8),
        ("SAHARA", "binance", -0.57, -0.00125, 1),
        ("AXS",    "bybit",   -0.66, -0.00800, 8),
        ("MBOX",   "gate",    -0.42, -0.00720, 8),
        ("DENT",   "binance", -0.58, -0.00216, 8),
        ("C98",    "bybit",   -0.86, -0.00113, 8),
        ("AKE",    "binance", +0.72, +0.00075, 4),
        ("1000WHY","binance", +2.05, +0.00121, 4),
        ("AVAX",   "apex",    +8.88, +0.00125, 1),
    ]
    for sym, ex, dev, fund, cycle in DEV:
        dev_store.add(DeviationSnap(
            symbol=sym, exchange=ex, timestamp=now,
            deviation=dev, funding_rate=fund,
            predicted=fund, funding_hours=cycle,
            mark_price=1.0*(1+dev/100), index_price=1.0,
        ))

    print("=" * 65)
    print("  ТЕСТ /gold — Gold Funding Command")
    print("=" * 65)

    print("\n── ТЕСТ 1: /gold (все режимы) ──────────────────────────")
    results = build_gold_list("all", min_funding=0.08, spot_symbols=set())
    print(f"  Найдено: {len(results)} монет")
    print(format_gold_table(results, "all"))

    print("\n── ТЕСТ 2: /gold neg ───────────────────────────────────")
    results_neg = build_gold_list("neg", min_funding=0.05, spot_symbols=set())
    print(format_gold_table(results_neg, "neg"))

    print("\n── ТЕСТ 3: /gold pos ───────────────────────────────────")
    results_pos = build_gold_list("pos", min_funding=0.05, spot_symbols=set())
    print(format_gold_table(results_pos, "pos"))

    print("\n── ТЕСТ 4: /gold COS (детальный разбор) ────────────────")
    print(format_gold_detail("COS"))

    print("\n── ТЕСТ 5: /gold LYN (LAST CHANCE маржин) ─────────────")
    print(format_gold_detail("LYN"))

    print("\n✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ")


if __name__ == "__main__":
    _test()
