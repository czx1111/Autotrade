"""当日状态持久化测试：日亏基线复用、已分析标记跨实例、盯盘查询失败告警。"""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pytest

from tradingagents.auto_trader import AutoTrader, ApprovalStore, Quote
from tradingagents.broker import Order, OrderSide, PaperBroker
from tradingagents.execution import OrderExecutor
from tradingagents.monitor import PriceMonitor
from tradingagents.strategy import Signal, StrategyConfig


def _tmp_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{prefix}_"))


def _paper_broker(capital: float = 1_000_000.0) -> PaperBroker:
    path = os.path.join(tempfile.gettempdir(), f"state_paper_{os.getpid()}.json")
    if os.path.exists(path):
        os.remove(path)
    return PaperBroker(initial_capital=capital, state_path=path)


@pytest.mark.unit
class TestDayStartEquityPersistence(unittest.TestCase):
    def test_same_day_restart_reuses_snapshot(self):
        state = _tmp_dir("daystart")
        broker1 = _paper_broker(1_000_000.0)
        exec1 = OrderExecutor(
            broker=broker1,
            config={"account_name": "acct", "state_dir": str(state)},
        )
        baseline = exec1.day_start_equity
        self.assertAlmostEqual(baseline, 1_000_000.0)

        # 模拟盘中亏损后重启：新 executor 复用当日基线，而不是重置
        broker2 = _paper_broker(900_000.0)
        exec2 = OrderExecutor(
            broker=broker2,
            config={"account_name": "acct", "state_dir": str(state)},
        )
        self.assertAlmostEqual(exec2.day_start_equity, baseline)

    def test_reset_day_overwrites_snapshot(self):
        state = _tmp_dir("daystart_reset")
        broker = _paper_broker(1_000_000.0)
        executor = OrderExecutor(
            broker=broker, config={"account_name": "acct", "state_dir": str(state)},
        )
        executor.reset_day()
        self.assertAlmostEqual(executor.day_start_equity, 1_000_000.0)

    def test_no_state_dir_no_persistence(self):
        executor = OrderExecutor(broker=_paper_broker(1_000_000.0), config={})
        self.assertAlmostEqual(executor.day_start_equity, 1_000_000.0)


@pytest.mark.unit
class TestAnalyzedStatePersistence(unittest.TestCase):
    def _trader(self, state_dir, broker, decision_text="**Rating**: Buy",
                with_state=True):
        store_path = state_dir / "approvals.json"
        store = ApprovalStore("test", path=store_path)
        return AutoTrader(
            {
                "name": "test",
                "broker_settings": {"broker": "paper"},
                "watchlist": ["600519"],
                "screening_enabled": False,
                "large_order_confirm_value": 999_999_999,  # 全走 executor 直发
            },
            broker=broker,
            decision_fn=lambda symbol, day: decision_text,
            quote_fn=lambda symbols: {
                s: Quote(price=10.0, prev_close=10.0, name="测试") for s in symbols
            },
            approval_store=store,
            now_fn=lambda: datetime(2026, 3, 3, 10, 0, 0),  # 周二盘中
            state_dir=state_dir if with_state else None,
        )

    def test_analyzed_symbols_survive_restart(self):
        state = _tmp_dir("analyzed")
        broker = _paper_broker()

        trader1 = self._trader(state, broker)
        cash_before = trader1.broker.get_account().available_cash
        records = trader1.run_intraday(force=True)
        # 首轮：分析并买入
        self.assertTrue(any(r.get("outcome") == "EXECUTED" for r in records))
        cash_after = trader1.broker.get_account().available_cash
        self.assertLess(cash_after, cash_before)

        # 重启（新实例、同一 broker）：已分析标记从盘上恢复，不再重复下单
        trader2 = self._trader(state, broker)
        self.assertEqual(trader2._analyzed.get("600519"), "2026-03-03")
        records2 = trader2.run_intraday(force=True)
        self.assertEqual(records2, [])
        self.assertAlmostEqual(
            trader2.broker.get_account().available_cash, cash_after,
        )

    def test_no_state_dir_uses_memory_only(self):
        state = _tmp_dir("analyzed_mem")
        broker = _paper_broker()
        trader = self._trader(state, broker, with_state=False)  # 不传 state_dir → 内存态
        self.assertEqual(trader._analyzed, {})
        self.assertIsNone(trader._analyzed_path())


@pytest.mark.unit
class TestMonitorQueryFailure(unittest.TestCase):
    def test_act_reports_query_failed(self):
        class _BrokenBroker(PaperBroker):
            def get_positions(self):
                raise RuntimeError("xiadan 会话失效")

            def get_account(self):
                from tradingagents.broker import AccountInfo

                return AccountInfo(total_asset=1_000_000, available_cash=1_000_000)

        broker = _BrokenBroker(state_path=os.path.join(
            tempfile.gettempdir(), f"broken_paper_{os.getpid()}.json"))
        monitor = PriceMonitor(
            "test", broker=broker,
            executor=OrderExecutor(broker=broker, config={"confirm_before_trade": False}),
            strategy=StrategyConfig(stop_loss_pct=0.07, take_profit_pct=0.15,
                                    trailing_stop_pct=None),
            signal_dir=str(_tmp_dir("mon_signals")),
            quote_fn=lambda code: {"price": 9.0, "prev_close": 9.5, "name": "测试"},
            kline_fn=lambda code, days=120: None,
            now_fn=lambda: datetime(2026, 3, 2, 10, 0, 0),
        )
        # check_once 顶层查询失败 → 空记录（check 层面无信号产生）
        self.assertEqual(monitor.check_once(), [])

        # _act 层（信号已触发后）查询失败 → QUERY_FAILED，绝不能记成 T+1
        sig = Signal(kind="stop_loss", symbol="600519", price=9.0,
                     detail="现价跌破止损线")
        record = monitor._act(sig, {"price": 9.0, "prev_close": 9.5, "name": "测试"}, None)
        self.assertEqual(record["outcome"], "QUERY_FAILED")
        self.assertIn("持仓查询失败", record["note"])

    def test_check_once_with_positions_ok(self):
        broker = _paper_broker()
        broker.place_order(Order(symbol="600519", side=OrderSide.BUY,
                                 quantity=1000, price=10.0))
        broker.next_trading_day()
        monitor = PriceMonitor(
            "test", broker=broker,
            executor=OrderExecutor(broker=broker, config={"confirm_before_trade": False}),
            strategy=StrategyConfig(stop_loss_pct=0.07, take_profit_pct=0.15,
                                    trailing_stop_pct=None),
            signal_dir=str(_tmp_dir("mon_signals")),
            quote_fn=lambda code: {"price": 9.0, "prev_close": 9.5, "name": "测试"},
            kline_fn=lambda code, days=120: None,
            now_fn=lambda: datetime(2026, 3, 2, 10, 0, 0),
        )
        records = monitor.check_once()
        self.assertEqual(records[0]["outcome"], "EXECUTED")
