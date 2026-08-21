"""Tests for the auto-trading loop: decision parsing, sizing, approvals, phases."""

import os
import tempfile
import unittest
from datetime import datetime

import pytest

from tradingagents.auto_trader import (
    ApprovalStore,
    AutoTrader,
    PendingOrder,
    Quote,
    parse_rating,
)
from tradingagents.broker import Order, OrderSide, PaperBroker


def _tmp_path(prefix: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"{prefix}_{os.getpid()}.json")


def _paper_broker(capital: float = 1_000_000.0) -> PaperBroker:
    path = _tmp_path("auto_paper")
    if os.path.exists(path):
        os.remove(path)
    return PaperBroker(initial_capital=capital, state_path=path)


class _LiveLikeBroker(PaperBroker):
    """Paper broker that claims to be live, to exercise the approval gate."""

    mode = "easytrader"


def _trader(
    broker=None,
    rating="buy",
    price=10.0,
    large_value=50_000.0,
    watchlist=("600519",),
    initial_capital=1_000_000.0,
):
    broker = broker or _paper_broker(initial_capital)
    store = ApprovalStore("test", path=_tmp_path("auto_approvals") + f".{id(broker)}.json")
    if os.path.exists(store.path):
        os.remove(store.path)
    store = ApprovalStore("test", path=store.path)

    decision_text = f"**Rating**: {rating.capitalize()}\n\n**Executive Summary**: test"
    quote = Quote(price=price, prev_close=price, name="测试股份")

    return AutoTrader(
        {
            "name": "test",
            "broker_settings": {"broker": "paper"},
            "watchlist": list(watchlist),
            "screening_enabled": False,
            "large_order_confirm_value": large_value,
        },
        broker=broker,
        decision_fn=lambda symbol, day: decision_text,
        quote_fn=lambda symbols: {s: quote for s in symbols},
        approval_store=store,
        now_fn=lambda: datetime(2026, 3, 2, 10, 0, 0),  # 周一盘中
    )


@pytest.mark.unit
class TestParseRating(unittest.TestCase):
    def test_bold_rating(self):
        text = "**Rating**: Buy\n\n**Executive Summary**: strong entry"
        self.assertEqual(parse_rating(text), "buy")

    def test_case_insensitive(self):
        self.assertEqual(parse_rating("**Rating**: SELL"), "sell")
        self.assertEqual(parse_rating("**Rating**: overweight"), "overweight")

    def test_missing(self):
        self.assertIsNone(parse_rating("no rating here"))
        self.assertIsNone(parse_rating(""))

    def test_bare_rating_from_propagate(self):
        """propagate() 第二返回值是已解析的裸评级词，必须直接采纳。"""
        self.assertEqual(parse_rating("Buy"), "buy")
        self.assertEqual(parse_rating("Underweight"), "underweight")
        self.assertEqual(parse_rating("  SELL  "), "sell")


@pytest.mark.unit
class TestApprovalStore(unittest.TestCase):
    def test_add_and_status_roundtrip(self):
        path = _tmp_path("approvals_test")
        store = ApprovalStore("acct", path=path)
        store.add(PendingOrder(id="a-1", symbol="600519", action="buy",
                               quantity=100, estimate_value=150000,
                               created_at="2026-03-02 10:00:00"))
        self.assertTrue(store.set_status("a-1", "approved"))
        self.assertFalse(store.set_status("missing", "approved"))

        # 新实例从磁盘恢复
        reopened = ApprovalStore("acct", path=path)
        self.assertEqual(reopened.get("a-1").status, "approved")

    def test_expire_before(self):
        path = _tmp_path("approvals_expire")
        store = ApprovalStore("acct", path=path)
        store.add(PendingOrder(id="old", symbol="600519", action="buy",
                               quantity=100, estimate_value=1,
                               created_at="2026-02-01 10:00:00"))
        store.add(PendingOrder(id="fresh", symbol="000858", action="sell",
                               quantity=100, estimate_value=1,
                               created_at="2026-03-02 10:00:00"))
        store.expire_before("2026-03-02")
        self.assertEqual(store.get("old").status, "expired")
        self.assertEqual(store.get("fresh").status, "pending")


