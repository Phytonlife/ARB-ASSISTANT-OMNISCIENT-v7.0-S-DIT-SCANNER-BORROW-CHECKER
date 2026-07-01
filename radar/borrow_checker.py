"""
radar/borrow_checker.py — ФИНАЛЬНАЯ ВЕРСИЯ v2
==============================================
Все исправления из аудита знакомого + 9 дополнительных исправлений.

ИСПРАВЛЕНО vs оригинала:
  [1] deque(maxlen=720) вместо list + чистка при каждом append
  [2] Глобальная aiohttp сессия (SESSION) — одна на модуль
  [3] Все 3 запроса basis_proxy в одном asyncio.gather
  [4] Правильная нормализация фандинга: только ApeX/HL = ×8
  [5] get_trend_analysis: acceleration (5мин vs 1мин vs сейчас)
  [6] Retry (1 попытка) при timeout — не теряем данные
  [7] hmac.new → hmac.new(key, msg, digestmod) — правильный синтаксис
  [8] Интеграция с dev_store для basis proxy (не нужен ccxt)
  [9] GC чистит deque а не list

Уровни проверки:
  Уровень 1 (без ключей): Gate P2P, OKX utilization
  Уровень 1.5 (без ключей): CoinEx, KuCoin, Bitget, MEXC
  Уровень 2 (Read-Only ключи): Bybit UTA, Binance Cross
  Уровень 3 (без ключей): Basis-прокси через dev_store данные
"""

import asyncio
import hashlib
import hmac
import time
from collections import defaultdict, deque
from typing import Optional

import aiohttp

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# Ключи из конфига — если нет, Уровень 2 пропускается
from core.config import settings

BYBIT_API_KEY = settings.bybit_api_key
BYBIT_SECRET = settings.bybit_api_secret
BINANCE_API_KEY = settings.binance_api_key
BINANCE_SECRET = settings.binance_api_secret


# ════════════════════════════════════════════════════════════════
# FIX #1: deque вместо list — O(1) вместо O(n)
# 720 точек × 5 сек = 1 час истории без ручной чистки
# ════════════════════════════════════════════════════════════════

borrow_rate_history: dict = defaultdict(lambda: deque(maxlen=720))


def save_borrow_history(
    symbol: str,
    exchange: str,
    rate: float,
    utilization: float = None,
    available: float = None,
):
    """Сохраняет в историю. deque сама удаляет старые данные."""
    key = (symbol, exchange)
    borrow_rate_history[key].append({
        "timestamp": time.time(),
        "rate":        rate,
        "utilization": utilization,
        "available":   available,
    })


# ════════════════════════════════════════════════════════════════
# FIX #2: Глобальная сессия — создаётся один раз
# Экономит 50-80мс на каждом запросе (нет повторного TLS handshake)
# ════════════════════════════════════════════════════════════════

_SESSION: Optional[aiohttp.ClientSession] = None
_SESSION_LOCK = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    """Возвращает или создаёт глобальную aiohttp сессию."""
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        async with _SESSION_LOCK:
            if _SESSION is None or _SESSION.closed:
                connector = aiohttp.TCPConnector(
                    limit=20,           # максимум 20 одновременных соединений
                    ttl_dns_cache=300,  # кэш DNS 5 минут
                    enable_cleanup_closed=True,
                )
                _SESSION = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=5),
                )
    return _SESSION


async def close_session():
    """Закрыть сессию при выключении бота."""
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
        _SESSION = None


# ════════════════════════════════════════════════════════════════
# FIX #6: Утилита retry при timeout — 1 повторная попытка
# ════════════════════════════════════════════════════════════════

