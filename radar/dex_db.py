# radar/dex_db.py
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Optional, Dict, List, Set
from loguru import logger
import httpx

# ════════════════════════════════════════════════════════════════
# КОНФИГ
# ════════════════════════════════════════════════════════════════

DB_PATH      = os.path.join("data", "dex_symbols.json")
CUSTOM_PATH  = os.path.join("data", "dex_custom.txt") # Ручной список
HTTP_TO      = 15
HEADERS      = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Accept":     "application/json",
}

DEX_ICONS = {
    "apex":        "🔷",
    "raydium":     "🌊",
    "aster":       "⚡",
    "hyperliquid": "🌀",
    "dydx":        "🦣",
    "aevo":        "🟣",
    "bluefin":     "🐬",
    "drift":       "🌀",
    "gmx":         "🫐",
    "jupiter":     "🪐",
}

# ════════════════════════════════════════════════════════════════
# УТИЛИТЫ ОЧИСТКИ
# ════════════════════════════════════════════════════════════════

def _clean_ticker(raw: str) -> str:
    if not raw: return ""
    s = str(raw).upper().strip()
    for pref in ["PERP_", "1000", "1000000", "S-"]:
        if s.startswith(pref): s = s[len(pref):]
    s = s.replace("/", "").replace("-", "").replace("_", "").replace(":", "")
    for suff in ["USDT", "USDC", "USD", "PERP", "P"]:
        if s.endswith(suff) and len(s) > len(suff):
            s = s[:-len(suff)]
    return s

# ════════════════════════════════════════════════════════════════
# FETCHERS (ApeX, Raydium, HL, Aevo, и т.д.)
# ════════════════════════════════════════════════════════════════

async def _get(client: httpx.AsyncClient, url: str, method="GET", json_data=None) -> Optional[dict]:
    try:
        r = await client.post(url, json=json_data) if method == "POST" else await client.get(url)
        return r.json() if r.status_code in [200, 201] else None
    except: return None

async def fetch_apex_symbols(client: httpx.AsyncClient) -> list[str]:
    d = await _get(client, "https://api.omni.apex.exchange/api/v3/symbols-list")
    if d: return [_clean_ticker(r.get("symbol")) for r in d.get("data", {}).get("list", [])]
    return []

async def fetch_raydium_symbols(client: httpx.AsyncClient) -> list[str]:
    d = await _get(client, "https://api-evm.orderly.org/v1/public/futures")
    if d: return [_clean_ticker(r.get("symbol")) for r in d.get("data", {}).get("rows", [])]
    return []

async def fetch_aster_symbols(client: httpx.AsyncClient) -> list[str]:
    d = await _get(client, "https://fapi.asterdex.com/fapi/v1/exchangeInfo")
    if d: return [_clean_ticker(s.get("symbol")) for s in d.get("symbols", [])]
    return []

async def fetch_hyperliquid_symbols(client: httpx.AsyncClient) -> list[str]:
    d = await _get(client, "https://api.hyperliquid.xyz/info", "POST", {"type": "metaAndAssetCtxs"})
    if d:
        universe = d[0].get("universe", []) if isinstance(d, list) else []
        return [_clean_ticker(c.get("name")) for c in universe]
    return []

async def fetch_aevo_symbols(client: httpx.AsyncClient) -> list[str]:
    d = await _get(client, "https://api.aevo.xyz/markets")
    if isinstance(d, list): return [_clean_ticker(m.get("instrument_name")) for m in d]
    return []

# ════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ КЛАСС
# ════════════════════════════════════════════════════════════════

