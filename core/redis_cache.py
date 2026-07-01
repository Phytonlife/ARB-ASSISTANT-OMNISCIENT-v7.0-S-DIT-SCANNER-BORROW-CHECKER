# core/redis_cache.py
# Если Redis недоступен — graceful fallback на in-memory dict

import json
import time
import redis.asyncio as aioredis
from loguru import logger
from core.config import settings

_redis = None
_fallback: dict = {}          # in-memory fallback
_fallback_ttl: dict = {}      # TTL для fallback


async def get_redis():
    global _redis
    if _redis is None:
        try:
            _redis = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            await _redis.ping()
            logger.info("Redis connected")
        except Exception as e:
            logger.warning(f"Redis unavailable: {e} — using in-memory fallback")
            _redis = None
    return _redis


async def cache_get(key: str):
    r = await get_redis()
    try:
        if r:
            val = await r.get(key)
            return json.loads(val) if val else None
    except Exception:
        pass
    # fallback
    entry = _fallback.get(key)
    if entry is not None:
        exp = _fallback_ttl.get(key, 0)
        if exp == 0 or time.time() < exp:
            return entry
        else:
            _fallback.pop(key, None)
            _fallback_ttl.pop(key, None)
    return None


async def cache_set(key: str, value, ttl: int = 900):
    r = await get_redis()
    try:
        if r:
            await r.setex(key, ttl, json.dumps(value, ensure_ascii=False))
            return
    except Exception:
        pass
    # fallback
    _fallback[key] = value
    _fallback_ttl[key] = time.time() + ttl


async def cache_delete(key: str):
    r = await get_redis()
    try:
        if r:
            await r.delete(key)
            return
    except Exception:
        pass
    _fallback.pop(key, None)
    _fallback_ttl.pop(key, None)