@pytest.mark.unit
class TestAutoTraderPhases(unittest.TestCase):
    def test_buy_rating_places_order(self):
        trader = _trader(rating="buy", price=10.0)
        records = trader.run_intraday(force=True)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["decision"], "buy")
        self.assertEqual(records[0]["outcome"], "EXECUTED")
        # 12% 目标仓位 → 1,000,000 * 0.12 / 10 = 12000 股
        self.assertEqual(records[0]["quantity"], 12000)
        self.assertIn("600519", trader.broker.get_positions())

    def test_hold_rating_skips(self):
        trader = _trader(rating="hold")
        records = trader.run_intraday(force=True)
        self.assertEqual(records[0]["decision"], "hold")
        self.assertNotIn("600519", trader.broker.get_positions())

    def test_sell_rating_clears_position(self):
        trader = _trader(rating="sell", price=10.0)
        trader.broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                        quantity=1000, price=10.0))
        trader.broker.next_trading_day()

        records = trader.run_intraday(force=True)
        self.assertEqual(records[0]["action"], "sell")
        self.assertEqual(records[0]["quantity"], 1000)
        self.assertNotIn("600519", trader.broker.get_positions())

    def test_underweight_halves_position(self):
        trader = _trader(rating="underweight", price=10.0)
        trader.broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                        quantity=1000, price=10.0))
        trader.broker.next_trading_day()

        records = trader.run_intraday(force=True)
        self.assertEqual(records[0]["quantity"], 500)
        self.assertEqual(trader.broker.get_positions()["600519"].quantity, 500)

    def test_analyzed_once_per_day(self):
        trader = _trader(rating="buy", price=10.0)
        trader.run_intraday(force=True)
        count = [0]

        def decision(symbol, day):
            count[0] += 1
            return "**Rating**: Buy"

        trader._decision_fn = decision
        trader.run_intraday(force=True)  # 同日第二轮：不应重复分析
        self.assertEqual(count[0], 0)

    def test_pre_market_expires_old_approvals(self):
        trader = _trader()
        trader.approvals.add(PendingOrder(
            id="x", symbol="600519", action="buy", quantity=100,
            estimate_value=1, created_at="2026-01-01 09:00:00",
        ))
        watchlist = trader.run_pre_market()
        self.assertEqual(watchlist, ["600519"])
        self.assertEqual(trader.approvals.get("x").status, "expired")

    def test_post_market_summary(self):
        trader = _trader(rating="buy", price=10.0)
        trader.run_intraday(force=True)
        summary = trader.run_post_market()
        self.assertEqual(summary["account"], "test")
        self.assertEqual(summary["trades_today"], 1)
        self.assertIn("600519", summary["positions"])


@pytest.mark.unit
class TestLargeOrderApproval(unittest.TestCase):
    def test_live_large_order_waits_for_approval(self):
        path = _tmp_path("live_paper")
        if os.path.exists(path):
            os.remove(path)
        broker = _LiveLikeBroker(initial_capital=1_000_000.0, state_path=path)

        trader = _trader(broker=broker, rating="buy", price=10.0, large_value=50_000.0)
        records = trader.run_intraday(force=True)

        # 120,000 CNY 订单 ≥ 50,000 阈值 → 不发单，进审批队列
        self.assertEqual(records[0]["outcome"], "PENDING_APPROVAL")
        self.assertEqual(trader.broker.get_positions(), {})
        pending = trader.approvals.list("pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].quantity, 12000)

        # 批准后下一轮自动执行
        trader.approvals.set_status(pending[0].id, "approved")
        records2 = trader.run_intraday(force=True)
        self.assertEqual(records2[0]["outcome"], "EXECUTED")
        self.assertEqual(trader.approvals.get(pending[0].id).status, "executed")
        self.assertIn("600519", trader.broker.get_positions())

    def test_paper_large_order_does_not_need_approval(self):
        trader = _trader(rating="buy", price=10.0, large_value=50_000.0)
        records = trader.run_intraday(force=True)
        self.assertEqual(records[0]["outcome"], "EXECUTED")


if __name__ == "__main__":
    unittest.main()
