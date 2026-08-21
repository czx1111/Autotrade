"""Tests for the paper-trading broker: fees, fills, and T+1 settlement."""

import os
import tempfile
import unittest

import pytest

from tradingagents.broker import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperBroker,
    calc_fees,
)


def _temp_state_path() -> str:
    return os.path.join(tempfile.gettempdir(), f"paper_test_{os.getpid()}.json")


def _fresh_broker(capital: float = 1_000_000.0) -> PaperBroker:
    path = _temp_state_path()
    if os.path.exists(path):
        os.remove(path)
    return PaperBroker(initial_capital=capital, state_path=path)


@pytest.mark.unit
class TestFees(unittest.TestCase):
    def test_commission_floor(self):
        # 5000 CNY turnover -> 1.25 CNY raw commission, floored at 5.
        f = calc_fees("000001", "buy", 10.0, 500)
        self.assertEqual(f.commission, 5.0)

    def test_sell_only_stamp_tax(self):
        buy = calc_fees("600519", "buy", 100.0, 1000)
        sell = calc_fees("600519", "sell", 100.0, 1000)
        self.assertEqual(buy.stamp_tax, 0.0)
        self.assertGreater(sell.stamp_tax, 0.0)
        self.assertAlmostEqual(sell.stamp_tax, 100000 * 0.0005, places=2)

    def test_transfer_fee_sh_only(self):
        sh = calc_fees("600519", "buy", 100.0, 1000)
        sz = calc_fees("000001", "buy", 100.0, 1000)
        self.assertGreater(sh.transfer_fee, 0.0)
        self.assertEqual(sz.transfer_fee, 0.0)

    def test_bad_side(self):
        with self.assertRaises(ValueError):
            calc_fees("600519", "hold", 100.0, 100)


@pytest.mark.unit
class TestPaperBrokerBuy(unittest.TestCase):
    def test_buy_fills_and_deducts_cash(self):
        b = _fresh_broker()
        order = Order(symbol="600519", side=OrderSide.BUY, quantity=100, price=1500.0)
        result = b.place_order(order)
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.filled_quantity, 100)

        pos = b.get_positions()["600519"]
        self.assertEqual(pos.quantity, 100)
        self.assertEqual(pos.available, 0)  # T+1: not sellable today
        # 100 * 1500 + fees(39) = 150039 spent
        self.assertAlmostEqual(b.get_account().available_cash, 1_000_000 - 150039, places=2)

    def test_buy_insufficient_cash_rejected(self):
        b = _fresh_broker(capital=10_000)
        order = Order(symbol="600519", side=OrderSide.BUY, quantity=100, price=1500.0)
        result = b.place_order(order)
        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertIn("insufficient cash", result.message)

    def test_market_order_needs_last_price(self):
        b = _fresh_broker()
        order = Order(
            symbol="600519", side=OrderSide.BUY, quantity=100,
            order_type=OrderType.MARKET,
        )
        result = b.place_order(order)  # no last_price
        self.assertEqual(result.status, OrderStatus.REJECTED)

    def test_average_cost_on_second_buy(self):
        b = _fresh_broker()
        b.place_order(Order(symbol="600519", side=OrderSide.BUY, quantity=100, price=100.0))
        b.place_order(Order(symbol="600519", side=OrderSide.BUY, quantity=100, price=200.0))
        pos = b.get_positions()["600519"]
        self.assertEqual(pos.quantity, 200)
        self.assertAlmostEqual(pos.avg_cost, 150.0, places=2)


@pytest.mark.unit
class TestPaperBrokerSellAndT1(unittest.TestCase):
    def test_same_day_sell_rejected(self):
        b = _fresh_broker()
        b.place_order(Order(symbol="600519", side=OrderSide.BUY, quantity=100, price=100.0))
        sell = Order(symbol="600519", side=OrderSide.SELL, quantity=100, price=110.0)
        result = b.place_order(sell)
        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertIn("T+1", result.message)

    def test_next_day_sell_allowed(self):
        b = _fresh_broker()
        b.place_order(Order(symbol="600519", side=OrderSide.BUY, quantity=100, price=100.0))
        b.next_trading_day()
        sell = Order(symbol="600519", side=OrderSide.SELL, quantity=100, price=110.0)
        result = b.place_order(sell)
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(b.get_positions(), {})  # fully closed

    def test_sell_more_than_available_rejected(self):
        b = _fresh_broker()
        b.place_order(Order(symbol="600519", side=OrderSide.BUY, quantity=200, price=100.0))
        b.next_trading_day()
        sell = Order(symbol="600519", side=OrderSide.SELL, quantity=300, price=110.0)
        result = b.place_order(sell)
        self.assertEqual(result.status, OrderStatus.REJECTED)


@pytest.mark.unit
class TestPaperBrokerPersistence(unittest.TestCase):
    def test_state_survives_restart(self):
        path = _temp_state_path()
        if os.path.exists(path):
            os.remove(path)
        b1 = PaperBroker(initial_capital=1_000_000, state_path=path)
        b1.place_order(Order(symbol="600519", side=OrderSide.BUY, quantity=100, price=100.0))
        b1.close()

        b2 = PaperBroker(initial_capital=1_000_000, state_path=path)
        self.assertIn("600519", b2.get_positions())
        self.assertLess(b2.get_account().available_cash, 1_000_000)
        self.assertEqual(len(b2.get_trades()), 1)


if __name__ == "__main__":
    unittest.main()
