"""Tests for the order execution pipeline (rules + risk + broker)."""

import os
import tempfile
import unittest

import pytest

from tradingagents.broker import PaperBroker
from tradingagents.execution import (
    EXECUTED,
    PENDING,
    REJECTED,
    SKIPPED,
    OrderExecutor,
    size_order,
)
from tradingagents.rules import RiskController


def _fresh_broker(capital: float = 1_000_000.0) -> PaperBroker:
    path = os.path.join(tempfile.gettempdir(), f"exec_test_{os.getpid()}.json")
    if os.path.exists(path):
        os.remove(path)
    return PaperBroker(initial_capital=capital, state_path=path)


@pytest.mark.unit
class TestSizeOrder(unittest.TestCase):
    def test_rounds_down_to_lot(self):
        # 10% of 1M = 100k, at 333/share -> 300 shares (300.3 -> 300)
        self.assertEqual(size_order(1_000_000, 333.0, 0.10), 300)

    def test_below_one_lot_is_zero(self):
        self.assertEqual(size_order(1_000_000, 1500.0, 0.05), 0)

    def test_zero_price(self):
        self.assertEqual(size_order(1_000_000, 0.0, 0.05), 0)


@pytest.mark.unit
class TestExecutorHappyPath(unittest.TestCase):
    def test_buy_executes(self):
        b = _fresh_broker()
        ex = OrderExecutor(b)
        r = ex.execute(
            symbol="600519", action="buy", price=1500.0, quantity=100,
            name="贵州茅台", prev_close=1490.0, last_price=1500.0,
        )
        self.assertEqual(r.decision, EXECUTED, r.reason)
        self.assertEqual(b.get_positions()["600519"].quantity, 100)

    def test_hold_skips(self):
        b = _fresh_broker()
        ex = OrderExecutor(b)
        r = ex.execute(symbol="600519", action="hold", price=1500.0, quantity=100)
        self.assertEqual(r.decision, SKIPPED)

    def test_invalid_symbol_rejected(self):
        b = _fresh_broker()
        ex = OrderExecutor(b)
        r = ex.execute(symbol="NVDA", action="buy", price=100.0, quantity=100)
        self.assertEqual(r.decision, REJECTED)


@pytest.mark.unit
class TestExecutorGuards(unittest.TestCase):
    def test_price_limit_clips_and_executes(self):
        b = _fresh_broker()
        ex = OrderExecutor(b)
        # prev_close 100 -> limit up 110; a 120 order is clipped to 110.
        r = ex.execute(
            symbol="600519", action="buy", price=120.0, quantity=100,
            prev_close=100.0, last_price=120.0,
        )
        self.assertEqual(r.decision, EXECUTED)
        self.assertAlmostEqual(r.order_result.avg_fill_price, 110.0)

    def test_t1_blocks_same_day_sell(self):
        b = _fresh_broker()
        ex = OrderExecutor(b)
        ex.execute(symbol="600519", action="buy", price=1500.0, quantity=100, prev_close=1490.0)
        r = ex.execute(symbol="600519", action="sell", price=1510.0, quantity=100)
        self.assertEqual(r.decision, REJECTED)
        self.assertIn("T+1", r.reason)

    def test_position_limit_blocks_oversized_buy(self):
        b = _fresh_broker()
        ex = OrderExecutor(
            b, risk=RiskController(max_single_position_pct=0.2)
        )
        # 30% of 1M in one shot
        r = ex.execute(
            symbol="600519", action="buy", price=100.0, quantity=3000,
            prev_close=100.0,
        )
        self.assertEqual(r.decision, REJECTED)
        self.assertIn("position", r.reason.lower())

    def test_order_budget_exhausted(self):
        b = _fresh_broker()
        ex = OrderExecutor(b, risk=RiskController(max_orders_per_day=1))
        r1 = ex.execute(symbol="600519", action="buy", price=1500.0, quantity=100, prev_close=1490.0)
        self.assertEqual(r1.decision, EXECUTED)
        b.next_trading_day()
        r2 = ex.execute(symbol="600519", action="sell", price=1510.0, quantity=100)
        self.assertEqual(r2.decision, REJECTED)
        self.assertIn("budget", r2.reason)


class _LiveBroker(PaperBroker):
    """A paper broker masquerading as live to exercise confirmation gating."""

    mode = "qmt"


@pytest.mark.unit
class TestExecutorConfirmation(unittest.TestCase):
    def test_live_requires_confirmation(self):
        b = _fresh_broker()
        live = _LiveBroker(initial_capital=1_000_000, state_path=b.state_path)
        ex = OrderExecutor(live, config={"confirm_before_trade": True})
        r = ex.execute(symbol="600519", action="buy", price=1500.0, quantity=100, prev_close=1490.0)
        self.assertEqual(r.decision, PENDING)
        # nothing executed yet
        self.assertEqual(live.get_positions(), {})

        r2 = ex.execute(
            symbol="600519", action="buy", price=1500.0, quantity=100,
            prev_close=1490.0, confirm=True,
        )
        self.assertEqual(r2.decision, EXECUTED)


if __name__ == "__main__":
    unittest.main()