async def _get_json(url: str, params: dict = None,
                    headers: dict = None, retries: int = 1) -> Optional[dict]:
    """GET запрос с retry. Возвращает dict или None."""
    session = await _get_session()
    for attempt in range(retries + 1):
        try:
            async with session.get(url, params=params,
                                   headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status == 200:
                    return await r.json()
                if r.status == 404:
                    return {"_status": 404}
                logger.debug(f"HTTP {r.status}: {url}")
                return None
        except asyncio.TimeoutError:
            if attempt < retries:
                await asyncio.sleep(0.3)
                continue
            return None
        except Exception as e:
            logger.debug(f"_get_json {url}: {e}")
            return None
    return None


# ════════════════════════════════════════════════════════════════
# УРОВЕНЬ 1: БЕЗ КЛЮЧЕЙ — РЕАЛЬНЫЕ ПУЛЫ
# ════════════════════════════════════════════════════════════════

async def check_gate_borrow(symbol: str) -> dict:
    """Gate.io P2P стакан займов."""
    data = await _get_json(
        "https://api.gateio.ws/api/v4/margin/funding_book",
        params={"currency": symbol.upper(), "limit": 10},
    )

    if data is None:
        return {"exchange": "gate", "borrowable": None, "reason": "timeout"}

    if isinstance(data, dict) and data.get("_status") == 404:
        return {"exchange": "gate", "symbol": symbol, "borrowable": False,
                "reason": "монета не в маржинальном списке Gate"}

    if not isinstance(data, list) or not data:
        return {"exchange": "gate", "symbol": symbol,
                "borrowable": False, "reason": "пул пуст"}

    best = data[0]
    total_available = sum(float(x.get("amount", 0)) for x in data)
    raw_rate = float(best.get("rate", 0))
    days = int(best.get("days", 1))

    rate_per_day_pct = (raw_rate * 100) / max(days, 1)
    save_borrow_history(symbol, "gate", raw_rate / max(days, 1),
                        available=total_available)

    return {
        "exchange":         "gate",
        "symbol":           symbol,
        "borrowable":       total_available > 0,
        "available_amount": round(total_available, 2),
        "rate_per_day_pct": round(rate_per_day_pct, 4),
        "pool_depth":       len(data),
        "confidence":       "HIGH",
    }


async def check_okx_borrow(symbol: str) -> dict:
    """OKX utilization — лучший индикатор скорости исчезновения пула."""
    data = await _get_json(
        "https://www.okx.com/api/v5/finance/savings/lending-rate-summary",
        params={"ccy": symbol.upper()},
    )

    if not data or not data.get("data"):
        return {"exchange": "okx", "symbol": symbol, "borrowable": False,
                "reason": "нет данных" if data is not None else "timeout"}

    item = data["data"][0]
    available    = float(item.get("lendingAmt", 0))
    utilization  = float(item.get("utilizationRate", 0))

    pool_risk = (
        "🚨 КРИТИЧНО — исчезнет через минуты" if utilization >= 0.95 else
        "⚠️  ВЫСОКИЙ — брать немедленно"      if utilization >= 0.85 else
        "⚡ СРЕДНИЙ — мониторить"              if utilization >= 0.70 else
        "✅ НИЗКИЙ — безопасно"
    )

    save_borrow_history(symbol, "okx",
                        float(item.get("estRate", 0)),
                        utilization=utilization, available=available)

    return {
        "exchange":         "okx",
        "symbol":           symbol,
        "borrowable":       available > 0,
        "available_amount": round(available, 2),
        "utilization_pct":  f"{utilization * 100:.1f}%",
        "pool_risk":        pool_risk,
        "confidence":       "HIGH",
    }


# ════════════════════════════════════════════════════════════════
# УРОВЕНЬ 1.5: БЕЗ КЛЮЧЕЙ — ДОПОЛНИТЕЛЬНЫЕ БИРЖИ
# ════════════════════════════════════════════════════════════════

async def check_coinex_borrow(symbol: str) -> dict:
    """CoinEx — реальный доступный пул и ставка. Без ключей."""
    data = await _get_json(
        "https://api.coinex.com/v2/margin/interest-limit",
        params={"market": f"{symbol.upper()}USDT"},
    )

    if not data:
        return {"exchange": "coinex", "borrowable": None, "reason": "timeout"}
    if data.get("code") != 0:
        return {"exchange": "coinex", "borrowable": False,
                "reason": data.get("message", "")}

    items = data.get("data", [])
    coin_data = next(
        (i for i in items if i.get("coin", "").upper() == symbol.upper()),
        None
    )
    if not coin_data:
        return {"exchange": "coinex", "borrowable": False}

    available  = float(coin_data.get("available_loan_amount", 0))
    daily_rate = float(coin_data.get("daily_interest_rate", 0))
    save_borrow_history(symbol, "coinex", daily_rate, available=available)

    return {
        "exchange":         "coinex",
        "symbol":           symbol,
        "borrowable":       available > 0,
        "available_amount": round(available, 2),
        "rate_per_day_pct": round(daily_rate * 100, 4),
        "confidence":       "HIGH",
    }


async def check_kucoin_borrow(symbol: str) -> dict:
    """KuCoin — подтверждаем листинг. Используем v1 эндпоинт."""
    # В v3 часто 400 без ключей, v1 более стабилен для публичного списка
    data = await _get_json("https://api.kucoin.com/api/v1/margin/currencies")

    if not data:
        return {"exchange": "kucoin", "borrowable": None, "reason": "timeout"}

    items     = data.get("data", [])
    coin_data = next(
        (i for i in items if i.get("currency", "").upper() == symbol.upper()),
        None
    )
    if not coin_data or not coin_data.get("isMarginEnabled", False):
        return {"exchange": "kucoin", "borrowable": False,
                "reason": "не в маржинальном списке"}

    # В v1 поле называется borrowRate
    min_rate = float(coin_data.get("borrowRate", 0) or coin_data.get("borrowMinInterestRate", 0))

    return {
        "exchange":         "kucoin",
        "symbol":           symbol,
        "borrowable":       True,
        "available_amount": None,
        "rate_per_day_pct": round(min_rate * 100, 4),
        "warning":          "Объём пула неизвестен без ключей",
        "confidence":       "MEDIUM",
        "note":             "KuCoin: v1 API check",
    }


async def check_bitget_borrow(symbol: str) -> dict:
    """Bitget V2 — листинг + ставка. maxBorrowAmount = тир-лимит, не пул."""
    data = await _get_json(
        "https://api.bitget.com/api/v2/margin/cross/public/interestRateAndLimit",
        params={"coin": symbol.upper()},
    )

    if not data:
        return {"exchange": "bitget", "borrowable": None, "reason": "timeout"}
    if data.get("code") != "00000" or not data.get("data"):
        return {"exchange": "bitget", "borrowable": False}

    item         = data["data"][0]
    is_borrowable = str(item.get("borrowable", "false")).lower() == "true"
    daily_rate    = float(item.get("dailyInterestRate", 0))

    return {
        "exchange":         "bitget",
        "symbol":           symbol,
        "borrowable":       is_borrowable,
        "available_amount": None,
        "rate_per_day_pct": round(daily_rate * 100, 4),
        "warning":          "Лимит тира, не реальный пул",
        "confidence":       "MEDIUM",
    }


async def check_mexc_borrow(symbol: str) -> dict:
    """MEXC — только листинг. Объём публично недоступен."""
    data = await _get_json("https://api.mexc.com/api/v3/margin/symbols")

    if not data:
        return {"exchange": "mexc", "borrowable": None, "reason": "timeout"}

    target     = f"{symbol.upper()}USDT"
    is_allowed = any(
        s.get("symbol") == target and s.get("isMarginTradingAllowed")
        for s in (data if isinstance(data, list) else [])
    )

    return {
        "exchange":         "mexc",
        "symbol":           symbol,
        "borrowable":       is_allowed,
        "available_amount": None,
        "warning":          "Объём пула недоступен без ключей",
        "confidence":       "LOW",
    }


# ════════════════════════════════════════════════════════════════
# УРОВЕНЬ 2: С READ-ONLY КЛЮЧАМИ
# ════════════════════════════════════════════════════════════════

def _bybit_sign(params_str: str) -> dict:
    """Генерация подписи для Bybit."""
    ts  = str(int(time.time() * 1000))
    rw  = "5000"
    msg = (ts + BYBIT_API_KEY + rw + params_str).encode()
    sig = hmac.new(BYBIT_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY":     BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        sig,
        "X-BAPI-RECV-WINDOW": rw,
    }


def _binance_sign(params_str: str) -> str:
    """Генерация подписи для Binance."""
    return hmac.new(
        BINANCE_SECRET.encode(),
        params_str.encode(),
        hashlib.sha256,
    ).hexdigest()


async def check_bybit_borrow(symbol: str) -> dict:
    """Bybit UTA — реальный availableToBorrow с учётом аккаунта."""
    if not BYBIT_API_KEY:
        return {"exchange": "bybit", "borrowable": None, "reason": "нет ключей"}

    base   = symbol.upper().replace("USDT", "")
    params = f"coin={base}"
    hdrs   = _bybit_sign(params)

    data = await _get_json(
        f"https://api.bybit.com/v5/account/collateral-info?{params}",
        headers=hdrs,
    )

    if not data or data.get("retCode") != 0:
        reason = (data or {}).get("retMsg", "нет данных")
        return {"exchange": "bybit", "symbol": symbol,
                "borrowable": False, "reason": reason}

    items     = data.get("result", {}).get("list", [])
    coin_data = next((i for i in items if i.get("currency") == base), None)
    if not coin_data:
        return {"exchange": "bybit", "symbol": symbol,
                "borrowable": False, "reason": "не в UTA коллатерале"}

    available      = float(coin_data.get("availableToBorrow", 0))
    already        = float(coin_data.get("borrowAmount", 0))
    max_borrow     = float(coin_data.get("maxBorrowingAmount", 0))
    is_borrowable  = coin_data.get("borrowable", False)
    hourly_rate    = float(coin_data.get("hourlyBorrowRate", 0))

    save_borrow_history(symbol, "bybit", hourly_rate, available=available)

    total = already + available
    util  = round(already / total, 3) if total > 0 else 0

    return {
        "exchange":           "bybit",
        "symbol":             symbol,
        "borrowable":         is_borrowable and available > 0,
        "available_to_borrow":round(available, 2),
        "max_borrow_amount":  round(max_borrow, 2),
        "already_borrowed":   round(already, 2),
        "daily_rate_pct":     round(hourly_rate * 24 * 100, 4),
        "utilization_approx": util,
        "confidence":         "HIGH",
    }


async def check_binance_borrow(symbol: str) -> dict:
    """Binance SAPI — лимит займа с учётом VIP-уровня."""
    if not BINANCE_API_KEY:
        return {"exchange": "binance", "borrowable": None, "reason": "нет ключей"}

    base    = symbol.upper().replace("USDT", "")
    ts      = int(time.time() * 1000)
    p_str   = f"asset={base}&isIsolated=FALSE&timestamp={ts}"
    sig     = _binance_sign(p_str)
    url     = f"https://api.binance.com/sapi/v1/margin/maxBorrowable?{p_str}&signature={sig}"

    data = await _get_json(url, headers={"X-MBX-APIKEY": BINANCE_API_KEY})

    if not data:
        return {"exchange": "binance", "borrowable": None, "reason": "timeout"}
    if "code" in data:
        return {"exchange": "binance", "symbol": symbol,
                "borrowable": False, "reason": data.get("msg", "")}

    amount = float(data.get("amount", 0))
    save_borrow_history(symbol, "binance", 0, available=amount)

    return {
        "exchange":         "binance",
        "symbol":           symbol,
        "borrowable":       amount > 0,
        "available_amount": round(amount, 2),
        "confidence":       "HIGH",
    }


# ════════════════════════════════════════════════════════════════
# УРОВЕНЬ 3: BASIS-ПРОКСИ
# ════════════════════════════════════════════════════════════════

_HOURLY_FUNDING_EXCHANGES = {"apex", "hyperliquid", "apexomni"}


def _normalize_funding_to_8h(raw_rate: float, exchange_id: str,
                               interval_sec: int = None) -> float:
    """Нормализует funding rate к эквиваленту 8ч."""
    ex = exchange_id.lower()
    if interval_sec:
        hours = interval_sec / 3600
        return raw_rate * (8 / hours)
    if any(h in ex for h in _HOURLY_FUNDING_EXCHANGES):
        return raw_rate * 8
    return raw_rate


async def check_basis_proxy(
    exchange_futures=None,
    exchange_spot=None,
    symbol: str = "",
) -> dict:
    """Basis-прокси: фьюч/спот спред + фандинг = индикатор пустого пула."""
    futures_price = spot_price = funding_8h = 0.0
    ts_diff_ms = None

    try:
        from radar.index_deviation_radar import dev_store
        snaps_f = dev_store.get_all_latest()
        best_f  = next((s for s in snaps_f
                        if s.symbol == symbol.upper()), None)
        if best_f:
            funding_8h  = best_f.funding_rate * 100
    except (ImportError, Exception):
        pass

    if exchange_futures and exchange_spot:
        try:
            futures_t, spot_t, fund_data = await asyncio.gather(
                exchange_futures.fetch_ticker(f"{symbol}/USDT:USDT"),
                exchange_spot.fetch_ticker(f"{symbol}/USDT"),
                exchange_futures.fetch_funding_rate(f"{symbol}/USDT:USDT"),
                return_exceptions=True,
            )

            if isinstance(futures_t, dict) and isinstance(spot_t, dict):
                futures_price = float(futures_t.get("last", 0))
                spot_price    = float(spot_t.get("last", 0))
                fts = futures_t.get("timestamp", 0)
                sts = spot_t.get("timestamp", 0)
                if fts and sts:
                    ts_diff_ms = abs(fts - sts)

            if isinstance(fund_data, dict):
                raw_rate    = float(fund_data.get("fundingRate", 0))
                ex_id       = str(getattr(exchange_futures, "id", "")).lower()
                funding_8h  = _normalize_funding_to_8h(raw_rate, ex_id) * 100

        except Exception as e:
            logger.debug(f"basis_proxy ccxt: {e}")

    if futures_price == 0 and spot_price == 0:
        try:
            sym_bn = f"{symbol.upper()}USDT"
            f_data, s_data = await asyncio.gather(
                _get_json("https://fapi.binance.com/fapi/v1/premiumIndex",
                          params={"symbol": sym_bn}),
                _get_json("https://api.binance.com/api/v3/ticker/price",
                          params={"symbol": sym_bn}),
            )
            if f_data:
                futures_price = float(f_data.get("markPrice", 0))
                raw_rate      = float(f_data.get("lastFundingRate", 0))
                funding_8h    = raw_rate * 100
            if s_data:
                spot_price = float(s_data.get("price", 0))
        except Exception as e:
            logger.debug(f"basis_proxy direct: {e}")

    if not futures_price or not spot_price or spot_price == 0:
        return {"method": "basis_proxy", "usable": False, "reason": "нет цены"}

    basis_pct   = (futures_price - spot_price) / spot_price * 100
    borrow_empty = funding_8h < -1.0 and basis_pct < -2.0

    signal = (
        "🚨 ЗАЙМА НЕТ (99%) — арбитраж сломан"   if borrow_empty else
        "⚠️ ЗАЙМ ИСЧЕЗАЕТ — брать сейчас"          if basis_pct < -1.0 and funding_8h < -0.5 else
        "⚡ Пул сокращается"                        if basis_pct < -0.5 else
        "✅ Займ вероятно доступен"
    )

    return {
        "method":             "basis_proxy",
        "symbol":             symbol,
        "basis_pct":          round(basis_pct, 3),
        "funding_8h_pct":     round(funding_8h, 3),
        "futures_price":      round(futures_price, 4),
        "spot_price":         round(spot_price, 4),
        "borrow_likely_empty":borrow_empty,
        "signal":             signal,
        "ts_diff_ms":         ts_diff_ms,
        "confidence":         "CERTAIN_EMPTY" if borrow_empty else "HIGH",
    }


# ════════════════════════════════════════════════════════════════
# АНАЛИЗ ТРЕНДА
# ════════════════════════════════════════════════════════════════

def get_trend_analysis(symbol: str, exchange: str) -> dict:
    """Анализирует историю + УСКОРЕНИЕ."""
    key     = (symbol, exchange)
    history = list(borrow_rate_history.get(key, []))

    if len(history) < 2:
        return {"urgency_score": 0, "urgency": "мало данных", "signals": []}

    now    = time.time()
    score  = 0
    signals = []

    def _window(minutes: float):
        cutoff = now - minutes * 60
        pts    = [r for r in history if r["timestamp"] >= cutoff]
        return pts if len(pts) >= 2 else None

    all_pts  = history
    pts_5m   = _window(5)
    pts_1m   = _window(1)

    curr = all_pts[-1]
    old  = all_pts[0]

    if old.get("rate", 0) > 0 and curr.get("rate", 0) > 0:
        ratio = curr["rate"] / old["rate"]
        if ratio >= 3.0:
            score += 3; signals.append(f"Ставка выросла в {ratio:.1f}x")
        elif ratio >= 2.0:
            score += 2; signals.append(f"Ставка выросла в {ratio:.1f}x")
        elif ratio >= 1.5:
            score += 1; signals.append(f"Ставка выросла в {ratio:.1f}x")

    if old.get("utilization") and curr.get("utilization"):
        util_now   = curr["utilization"]
        util_delta = util_now - old["utilization"]
        if util_now >= 0.90:
            score += 3; signals.append(f"Utilization {util_now*100:.0f}% — критично")
        elif util_delta >= 0.20:
            score += 2; signals.append(f"Utilization +{util_delta*100:.0f}% за период")

    if old.get("available") and curr.get("available") and old["available"] > 0:
        drop = (old["available"] - curr["available"]) / old["available"]
        if drop >= 0.70:
            score += 3; signals.append(f"Пул упал на {drop*100:.0f}%")
        elif drop >= 0.40:
            score += 2; signals.append(f"Пул упал на {drop*100:.0f}%")
        elif drop >= 0.20:
            score += 1; signals.append(f"Пул упал на {drop*100:.0f}%")

    if pts_5m:
        old_5m = pts_5m[0]
        cur_5m = pts_5m[-1]
        if (old_5m.get("available") and cur_5m.get("available")
                and old_5m["available"] > 0):
            drop_5m = (old_5m["available"] - cur_5m["available"]) / old_5m["available"]
            if drop_5m >= 0.30:
                score += 3; signals.append(f"🚨 Пул -30%+ за 5 мин! (ускорение)")
            elif drop_5m >= 0.15:
                score += 2; signals.append(f"⚡ Пул -{drop_5m*100:.0f}% за 5 мин")

        if (old_5m.get("rate", 0) > 0 and cur_5m.get("rate", 0) > 0):
            ratio_5m = cur_5m["rate"] / old_5m["rate"]
            if ratio_5m >= 2.0:
                score += 2; signals.append(f"Ставка ×{ratio_5m:.1f} за 5 мин!")

    if pts_1m:
        old_1m = pts_1m[0]
        cur_1m = pts_1m[-1]
        if (old_1m.get("available") and cur_1m.get("available")
                and old_1m["available"] > 0):
            drop_1m = (old_1m["available"] - cur_1m["available"]) / old_1m["available"]
            if drop_1m >= 0.20:
                score += 3; signals.append(f"🚨🚨 Пул -{drop_1m*100:.0f}% за 1 МИН!")

    urgency = (
        "🚨 КРИТИЧНО — занять в 5-10 мин или не входить" if score >= 7 else
        "🚨 КРИТИЧНО — занять в 10-15 мин"               if score >= 5 else
        "⚠️ ВЫСОКИЙ — занять сейчас"                     if score >= 3 else
        "⚡ СРЕДНИЙ — мониторить каждые 5 мин"           if score >= 1 else
        "✅ НИЗКИЙ — займ стабилен"
    )

    return {"urgency_score": score, "urgency": urgency, "signals": signals}


# ════════════════════════════════════════════════════════════════
# СБОРЩИК МУСОРА
# ════════════════════════════════════════════════════════════════

async def garbage_collect_borrow_history():
    """Удаляет ключи делистнутых монет (один проход)."""
    now    = time.time()
    cutoff = now - 3 * 3600
    dead   = [
        key for key, dq in list(borrow_rate_history.items())
        if not dq or dq[-1]["timestamp"] < cutoff
    ]
    for key in dead:
        del borrow_rate_history[key]
    if dead:
        logger.info(f"[GC] Удалено {len(dead)} мёртвых пар из borrow_history")


# ════════════════════════════════════════════════════════════════
# ИКОНКИ УВЕРЕННОСТИ
# ════════════════════════════════════════════════════════════════

CONFIDENCE_ICON = {
    "HIGH":          "✅",
    "MEDIUM":        "🟡",
    "LOW":           "🔵",
    "CERTAIN_EMPTY": "🚨",
}


# ════════════════════════════════════════════════════════════════
# ГЛАВНАЯ ФУНКЦИЯ
# ════════════════════════════════════════════════════════════════

async def check_all_borrow(
    symbol: str,
    exchange_futures=None,
    exchange_spot=None,
) -> str:
    """Запускает все проверки параллельно."""
    tasks = [
        check_gate_borrow(symbol),
        check_okx_borrow(symbol),
        check_coinex_borrow(symbol),
        check_kucoin_borrow(symbol),
        check_bitget_borrow(symbol),
        check_mexc_borrow(symbol),
    ]
    if BYBIT_API_KEY:
        tasks.append(check_bybit_borrow(symbol))
    if BINANCE_API_KEY:
        tasks.append(check_binance_borrow(symbol))

    tasks.append(check_basis_proxy(exchange_futures, exchange_spot, symbol))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    lines = [f"\n💳 ЗАЙМ ДЛЯ ШОРТА [{symbol}]:"]

    high_confidence   = []
    medium_confidence = []
    not_available     = []
    basis_signal      = None

    for r in results:
        if isinstance(r, Exception):
            logger.debug(f"borrow_checker exception: {r}")
            continue

        if r.get("method") == "basis_proxy":
            basis_signal = r
            continue

        if r.get("borrowable") is None:
            continue

        exchange   = r.get("exchange", "").upper()
        confidence = r.get("confidence", "LOW")
        available  = r.get("available_amount") or r.get("available_to_borrow")
        rate       = r.get("rate_per_day_pct") or r.get("daily_rate_pct", 0)
        util       = r.get("utilization_pct", "")
        risk       = r.get("pool_risk", "")
        warn       = r.get("warning", "")

        if not r.get("borrowable"):
            not_available.append(exchange)
            continue

        icon  = CONFIDENCE_ICON.get(confidence, "🔵")
        parts = [f"  {icon} {exchange}:"]
        if rate:
            parts.append(f"{rate:.3f}%/день")
        if available:
            parts.append(f"пул: ${available:,.0f}")
        else:
            parts.append("пул: скрыт")
        if util:
            parts.append(f"утил: {util}")
        if risk:
            parts.append(risk)
        if warn:
            parts.append(f"[{warn}]")

        line = " | ".join(parts)
        if confidence == "HIGH" and available:
            high_confidence.append(line)
        else:
            medium_confidence.append(line)

    if basis_signal and basis_signal.get("usable", True):
        lines.append(f"  📊 Basis: {basis_signal.get('signal', '?')}")
        bp = basis_signal.get("basis_pct", 0)
        fd = basis_signal.get("funding_8h_pct", 0)
        lines.append(f"     Basis: {bp:+.2f}% | Fund/8ч: {fd:+.3f}%")
        if basis_signal.get("ts_diff_ms", 0) > 500:
            lines.append(f"     ⚠️ Рассинхрон: {basis_signal['ts_diff_ms']}мс")
        lines.append("")

    if high_confidence:
        lines.append("  ─ Реальный пул:")
        lines.extend(high_confidence)

    if medium_confidence:
        lines.append("  ─ Листинг (объём неизвестен):")
        lines.extend(medium_confidence)

    if not_available:
        lines.append(f"  ❌ Нет займа: {', '.join(not_available)}")

    trend_lines = []
    for ex_id in ["gate", "okx", "coinex", "bybit", "binance"]:
        t = get_trend_analysis(symbol, ex_id)
        if t["urgency_score"] >= 3:
            trend_lines.append(f"  ↳ {ex_id.upper()}: {t['urgency']}")
            for sig in t["signals"]:
                trend_lines.append(f"     → {sig}")

    if trend_lines:
        lines.append("\n  📈 ТРЕНД:")
        lines.extend(trend_lines)

    lines.append("")
    basis_empty = basis_signal and basis_signal.get("borrow_likely_empty")

    if basis_empty:
        lines.append("  🔴 ВЕРДИКТ: Basis-прокси → займ НЕ доступен (99%)")
    elif high_confidence:
        lines.append("  🟢 ВЕРДИКТ: ЗАЙМ ДОСТУПЕН — можно действовать")
    elif medium_confidence:
        lines.append("  🟡 ВЕРДИКТ: ВЕРОЯТНО ДОСТУПЕН — проверить вручную")
    else:
        lines.append("  🔴 ВЕРДИКТ: ЗАЙМА НЕТ НИ НА ОДНОЙ БИРЖЕ")
        lines.append("             Шорт спота невозможен")

    return "\n".join(lines)
