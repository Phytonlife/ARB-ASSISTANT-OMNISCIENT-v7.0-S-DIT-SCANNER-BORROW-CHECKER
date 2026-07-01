# data/listings.py
# CoinMarketCal API — предстоящие листинги
# FIX: добавлена поддержка API ключа из .env

import httpx
from datetime import datetime, timedelta
from loguru import logger
from core.redis_cache import cache_get, cache_set


async def get_upcoming_listings(days: int = 7) -> list[dict]:
    """Получить листинги на ближайшие N дней."""
    cache_key = f"listings:{days}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    from core.config import settings
    api_key = getattr(settings, "coinmarketcal_api_key", "")

    url = "https://api.coinmarketcal.com/v1/events"
    params = {
        "max": 30,
        "dateRangeStart": datetime.utcnow().strftime("%Y-%m-%d"),
        "dateRangeEnd": (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d"),
        "categories": "listings",
        "sortBy": "created_desc",
    }

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    else:
        logger.warning("CoinMarketCal: нет API ключа (COINMARKETCAL_API_KEY в .env)")
        return []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("body", [])
                events = [
                    {
                        "title": e.get("title", {}).get("en", ""),
                        "date": e.get("date_event", ""),
                        "coins": [c.get("symbol", "") for c in e.get("coins", [])],
                        "description": e.get("description", ""),
                    }
                    for e in data
                ]
                await cache_set(cache_key, events, ttl=7200)
                return events
            else:
                logger.warning(f"CoinMarketCal HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"CoinMarketCal failed: {e}")

    return []