class DexSymbolDB:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._data: Dict[str, Set[str]] = {}
        self._ts: str = ""
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    doc = json.load(f)
                    for dex, info in doc.get("sources", {}).items():
                        self._data[dex] = set(info.get("symbols", []))
                    self._ts = doc.get("updated_at", "")
            # Подгружаем ручной список
            self._load_custom()
        except Exception as e: logger.warning(f"DexDB load error: {e}")

    def _load_custom(self):
        """Читает data/dex_custom.txt в формате 'DEX:SYMBOL' (напр. 'apex:SAHARA')"""
        if not os.path.exists(CUSTOM_PATH): return
        try:
            with open(CUSTOM_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        dex, sym = line.strip().lower().split(":", 1)
                        if dex in DEX_ICONS:
                            if dex not in self._data: self._data[dex] = set()
                            self._data[dex].add(_clean_ticker(sym))
            logger.info(f"Custom DEX symbols loaded from {CUSTOM_PATH}")
        except Exception as e: logger.error(f"Error loading custom symbols: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            now_s = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            doc = {
                "updated_at": now_s,
                "sources": {
                    dex: {"count": len(syms), "symbols": sorted(list(syms))}
                    for dex, syms in self._data.items()
                }
            }
            with open(self.path, "w", encoding="utf-8") as f: json.dump(doc, f, indent=2)
        except Exception as e: logger.error(f"DexDB save error: {e}")

    def where_listed(self, symbol: str) -> List[str]:
        target = _clean_ticker(symbol)
        if not target: return []
        alternatives = {target}
        if target.endswith("U") and len(target) > 2: alternatives.add(target[:-1])
        
        found_dexes = []
        for dex, syms in self._data.items():
            if any(a in syms for a in alternatives): found_dexes.append(dex)
        return found_dexes

    def format_dex_line(self, symbol: str) -> str:
        dexes = self.where_listed(symbol)
        if not dexes: return ""
        items = []
        sorted_dexes = sorted(dexes, key=lambda x: (x != "apex", x))
        for d in sorted_dexes:
            icon = DEX_ICONS.get(d, "•")
            items.append(f"{icon} {d.capitalize()}")
        return "DEX: " + " | ".join(items)

    async def refresh_all(self):
        logger.info("🔄 Refreshing DEX Symbol Registry...")
        async with httpx.AsyncClient(timeout=HTTP_TO, headers=HEADERS, follow_redirects=True) as client:
            tasks = {
                "apex": fetch_apex_symbols(client),
                "raydium": fetch_raydium_symbols(client),
                "aster": fetch_aster_symbols(client),
                "hyperliquid": fetch_hyperliquid_symbols(client),
                "aevo": fetch_aevo_symbols(client),
            }
            keys = list(tasks.keys())
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            
            from core.database import update_dex_assets
            for dex, res in zip(keys, results):
                if isinstance(res, list) and res:
                    clean_res = sorted(list(set(filter(None, res))))
                    self._data[dex] = set(clean_res)
                    await update_dex_assets(dex, clean_res)
                elif isinstance(res, Exception): logger.error(f"{dex} failed: {res}")
            
            self._load_custom() # Перезагружаем ручные поверх
            self._save()
            logger.info("✅ DEX Symbol Registry updated.")

dex_db = DexSymbolDB()

# ── БОТ КОМАНДЫ ──────────────────────────────────────────────────

async def cmd_dex(update, context):
    args = context.args or []
    if not args:
        lines = [f"📊 *DEX Registry*", f"Обновлено: {dex_db._ts}", ""]
        for dex, syms in sorted(dex_db._data.items()):
            lines.append(f"{DEX_ICONS.get(dex, '•')} {dex.capitalize()}: {len(syms)} монет")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    arg = args[0].upper()
    if arg == "REFRESH":
        await update.effective_message.reply_text("🔄 Обновляю базу DEX...")
        await dex_db.refresh_all()
        await update.effective_message.reply_text("✅ База обновлена!")
        return

    dexes = dex_db.where_listed(arg)
    if dexes:
        await update.effective_message.reply_text(f"🔍 *{arg}* найден:\n\n{dex_db.format_dex_line(arg)}", parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(f"❌ *{arg}* не найден. Можете добавить в `data/dex_custom.txt` в формате `apex:{arg}`", parse_mode="Markdown")
