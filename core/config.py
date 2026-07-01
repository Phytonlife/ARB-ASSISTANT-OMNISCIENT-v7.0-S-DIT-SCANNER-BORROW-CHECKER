# core/config.py
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Telegram
    telegram_token: str = ""
    telegram_chat_id: int = 0

    # AI — Groq PRIMARY
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # AI — OpenRouter FALLBACK
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-4-maverick:free"

    # OpenAI embeddings (разово)
    openai_api_key: str = ""

    # Торговля
    deposit_usd: float = 500.0
    max_position_pct: float = 10.0
    funding_alert_threshold: float = 0.30

    # Режим скана:
    # "auto"   — сканирует ВСЕ перпетуалы автоматически (рекомендуется)
    # "manual" — только монеты из watch_symbols
    scan_mode: str = "auto"

    # Минимальный объём за 24ч для попадания в авто-скан
    min_volume_usd: float = 500_000.0

    # Фиксированный список (используется только при scan_mode=manual
    # или для /funding /analyze команд)
    watch_symbols: str = "BTC,ETH,SOL,PEPE,WIF,DOGE,ORCA,JTO,RENDER,ARB,SUI,APT"

    # Биржи (Read-only для проверки займа)
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    binance_api_key: str = ""
    binance_api_secret: str = ""

    # Инфраструктура
    database_url: str = "sqlite+aiosqlite:///./data/arb.db"
    redis_url: str = "redis://redis:6379"
    coinmarketcal_api_key: str = ""

    # PostgreSQL (Docker)
    postgres_user: str = "arb"
    postgres_password: str = "arb_secret_2024"
    postgres_db: str = "arb_db"

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watch_symbols.split(",")]

    @property
    def max_position_usd(self) -> float:
        return self.deposit_usd * self.max_position_pct / 100

    @property
    def is_auto_scan(self) -> bool:
        return self.scan_mode.lower() == "auto"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
