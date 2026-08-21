"""Tests for A-share symbol utils, venue rules, and risk controls."""

import unittest
from datetime import datetime

import pytest

from tradingagents.dataflows.ashare_symbol_utils import (
    normalize_ashare_symbol,
    parse_ashare_symbol,
    price_limit_for,
    to_vendor_symbol,
)
from tradingagents.rules import (
    AShareTradingRules,
    RiskController,
    is_trading_time,
    trading_phase,
)


@pytest.mark.unit
class TestParseAshareSymbol(unittest.TestCase):
    def test_bare_main_board_sh(self):
        parsed = parse_ashare_symbol("600519")
        self.assertEqual(parsed["code"], "600519")
        self.assertEqual(parsed["exchange"], "SH")
        self.assertEqual(parsed["board"], "main")
        self.assertEqual(parsed["limit_pct"], 10.0)
        self.assertEqual(parsed["suffixed"], "600519.SH")

    def test_suffixed_shenzhen(self):
        parsed = parse_ashare_symbol("000001.SZ")
        self.assertEqual(parsed["exchange"], "SZ")
        self.assertEqual(parsed["board"], "main")

    def test_star_market_20pct(self):
        parsed = parse_ashare_symbol("688001")
        self.assertEqual(parsed["board"], "star")
        self.assertEqual(parsed["limit_pct"], 20.0)

    def test_chinext_300(self):
        parsed = parse_ashare_symbol("300750")
        self.assertEqual(parsed["board"], "chinext")
        self.assertEqual(parsed["limit_pct"], 20.0)

    def test_bse_30pct(self):
        parsed = parse_ashare_symbol("830799")
        self.assertEqual(parsed["board"], "bse")
        self.assertEqual(parsed["limit_pct"], 30.0)

    def test_sh600519_prefix_style(self):
        parsed = parse_ashare_symbol("sh600519")
        self.assertEqual(parsed["code"], "600519")
        self.assertEqual(parsed["exchange"], "SH")

    def test_non_ashare_returns_none(self):
        self.assertIsNone(parse_ashare_symbol("AAPL"))
        self.assertIsNone(parse_ashare_symbol("12345"))

    def test_normalize(self):
        self.assertEqual(normalize_ashare_symbol("600519.SH"), "600519")
        self.assertIsNone(normalize_ashare_symbol("NVDA"))

    def test_vendor_symbols(self):
        self.assertEqual(to_vendor_symbol("600519", "qmt"), "600519.SH")
        self.assertEqual(to_vendor_symbol("600519", "akshare"), "600519")


@pytest.mark.unit
class TestPriceLimitFor(unittest.TestCase):
    def test_main_board_10pct(self):
        self.assertEqual(price_limit_for("600519", 100.0), (90.0, 110.0))

    def test_st_main_board_5pct(self):
        self.assertEqual(price_limit_for("600519", 100.0, is_st=True), (95.0, 105.0))

    def test_star_board_not_narrowed_for_st(self):
        self.assertEqual(price_limit_for("688001", 100.0, is_st=True), (80.0, 120.0))

    def test_rounds_to_cent(self):
        self.assertEqual(price_limit_for("600000", 10.03), (9.03, 11.03))


@pytest.mark.unit
class TestTradingPhases(unittest.TestCase):
    def test_phases(self):
        cases = [
            ((9, 0), "pre_market"),
            ((9, 20), "open_call"),
            ((10, 0), "morning"),
            ((12, 0), "lunch_break"),
            ((13, 30), "afternoon"),
            ((14, 58), "close_call"),
            ((16, 0), "closed"),
        ]
        for (h, m), expected in cases:
            self.assertEqual(trading_phase(datetime(2026, 8, 17, h, m)), expected)

    def test_is_trading_time(self):
        self.assertTrue(is_trading_time(datetime(2026, 8, 17, 10, 0)))
        self.assertFalse(is_trading_time(datetime(2026, 8, 17, 12, 0)))


@pytest.mark.unit
class TestAShareTradingRules(unittest.TestCase):
    def setUp(self):
        self.rules = AShareTradingRules()

    def test_buy_lot_rounding(self):
        v = self.rules.validate_order("600519", "buy", 1500.0, 250)
        self.assertTrue(v.ok)
        self.assertEqual(v.adjusted_quantity, 200)  # 250 -> 200 (multiple of 100)

    def test_buy_below_min_lot_rejected(self):
        v = self.rules.validate_order("600519", "buy", 1500.0, 50)
        self.assertFalse(v.ok)
        self.assertIn("minimum lot", v.reason)

    def test_star_market_lot_200(self):
        v = self.rules.validate_order("688001", "buy", 50.0, 100)
        self.assertFalse(v.ok)
        self.assertIn("minimum lot", v.reason)

    def test_price_clipped_to_limit_up(self):
        # prev_close 100 -> limit up 110
        v = self.rules.validate_order("600519", "buy", 120.0, 100, prev_close=100.0)
        self.assertEqual(v.adjusted_price, 110.0)

    def test_st_blacklist(self):
        v = self.rules.validate_order("600519", "buy", 100.0, 100, name="*ST茅台")
        self.assertFalse(v.ok)
        self.assertIn("ST", v.reason)

    def test_st_blacklist_disabled(self):
        rules = AShareTradingRules(st_blacklist=False)
        v = rules.validate_order("600519", "buy", 100.0, 100, name="*ST茅台")
        self.assertTrue(v.ok)

    def test_t1_check(self):
        positions = {"600519": {"quantity": 300, "available": 100}}
        ok = self.rules.check_t1("600519", 100, positions)
        self.assertTrue(ok.ok)
        bad = self.rules.check_t1("600519", 200, positions)
        self.assertFalse(bad.ok)
        self.assertIn("T+1", bad.reason)

    def test_invalid_symbol(self):
        v = self.rules.validate_order("NVDA", "buy", 100.0, 100)
        self.assertFalse(v.ok)


@pytest.mark.unit
class TestRiskController(unittest.TestCase):
    def test_position_limit(self):
        rc = RiskController(max_single_position_pct=0.2)
        d = rc.check_position_limit("600519", 250000, 1_000_000)
        self.assertFalse(d.ok)  # 250k > 20% of 1M
        ok = rc.check_position_limit("600519", 150000, 1_000_000)
        self.assertTrue(ok.ok)

    def test_daily_loss_limit(self):
        rc = RiskController(max_daily_loss_pct=0.03)
        ok = rc.check_daily_loss_limit(1_000_000, 990_000)  # 1% loss
        self.assertTrue(ok.ok)
        bad = rc.check_daily_loss_limit(1_000_000, 950_000)  # 5% loss
        self.assertFalse(bad.ok)

    def test_order_budget(self):
        rc = RiskController(max_orders_per_day=2)
        self.assertTrue(rc.check_order_budget().ok)
        rc.record_order()
        rc.record_order()
        self.assertFalse(rc.check_order_budget().ok)

    def test_cash_reserve(self):
        rc = RiskController()
        bad = rc.check_cash_reserve(96000, 100000, reserve_pct=0.05)
        self.assertFalse(bad.ok)
        ok = rc.check_cash_reserve(90000, 100000, reserve_pct=0.05)
        self.assertTrue(ok.ok)

    def test_concentration(self):
        rc = RiskController(max_sector_concentration_pct=0.4)
        bad = rc.check_concentration("白酒", 300000, {"白酒": 200000}, 1_000_000)
        self.assertFalse(bad.ok)
        ok = rc.check_concentration("白酒", 100000, {"白酒": 200000}, 1_000_000)
        self.assertTrue(ok.ok)


if __name__ == "__main__":
    unittest.main()
