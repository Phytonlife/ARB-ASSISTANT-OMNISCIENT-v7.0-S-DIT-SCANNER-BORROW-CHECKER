# tests/test_execution.py — ПОЛНАЯ ВЕРСИЯ v7.1
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import asyncio
from hunter.execution import get_order_type, open_hedge


class TestGetOrderType:
    def test_blacklist_ourbit(self):
        """BUG#2 FIXED: Blacklist проверяется ДО глубины стакана."""
        assert get_order_type("ourbit", 50, 99999)["type"] == "BLOCKED"
        assert get_order_type("ourbit", 50, 99999)["safe"] is False

    def test_blacklist_htx(self):
        assert get_order_type("htx", 50, 99999)["type"] == "BLOCKED"

    def test_blacklist_beats_any_depth(self):
        """Даже миллиардный стакан не проходит blacklist."""
        assert get_order_type("ourbit", 50, 1_000_000_000)["type"] == "BLOCKED"

    def test_market_ioc_4x(self):
        assert get_order_type("binance", 50, 200)["type"] == "MARKET_IOC"
        assert get_order_type("binance", 50, 200)["safe"] is True

    def test_market_ioc_exact_3x(self):
        assert get_order_type("binance", 100, 300)["type"] == "MARKET_IOC"

    def test_limit_fok_2x(self):
        assert get_order_type("binance", 50, 100)["type"] == "LIMIT_FOK_5S"

    def test_limit_fok_exact_15x(self):
        assert get_order_type("binance", 100, 150)["type"] == "LIMIT_FOK_5S"

    def test_skip_14x(self):
        assert get_order_type("binance", 50, 70)["type"] == "SKIP"
        assert get_order_type("binance", 50, 70)["safe"] is False

    def test_skip_empty_book(self):
        assert get_order_type("binance", 50, 0)["type"] == "SKIP"

    def test_zero_position_no_crash(self):
        """position_usd=0 не вызывает ZeroDivisionError."""
        r = get_order_type("binance", 0, 100)
        assert r["type"] in ("SKIP", "MARKET_IOC", "LIMIT_FOK_5S", "BLOCKED")

    def test_all_valid_exchanges(self):
        for ex in ["binance","bybit","okx","gate","mexc","bitget","kucoin"]:
            r = get_order_type(ex, 50, 200)
            assert r["type"] in ("MARKET_IOC", "LIMIT_FOK_5S", "SKIP", "BLOCKED")


class TestOpenHedge:
    def test_blacklisted_long_blocked(self):
        signal = {"symbol":"ORCA","ex_long":"ourbit","ex_short":"binance",
                  "size_usd":50,"depth_a":9999,"depth_b":9999}
        r = asyncio.run(open_hedge(signal))
        assert r["success"] is False
        assert "BLACKLIST" in r["reason"]

    def test_blacklisted_short_blocked(self):
        signal = {"symbol":"ORCA","ex_long":"binance","ex_short":"htx",
                  "size_usd":50,"depth_a":9999,"depth_b":9999}
        r = asyncio.run(open_hedge(signal))
        assert r["success"] is False
        assert "BLACKLIST" in r["reason"]

    def test_thin_book_skipped(self):
        signal = {"symbol":"SOL","ex_long":"binance","ex_short":"bybit",
                  "size_usd":50,"depth_a":10,"depth_b":9999}  # 10/50 = 0.2x < 1.5x
        r = asyncio.run(open_hedge(signal))
        assert r["success"] is False

    def test_successful_parallel_hedge(self):
        signal = {"symbol":"SOL","ex_long":"binance","ex_short":"bybit",
                  "size_usd":50,"depth_a":5000,"depth_b":5000}
        r = asyncio.run(open_hedge(signal))
        assert r["success"] is True
        assert "long" in r and "short" in r


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
