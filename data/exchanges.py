# data/exchanges.py
# ccxt async: get_all_rates + get_orderbook + AUTO-SCAN всех перпетуалов
# Все запросы — параллельно через asyncio.gather + Redis кэш

import asyncio
import ccxt.async_support as ccxt
from loguru import logger
from core.redis_cache import cache_get, cache_set
from data.fees import EXCHANGES

# Список ID бирж для АВТО-СКАНА (только те, кто поддерживает fetch_funding_rates за 1 запрос)
EXCHANGE_IDS = ["binance", "bybit", "gate", "mexc", "bitget", "okx", "bingx", "coinex"]

# Все поддерживаемые биржи (для ручных запросов /funding)
ALL_EXCHANGE_IDS = [ex for ex, cfg in EXCHANGES.items() if cfg.get("ok", True)]

# Минимальный объём торгов за 24ч в USD чтобы попасть в авто-скан
MIN_VOLUME_USD = 500_000  # $500k/24ч — убирает монеты-призраки


def _make_exchange(name: str):
    """
    Инициализирует CCXT объект биржи с правильными параметрами.
    """
    name = name.lower()
    # Обработка маппинга если имена в FEES отличаются от имен в CCXT
    ccxt_id = name
    if name == "hyperliq":
        ccxt_id = "hyperliquid"
    
    if not hasattr(ccxt, ccxt_id):
        return None

    ex_class = getattr(ccxt, ccxt_id)
    
    # Конфигурация по умолчанию для фьючерсов/свопов
    options = {}
    if name == "binance":
        options = {"defaultType": "future"}
    elif name == "bybit":
        options = {"defaultType": "linear"}
    else:
        options = {"defaultType": "swap"}

    try:
        ex = ex_class({"options": options, "enableRateLimit": True})
        return ex
    except Exception as e:
        logger.error(f"Error creating exchange {name}: {e}")
        return None


# ── AUTO-SCAN: все символы со всех бирж за 1 запрос ──────────────────

async def _fetch_all_rates_from_exchange(ex_name: str) -> dict[str, float]:
    """
    Загружает ВСЕ перпетуальные символы с биржи и их funding rates за 1 запрос.
    Возвращает {base_symbol: rate_pct}
    """
    ex = _make_exchange(ex_name)
    if not ex:
        return {}
    try:
        # Пытаемся получить все ставки фандинга одним запросом
        if not hasattr(ex, 'fetch_funding_rates'):
            # Если биржа не поддерживает fetch_funding_rates, 
            # придется сканировать по одному (но в авто-скане это медленно)
            # В v8 мы рассчитываем что основные TIER-1 поддерживают это.
            return {}
            
        rates_raw = await ex.fetch_funding_rates()
        result = {}
        for market_id, info in rates_raw.items():
            try:
                rate = info.get("fundingRate")
                if rate is None:
                    continue
                symbol = info.get("symbol", market_id)
                
                # Фильтруем только USDT пары
                if "/USDT:USDT" in symbol:
                    base = symbol.split("/")[0]
                elif "/USDT" in symbol:
                    base = symbol.split("/")[0]
                else:
                    continue
                    
                # Фильтр по объёму
                quote_vol = (info.get("quoteVolume")
                             or info.get("info", {}).get("turnover24h")
                             or info.get("info", {}).get("volume24h"))
                if quote_vol is not None:
                    try:
                        if float(quote_vol) < MIN_VOLUME_USD:
                            continue
                    except (TypeError, ValueError):
                        pass
                result[base] = round(float(rate) * 100, 5)
            except Exception:
                continue
        return result
    except Exception as e:
        # Тихие логи для неподдерживаемых методов
        if "not supported" in str(e) or "not found" in str(e):
            logger.debug(f"{ex_name} does not support fetch_funding_rates")
        else:
            logger.debug(f"fetch_funding_rates {ex_name}: {e}")
        return {}
    finally:
        await ex.close()


