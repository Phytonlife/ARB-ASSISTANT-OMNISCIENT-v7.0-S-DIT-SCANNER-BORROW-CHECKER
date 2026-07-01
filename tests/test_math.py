# tests/test_math.py — ПОЛНАЯ ВЕРСИЯ v7.1 (все баги покрыты)
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from hunter.math_engine import (
    calc_net_spread, calc_ofi, estimate_slippage, score_signal, check_ticker_trap
)


class TestCalcNetSpread:
    def test_perp_perp_basic(self):
        r = calc_net_spread(0.520, "binance", "bybit", 50, "perp", "perp", False)
        assert "error" not in r
        assert r["wd_pct"] == 0.0
        assert r["fee_a"] == 0.05
        assert r["fee_b"] == 0.055
        assert r["net"] == pytest.approx(0.415, abs=0.001)
        assert r["ok"] is True

    def test_spot_perp_50_withdraw_2pct(self):
        """BUG#1 FIXED: $1 withdraw на $50 позиции = 2.0%."""
        r = calc_net_spread(2.5, "binance", "bybit", 50, "spot", "perp", True)
        assert r["wd_pct"] == pytest.approx(2.0, abs=0.001)

    def test_spot_perp_100_withdraw_1pct(self):
        r = calc_net_spread(2.5, "binance", "bybit", 100, "spot", "perp", True)
        assert r["wd_pct"] == pytest.approx(1.0, abs=0.001)

    def test_spot_perp_200_withdraw_05pct(self):
        r = calc_net_spread(2.5, "binance", "bybit", 200, "spot", "perp", True)
        assert r["wd_pct"] == pytest.approx(0.5, abs=0.001)

    def test_okx_wd_half_dollar(self):
        """OKX: withdraw $0.5 → wd_pct = 1.0% на $50."""
        r = calc_net_spread(2.0, "okx", "bybit", 50, "spot", "perp", True)
        assert r["wd_pct"] == pytest.approx(1.0, abs=0.001)

    def test_bitget_wd_08(self):
        """Bitget: withdraw $0.8 → wd_pct = 1.6% на $50."""
        r = calc_net_spread(2.0, "bitget", "bybit", 50, "spot", "perp", True)
        assert r["wd_pct"] == pytest.approx(1.6, abs=0.001)

    def test_blacklist_ourbit_first(self):
        """BUG#2 FIXED: Blacklist проверяется ДО любых вычислений."""
        r = calc_net_spread(99.0, "ourbit", "binance", 1)
        assert "error" in r
        assert "BLACKLIST" in r["error"]

    def test_blacklist_htx(self):
        r = calc_net_spread(1.0, "binance", "htx", 50)
        assert "error" in r

    def test_unknown_exchange(self):
        r = calc_net_spread(0.5, "unknownex", "binance", 50)
        assert "error" in r

    def test_mexc_spot_zero_fee(self):
        """MEXC spot_t = 0% — лучшая spot биржа."""
        r = calc_net_spread(0.5, "mexc", "binance", 50, "spot", "perp", True)
        assert r["fee_a"] == 0.0

    def test_all_exchange_pairs_valid(self):
        """42 пары всех поддерживаемых бирж не вызывают ошибок."""
        exchanges = ["binance","bybit","okx","gate","mexc","bitget","kucoin"]
        for ea in exchanges:
            for eb in exchanges:
                if ea != eb:
                    r = calc_net_spread(0.5, ea, eb, 50)
                    assert "error" not in r, f"{ea}+{eb} error: {r}"

    def test_ok_flag_correct(self):
        r = calc_net_spread(0.20, "binance", "bybit", 50, "perp", "perp", False)
        assert r["ok"] == (r["net"] > 0.10)

    def test_good_flag_correct(self):
        r = calc_net_spread(0.50, "binance", "bybit", 50, "perp", "perp", False)
        assert r["good"] == (r["net"] > 0.30)

    def test_zero_gross_negative_net(self):
        r = calc_net_spread(0.0, "binance", "bybit", 50, "perp", "perp", False)
        assert r["net"] < 0

    def test_index_arb_threshold(self):
        """Index arb: diff=0.175% → net=0.07% → ok=False (< 0.10%)."""
        r = calc_net_spread(0.175, "binance", "bybit", 50, "perp", "perp", False)
        assert r["ok"] is False
        assert r["net"] == pytest.approx(0.07, abs=0.001)

    def test_perp_perp_no_withdraw(self):
        """Перп-перп НИКОГДА не имеет withdraw."""
        r = calc_net_spread(0.5, "binance", "bybit", 50, "perp", "perp", False)
        assert r["wd_pct"] == 0.0


class TestTickerTrap:
    def test_normal_diff_no_trap(self):
        assert check_ticker_trap(0.5)["trap"] is False

    def test_large_diff_no_trap_below_40(self):
        assert check_ticker_trap(39.9)["trap"] is False

    def test_exactly_40_no_trap(self):
        assert check_ticker_trap(40.0)["trap"] is False

    def test_above_40_is_trap(self):
        r = check_ticker_trap(40.1)
        assert r["trap"] is True
        assert "ТИКЕР-ЛОВУШКА" in r["warning"]

    def test_extreme_diff_is_trap(self):
        assert check_ticker_trap(100.0)["trap"] is True


class TestCalcOFI:
    def test_accumulation(self):
        bids = [[1.0,100],[0.99,50],[0.98,30],[0.97,20],[0.96,10]]
        asks = [[1.01,10],[1.02,5],[1.03,3],[1.04,2],[1.05,1]]
        assert calc_ofi(bids, asks) > 0.8

    def test_selling(self):
        bids = [[1.0,10],[0.99,5]]
        asks = [[1.01,100],[1.02,50],[1.03,30],[1.04,20],[1.05,10]]
        assert calc_ofi(bids, asks) < -0.8

    def test_neutral(self):
        bids = [[1.0,50],[0.99,30],[0.98,20]]
        asks = [[1.01,50],[1.02,30],[1.03,20]]
        assert abs(calc_ofi(bids, asks)) < 0.05

    def test_empty(self):
        assert calc_ofi([], []) == 0.0

    def test_single_bid_only(self):
        assert calc_ofi([[1.0, 100]], [[1.01, 0]]) == 1.0


class TestEstimateSlippage:
    def test_deep_book_low_slippage(self):
        assert estimate_slippage(50, 10000) < 0.01

    def test_thin_book_high_slippage(self):
        assert estimate_slippage(50, 60) > 0.3

    def test_zero_depth(self):
        assert estimate_slippage(50, 0) == 99.0

    def test_formula(self):
        assert estimate_slippage(100, 1000) == pytest.approx(0.05, abs=0.001)

    def test_medium_book(self):
        slip = estimate_slippage(50, 500)
        assert 0.01 < slip < 0.1


class TestScoreSignal:
    def test_perfect(self):
        assert score_signal(0.6, 0.4, 0.5, 0.1, 0.1) >= 8

    def test_weak(self):
        assert score_signal(0.1, 0.05, -0.1, 0.5, 0.6) <= 3

    def test_max_10(self):
        assert score_signal(1.0, 1.0, 1.0, 0.0, 0.0) == 10

    def test_does_not_exceed_10(self):
        assert score_signal(999, 999, 999, 0, 0) == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
