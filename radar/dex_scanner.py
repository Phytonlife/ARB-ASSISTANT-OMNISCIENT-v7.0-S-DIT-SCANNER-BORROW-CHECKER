# radar/dex_scanner.py
import asyncio
import httpx
from loguru import logger
from core.database import update_dex_assets

async def scan_apex_assets_ccxt() -> list[str]:
    """Получает все символы с ApeX через CCXT (Omni)."""
    try:
        import ccxt.async_support as ccxt
        ex = ccxt.apex()
        try:
            tickers = await ex.fetch_tickers()
            symbols = []
            for s in tickers.keys():
                # Убираем /USDT:USDT и прочее
                # s может быть SAHARA/USDT:USDT или BTC/USDT
                base = s.split('/')[0].split('-')[0].split('_')[0].upper()
                symbols.append(base)
                # Добавляем также вариант с USDT без разделителя
                symbols.append(f"{base}USDT")
            return list(set(symbols))
        finally:
            await ex.close()
    except Exception as e:
        logger.error(f"Error scanning ApeX via CCXT: {e}")
        return []

async def scan_raydium_assets() -> list[str]:
    """Получает все символы с Raydium (Orderly Network)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://api-evm.orderly.org/v1/public/futures")
            if resp.status_code == 200:
                data = resp.json()
                rows = data.get("data", {}).get("rows", [])
                symbols = []
                for row in rows:
                    # Формат: PERP_BTC_USDC
                    raw = row.get("symbol", "").replace("PERP_", "").upper()
                    base = raw.split("_")[0]
                    symbols.append(base)
                    symbols.append(raw)
                return list(set(symbols))
    except Exception as e:
        logger.error(f"Error scanning Raydium: {e}")
    return []

async def full_dex_scan():
    """Запускает полный скан всех DEX и обновляет БД."""
    logger.info("Starting DEX Registry scan...")
    
    # ApeX
    apex_syms = await scan_apex_assets_ccxt()
    if apex_syms:
        await update_dex_assets("apex", apex_syms)
    
    # Raydium
    ray_syms = await scan_raydium_assets()
    if ray_syms:
        await update_dex_assets("raydium", ray_syms)
    
    logger.info(f"DEX Registry updated. ApeX variants: {len(apex_syms)}, Raydium: {len(ray_syms)}")

if __name__ == "__main__":
    asyncio.run(full_dex_scan())