async def scan_all_funding_diffs(
    threshold: float = 0.30,
    top_n: int = 20,
) -> list[dict]:
    """
    ГЛАВНАЯ ФУНКЦИЯ АВТО-СКАНА.
    """
    cache_key = f"all_diffs:{threshold}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    logger.info(f"Auto-scan: загружаю funding rates с {len(EXCHANGE_IDS)} бирж...")
    tasks = [_fetch_all_rates_from_exchange(ex) for ex in EXCHANGE_IDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    symbol_rates: dict[str, dict[str, float]] = {}
    for ex_name, item in zip(EXCHANGE_IDS, results):
        if isinstance(item, Exception) or not item:
            continue
        for sym, rate in item.items():
            if sym not in symbol_rates:
                symbol_rates[sym] = {}
            symbol_rates[sym][ex_name] = rate

    diffs = []
    for sym, rates in symbol_rates.items():
        if len(rates) < 2:
            continue
        max_ex = max(rates, key=lambda k: rates[k])
        min_ex = min(rates, key=lambda k: rates[k])
        diff = round(rates[max_ex] - rates[min_ex], 5)
        if diff >= threshold:
            diffs.append({
                "symbol": sym,
                "max_ex": max_ex,
                "min_ex": min_ex,
                "max_rate": rates[max_ex],
                "min_rate": rates[min_ex],
                "diff": diff,
                "rates": rates,
                "exchanges": len(rates),
            })

    diffs.sort(key=lambda x: x["diff"], reverse=True)
    top = diffs[:top_n]
    logger.info(
        f"Auto-scan: {len(symbol_rates)} символов, "
        f"{len(diffs)} аномалий >= {threshold}%, топ {len(top)}"
    )
    await cache_set(cache_key, top, ttl=900)
    return top


async def get_all_active_symbols(min_exchanges: int = 2) -> list[str]:
    cache_key = f"active_symbols:{min_exchanges}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    tasks = [_fetch_all_rates_from_exchange(ex) for ex in EXCHANGE_IDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    symbol_count: dict[str, int] = {}
    for item in results:
        if isinstance(item, Exception) or not item:
            continue
        for sym in item:
            symbol_count[sym] = symbol_count.get(sym, 0) + 1

    active = sorted(
        [s for s, cnt in symbol_count.items() if cnt >= min_exchanges],
        key=lambda s: symbol_count[s], reverse=True
    )
    await cache_set(cache_key, active, ttl=1800)
    return active


# ── Функции для конкретного символа (/analyze, /funding) ─────────────

async def _get_rate(ex_name: str, symbol: str) -> tuple[str, float | None]:
    ex = _make_exchange(ex_name)
    if not ex:
        return ex_name, None
    try:
        ticker_symbol = f"{symbol}/USDT:USDT"
        info = await ex.fetch_funding_rate(ticker_symbol)
        rate = info.get("fundingRate")
        if rate is not None:
            return ex_name, round(float(rate) * 100, 5)
    except Exception:
        pass
    finally:
        if ex:
            await ex.close()
    return ex_name, None


async def get_all_rates(symbol: str) -> dict[str, float]:
    """Funding rates конкретного символа со всех поддерживаемых бирж."""
    cache_key = f"rates:{symbol}"
    cached = await cache_get(cache_key)
    if cached:
        return cached
    tasks = [_get_rate(ex, symbol) for ex in ALL_EXCHANGE_IDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rates = {}
    for item in results:
        if isinstance(item, (Exception, type(None))):
            continue
        try:
            ex_name, rate = item
            if rate is not None:
                rates[ex_name] = rate
        except Exception:
            continue
    await cache_set(cache_key, rates, ttl=900)
    return rates


async def _get_orderbook(ex_name: str, symbol: str) -> dict:
    ex = _make_exchange(ex_name)
    if not ex:
        return {}
    try:
        ticker_symbol = f"{symbol}/USDT:USDT"
        ob = await ex.fetch_order_book(ticker_symbol, limit=20)
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        price = bids[0][0] if bids else 1.0
        bid_vol = sum(b[1] * price for b in bids[:5])
        return {"bids": bids[:20], "asks": asks[:20], "depth_usd": round(bid_vol, 2)}
    except Exception:
        return {}
    finally:
        await ex.close()


async def get_orderbook_depth(ex_name: str, symbol: str) -> dict:
    cache_key = f"ob:{ex_name}:{symbol}"
    cached = await cache_get(cache_key)
    if cached:
        return cached
    data = await _get_orderbook(ex_name, symbol)
    if data:
        await cache_set(cache_key, data, ttl=120)
    return data


async def get_btc_ohlcv(timeframe: str = "1h", limit: int = 24) -> list:
    cache_key = f"btc_ohlcv:{timeframe}:{limit}"
    cached = await cache_get(cache_key)
    if cached:
        return cached
    ex = ccxt.binanceusdm()
    try:
        ohlcv = await ex.fetch_ohlcv("BTC/USDT:USDT", timeframe=timeframe, limit=limit)
        await cache_set(cache_key, ohlcv, ttl=300)
        return ohlcv
    except Exception:
        return []
    finally:
        await ex.close()
