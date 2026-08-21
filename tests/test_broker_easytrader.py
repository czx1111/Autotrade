"""Tests for the easytrader live broker: field mapping and order routing.

easytrader is UI automation over broker clients and is not installed in CI —
``_connect`` is patched with a fake client so only the mapping logic runs.
"""

import unittest
from unittest.mock import patch

import pytest

from tradingagents.broker import Order, OrderSide, OrderType
from tradingagents.broker.easytrader_broker import EasytraderBroker


class _FakeClient:
    """Mimics the easytrader user object (THS / yh field spellings)."""

    def __init__(self):
        self.balance = {
            "总资产": 200000.0,
            "可用金额": 80000.0,
            "证券市值": 120000.0,
            "冻结金额": 0.0,
        }
        self.position = [
            {"证券代码": "600519", "证券名称": "贵州茅台", "持仓数量": 100,
             "可用余额": 100, "成本价": 1500.0, "现价": 1600.0},
            # 残留的零股/无效行应被跳过
            {"证券代码": "000001", "证券名称": "平安银行", "持仓数量": 0,
             "可用余额": 0, "成本价": 10.0, "现价": 10.5},
        ]
        self.orders = []
        self.today_trades = [
            {"证券代码": "600519", "买卖标志": "买入", "成交数量": 100,
             "成交价格": 1600.0, "成交编号": "T1", "合同编号": "E1"},
            {"证券代码": "600519", "买卖标志": "卖出", "成交数量": 50,
             "成交价格": 1610.0, "成交编号": "T2", "合同编号": "E2"},
        ]

    def buy(self, security, price, amount):
        self.orders.append(("buy", security, price, amount))
        return {"entrust_no": "8888"}

    def sell(self, security, price, amount):
        self.orders.append(("sell", security, price, amount))
        return {"entrust_no": "9999"}

    def cancel_entrust(self, entrust_no):
        return {"error": None}


def _broker() -> EasytraderBroker:
    with patch.object(EasytraderBroker, "_connect", return_value=_FakeClient()):
        return EasytraderBroker(client_type="universal", client_path="C:\\ths\\xiadan.exe")


@pytest.mark.unit
class TestEasytraderBroker(unittest.TestCase):
    def test_account_mapping(self):
        acct = _broker().get_account()
        self.assertEqual(acct.total_asset, 200000.0)
        self.assertEqual(acct.available_cash, 80000.0)
        self.assertEqual(acct.market_value, 120000.0)

    def test_positions_skip_zero_rows(self):
        positions = _broker().get_positions()
        self.assertIn("600519", positions)
        self.assertNotIn("000001", positions)
        pos = positions["600519"]
        self.assertEqual(pos.quantity, 100)
        self.assertEqual(pos.available, 100)
        self.assertEqual(pos.last_price, 1600.0)

    def test_buy_order_accepted(self):
        broker = _broker()
        order = Order(symbol="600519", side=OrderSide.BUY, quantity=100, price=1600.0)
        result = broker.place_order(order)
        self.assertEqual(result.status.value, "accepted")
        self.assertEqual(result.order_id, "8888")
        self.assertEqual(broker._client.orders, [("buy", "600519", 1600.0, 100)])

    def test_limit_order_requires_price(self):
        broker = _broker()
        order = Order(symbol="600519", side=OrderSide.BUY, quantity=100)
        result = broker.place_order(order)
        self.assertEqual(result.status.value, "rejected")

    def test_market_order_uses_last_price(self):
        broker = _broker()
        order = Order(symbol="600519", side=OrderSide.BUY, quantity=100,
                      order_type=OrderType.MARKET)
        result = broker.place_order(order, last_price=1605.0)
        self.assertEqual(result.status.value, "accepted")
        self.assertEqual(broker._client.orders, [("buy", "600519", 1605.0, 100)])

    def test_trades_mapping_and_filter(self):
        broker = _broker()
        trades = broker.get_trades("600519")
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].side, OrderSide.BUY)
        self.assertEqual(trades[1].side, OrderSide.SELL)
        self.assertEqual(trades[1].quantity, 50)

    def test_cancel(self):
        result = _broker().cancel_order("8888")
        self.assertEqual(result.status.value, "cancelled")


if __name__ == "__main__":
    unittest.main()
