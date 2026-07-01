# data/fees.py
# ПОЛНАЯ БАЗА БИРЖ v8.0 — 25 штук

EXCHANGES: dict[str, dict] = {

    # ── TIER-1: надёжные, must-have ──────────────────────────────────

    "binance": {
        "tier": 1,
        "perp_t":   0.05,    # % taker fee перп
        "spot_t":   0.10,    # % taker fee спот
        "wd_usd":   1.00,    # фиксированная комиссия вывода USDT
        "funding_cap": 0.75, # максимальный |rate| за период
        "lag_hours": 1.0,    # скорость реакции на изменение premium (ч)
        "role":     "both",  # long / short / both
        "ok":       True,
        "margin":   True,    # маржинальный спот доступен
        "borrow_rate_token": 0.050,  # %/час занять токен
        "borrow_rate_usdt":  0.020,  # %/час занять USDT
        "cycle_rules": {
            # rate% -> переход на N-часовой цикл
            0.50: 4,
            1.50: 2,
            2.00: 1,
        },
        "note": "Инертен при rate < 3%, рампа при > 1.5%",
    },

    "bybit": {
        "tier": 1,
        "perp_t":   0.055,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 4.00,
        "lag_hours": 0.5,    # быстрый
        "role":     "short",
        "ok":       True,
        "margin":   True,
        "borrow_rate_token": 0.080,
        "borrow_rate_usdt":  0.040,
        "cycle_rules": {2.0: 4, 3.0: 2, 4.0: 1},
        "note": "Лучшая шорт-нога. Unified Account удобен",
    },

    "okx": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.10,
        "wd_usd":   0.50,    # дешевле вывод!
        "funding_cap": 0.50, # жёсткий лимит — быстро у предела
        "lag_hours": 1.5,
        "role":     "both",
        "ok":       True,
        "margin":   True,
        "borrow_rate_token": 0.060,
        "borrow_rate_usdt":  0.030,
        "cycle_rules": {0.35: 4, 0.45: 2, 0.50: 1},
        "note": "Лимит 0.5% — часто у предела, лимит-арб сигнал",
    },

    "gate": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 3.00,
        "lag_hours": 4.0,    # МЕДЛЕННЫЙ — инертный
        "role":     "long",
        "ok":       True,
        "margin":   True,
        "borrow_rate_token": 0.080,
        "borrow_rate_usdt":  0.030,
        "cycle_rules": {1.0: 4, 2.0: 2, 3.0: 1},
        "note": "Инертный Gate — лучшая лонг-нога. ADL риск при расходе фонда",
    },

    "bitget": {
        "tier": 1,
        "perp_t":   0.06,
        "spot_t":   0.10,
        "wd_usd":   0.80,
        "funding_cap": 1.50,
        "lag_hours": 1.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.2: 2, 1.5: 1},
        "note": "Быстрая шорт-нога",
    },

    "kucoin": {
        "tier": 1,
        "perp_t":   0.06,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 3.00,
        "lag_hours": 3.0,
        "role":     "long",
        "ok":       True,
        "margin":   True,
        "borrow_rate_token": 0.070,
        "borrow_rate_usdt":  0.025,
        "cycle_rules": {1.0: 4, 2.0: 2, 3.0: 1},
        "note": "Переходит через 20мин от отсечки — поздний",
    },

    "bingx": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 1.5,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 1.5: 2, 2.0: 1},
        "note": "Tier-1, хорошая ликвидность",
    },

    "blofin": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.00,    # нет спот секции
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 1.5,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 1.5: 2, 2.0: 1},
        "note": "Только перп, хорошая шорт-нога",
    },

    "coinex": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 1.50,
        "lag_hours": 2.0,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.2: 2, 1.5: 1},
        "note": "РАМПА: снимает раз в 8ч с шорта при расходе. "
                "Переход 8ч→1ч при rate≈1.5% — входить заранее!",
    },

    "hyperliquid": {
        "tier": 1,
        "perp_t":   0.035,   # самый дешёвый taker!
        "spot_t":   0.00,
        "wd_usd":   1.00,
        "funding_cap": 4.00,
        "lag_hours": 0.5,    # DEX — мгновенная реакция
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {4.0: 1},   # всегда 1ч
        "note": "DEX перп, всегда 1ч цикл, самые низкие fees",
    },

    "xt": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 1.5: 2, 2.0: 1},
        "note": "Tier-1, нормальная ликвидность",
    },

    "coinw": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.50,
        "funding_cap": 2.00,
        "lag_hours": 3.0,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Медленная биржа — хорошая лонг-нога",
    },

    "tapbit": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.5,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Медленная, хорошая лонг-нога",
    },

    "bitunix": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Tier-1, шорт-нога",
    },

    "paradex": {
        "tier": 1,
        "perp_t":   0.03,    # дешёвый DEX
        "spot_t":   0.00,
        "wd_usd":   0.50,
        "funding_cap": 4.00,
        "lag_hours": 0.5,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {4.0: 1},
        "note": "DEX StarkNet. Первые мин листинга: спред 5-10%. 1ч цикл.",
    },

    "backpack": {
        "tier": 1,
        "perp_t":   0.04,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 3.00,
        "lag_hours": 1.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.5: 4, 2.5: 1},
        "note": "Solana DEX. 1ч цикл для SOL-токенов",
    },

    "pionex": {
        "tier": 1,
        "perp_t":   0.05,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 1.50,
        "lag_hours": 2.0,
        "role":     "both",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.5: 1},
        "note": "Tier-1, grid-bot биржа",
    },

    # ── TIER-2: есть риски ────────────────────────────────────────────

    "mexc": {
        "tier": 2,
        "perp_t":   0.01,    # 0% maker, 0.01% taker — дешевейший!
        "spot_t":   0.00,    # spot БЕСПЛАТНО
        "wd_usd":   1.00,
        "funding_cap": 1.50,
        "lag_hours": 1.5,
        "role":     "both",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.5: 1},
        "note": "Tier-2 но 0% спот! Лучшая для спот-лонг ноги",
    },

    "weex": {
        "tier": 2,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Tier-2, нормальная ликвидность",
    },

    "phemex": {
        "tier": 2,
        "perp_t":   0.06,
        "spot_t":   0.10,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 2.0,
        "role":     "short",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Tier-2",
    },

    "poloniex": {
        "tier": 2,
        "perp_t":   0.05,
        "spot_t":   0.15,
        "wd_usd":   1.50,
        "funding_cap": 1.50,
        "lag_hours": 3.0,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.5: 1},
        "note": "Tier-2 медленная лонг-нога",
    },

    "deepcoin": {
        "tier": 2,
        "perp_t":   0.05,
        "spot_t":   0.20,
        "wd_usd":   1.00,
        "funding_cap": 2.00,
        "lag_hours": 3.0,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {1.0: 4, 2.0: 1},
        "note": "Tier-2 медленная",
    },

    "bitmart": {
        "tier": 2,
        "perp_t":   0.06,
        "spot_t":   0.25,
        "wd_usd":   1.50,
        "funding_cap": 1.50,
        "lag_hours": 3.5,
        "role":     "long",
        "ok":       True,
        "margin":   False,
        "cycle_rules": {0.8: 4, 1.5: 1},
        "note": "Tier-2, медленная, высокие спот-fees",
    },

    # ── BLACKLIST ─────────────────────────────────────────────────────
    "ourbit": {
        "ok": False,
        "note": "BLACKLIST: тонкий стакан, нельзя выйти",
    },
    "htx": {
        "ok": False,
        "note": "BLACKLIST: берёт лимитки по рынку, непредсказуем",
    },
}

# Совместимость с v7
FEES = EXCHANGES
BLACKLIST: set[str] = {ex for ex, cfg in EXCHANGES.items() if not cfg.get("ok", True)}
