"""Tests for the price monitor: signal detection, T+1 skip, auto-sell, logging."""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pytest

from tradingagents.broker import Order, OrderSide, PaperBroker
from tradingagents.execution import OrderExecutor
from tradingagents.monitor import PriceMonitor
from tradingagents.strategy import StrategyConfig


def _tmp(name: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{name}_{os.getpid()}.json"


def _broker(capital: float = 1_000_000.0) -> PaperBroker:
    path = _tmp("mon_paper")
    if path.exists():
        os.remove(path)
    return PaperBroker(initial_capital=capital, state_path=str(path))


def _monitor(broker, strategy=None, signal_dir=None, now=None):
    return PriceMonitor(
        "test", broker=broker,
        executor=OrderExecutor(broker=broker, config={"confirm_before_trade": False}),
        strategy=strategy or StrategyConfig(stop_loss_pct=0.07, take_profit_pct=0.15,
                                            trailing_stop_pct=None),
        signal_dir=signal_dir or str(_tmp("mon_signals")),
        quote_fn=lambda code: {"price": 9.0, "prev_close": 9.5, "name": "测试股份"},
        kline_fn=lambda code, days=120: None,
        now_fn=now or (lambda: datetime(2026, 3, 2, 10, 0, 0)),
    )


@pytest.mark.unit
class TestPriceMonitor(unittest.TestCase):
    def test_stop_loss_auto_sells(self):
        broker = _broker()
        broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                 quantity=1000, price=10.0))
        broker.next_trading_day()   # T+1 可卖

        monitor = _monitor(broker)  # 现价 9.0 < 止损线 9.3
        records = monitor.check_once()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "stop_loss")
        self.assertEqual(records[0]["outcome"], "EXECUTED")
        self.assertNotIn("600519", broker.get_positions())

    def test_t1_same_day_buy_skipped(self):
        broker = _broker()
        broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                 quantity=1000, price=10.0))
        # 不做 next_trading_day：当日买入 available=0

        monitor = _monitor(broker)
        records = monitor.check_once()

        self.assertEqual(records[0]["kind"], "stop_loss")
        self.assertEqual(records[0]["outcome"], "SKIPPED_T1")
        self.assertIn("600519", broker.get_positions())   # 未卖出

    def test_no_signal_when_in_range(self):
        broker = _broker()
        broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                 quantity=1000, price=8.5))
        broker.next_trading_day()

        monitor = PriceMonitor(
            "test", broker=broker,
            executor=OrderExecutor(broker=broker, config={"confirm_before_trade": False}),
            strategy=StrategyConfig(stop_loss_pct=0.07, take_profit_pct=0.15,
                                    trailing_stop_pct=None),
            signal_dir=str(_tmp("mon_signals")),
            quote_fn=lambda code: {"price": 8.8, "prev_close": 8.5, "name": "测试"},
            now_fn=lambda: datetime(2026, 3, 2, 10, 0, 0),
        )
        self.assertEqual(monitor.check_once(), [])

    def test_signal_log_written_and_loaded(self):
        broker = _broker()
        broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                 quantity=1000, price=10.0))
        broker.next_trading_day()

        log_dir = _tmp("mon_log")
        monitor = _monitor(broker, signal_dir=str(log_dir))
        monitor.check_once()

        logfile = log_dir / "test.jsonl"
        self.assertTrue(logfile.exists())
        history = monitor.load_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["kind"], "stop_loss")
        # JSONL 每行可解析
        line = json.loads(logfile.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["symbol"], "600519")

    def test_position_status(self):
        broker = _broker()
        broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                 quantity=1000, price=10.0))
        broker.next_trading_day()

        monitor = _monitor(broker)
        statuses = monitor.position_status()
        self.assertEqual(len(statuses), 1)
        s = statuses[0]
        self.assertEqual(s["symbol"], "600519")
        self.assertAlmostEqual(s["price"], 9.0)
        self.assertAlmostEqual(s["stop_line"], 9.3, places=2)
        self.assertAlmostEqual(s["target_line"], 11.5, places=2)

    def test_quote_failure_skips_position(self):
        broker = _broker()
        broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                 quantity=1000, price=10.0))
        broker.next_trading_day()

        def _bad_quote(code):
            raise ConnectionError("down")

        monitor = PriceMonitor(
            "test", broker=broker,
            executor=OrderExecutor(broker=broker, config={"confirm_before_trade": False}),
            strategy=StrategyConfig(stop_loss_pct=0.07),
            signal_dir=str(_tmp("mon_signals")),
            quote_fn=_bad_quote,
            now_fn=lambda: datetime(2026, 3, 2, 10, 0, 0),
        )
        self.assertEqual(monitor.check_once(), [])   # 行情失败不产生信号也不崩溃


if __name__ == "__main__":
    unittest.main()
