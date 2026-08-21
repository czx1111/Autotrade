"""挂单看护（OpenOrderTracker）测试：登记、成交确认、超时撤单、持久化。"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tradingagents.broker import OrderResult, OrderStatus, TradeRecord
from tradingagents.open_orders import OpenOrderTracker


def _tmp_json(prefix: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{prefix}_{os.getpid()}.json"


class _FakeBroker:
    """受受理即挂起的假通道：get_trades/cancel_order 行为可注入。"""

    mode = "fake"

    def __init__(self, trades=None, cancel_ok=True):
        self._trades = trades or []
        self._cancel_ok = cancel_ok
        self.cancelled: list[str] = []

    def get_trades(self, symbol=None):
        return self._trades

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        status = OrderStatus.CANCELLED if self._cancel_ok else OrderStatus.REJECTED
        return OrderResult(order_id=order_id, status=status,
                           message="" if self._cancel_ok else "撤单失败")


def _tracker(broker, path, timeout_min=15.0, now=None):
    return OpenOrderTracker(
        "test", broker, path=path, timeout_min=timeout_min,
        now_fn=now or (lambda: datetime(2026, 3, 2, 10, 0, 0)),
    )


def _fill(order_id="E1", symbol="600519", side="sell"):
    return TradeRecord(trade_id="T1", order_id=order_id, symbol=symbol,
                       side=side, quantity=100, price=10.0)


@pytest.mark.unit
class TestOpenOrderTracker(unittest.TestCase):
    def setUp(self):
        self.path = _tmp_json("open_orders")
        if self.path.exists():
            os.remove(self.path)

    def test_track_is_idempotent(self):
        broker = _FakeBroker()
        tracker = _tracker(broker, self.path)
        tracker.track("E1", "600519", "buy", 100, 10.0)
        tracker.track("E1", "600519", "buy", 100, 10.0)
        self.assertEqual(len(tracker.pending()), 1)

    def test_track_without_order_id_ignored(self):
        tracker = _tracker(_FakeBroker(), self.path)
        tracker.track("", "600519", "buy", 100, 10.0)
        self.assertEqual(tracker.pending(), [])

    def test_filled_order_removed(self):
        broker = _FakeBroker(trades=[_fill("E1")])
        tracker = _tracker(broker, self.path)
        tracker.track("E1", "600519", "sell", 100, 10.0)
        events = tracker.reconcile()
        self.assertEqual([e["kind"] for e in events], ["filled"])
        self.assertEqual(tracker.pending(), [])

    def test_timeout_cancels_order(self):
        broker = _FakeBroker()
        placed = datetime(2026, 3, 2, 9, 30, 0)
        tracker = _tracker(broker, self.path, timeout_min=15.0)  # now = 10:00
        tracker.track("E1", "600519", "buy", 100, 10.0, tag="auto:buy")
        # 手动把 placed_at 回拨到 9:30（track 用 now_fn 已是 10:00，直接改文件更直观）
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data[0]["placed_at"] = placed.strftime("%Y-%m-%d %H:%M:%S")
        self.path.write_text(json.dumps(data), encoding="utf-8")

        events = tracker.reconcile()
        self.assertEqual([e["kind"] for e in events], ["cancelled"])
        self.assertEqual(broker.cancelled, ["E1"])
        self.assertEqual(tracker.pending(), [])

    def test_cancel_failure_keeps_tracking(self):
        broker = _FakeBroker(cancel_ok=False)
        tracker = _tracker(broker, self.path, timeout_min=15.0)
        tracker.track("E1", "600519", "buy", 100, 10.0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data[0]["placed_at"] = "2026-03-02 09:30:00"
        self.path.write_text(json.dumps(data), encoding="utf-8")

        events = tracker.reconcile()
        self.assertEqual(events[0]["kind"], "cancel_failed")
        self.assertIn("error", events[0])
        self.assertEqual(len(tracker.pending()), 1)   # 继续看护，下轮重试

    def test_fresh_order_kept_tracking(self):
        broker = _FakeBroker()
        tracker = _tracker(broker, self.path, timeout_min=15.0)
        tracker.track("E1", "600519", "buy", 100, 10.0)   # placed_at = 10:00（现在）
        events = tracker.reconcile()
        self.assertEqual(events, [])
        self.assertEqual(len(tracker.pending()), 1)

    def test_stale_order_from_previous_day_expired(self):
        tracker = _tracker(_FakeBroker(), self.path)
        tracker.track("E1", "600519", "buy", 100, 10.0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data[0]["placed_at"] = "2026-02-27 14:00:00"    # 上一交易日
        self.path.write_text(json.dumps(data), encoding="utf-8")

        events = tracker.reconcile()
        self.assertEqual([e["kind"] for e in events], ["expired"])
        self.assertEqual(tracker.pending(), [])

    def test_get_trades_failure_leaves_state_untouched(self):
        class _BrokenBroker(_FakeBroker):
            def get_trades(self, symbol=None):
                raise RuntimeError("xiadan 会话失效")

        tracker = _tracker(_BrokenBroker(), self.path)
        tracker.track("E1", "600519", "buy", 100, 10.0)
        self.assertEqual(tracker.reconcile(), [])
        self.assertEqual(len(tracker.pending()), 1)     # 下轮再对

    def test_state_persists_across_instances(self):
        tracker = _tracker(_FakeBroker(), self.path)
        tracker.track("E1", "600519", "buy", 100, 10.0)
        tracker2 = _tracker(_FakeBroker(), self.path)
        self.assertEqual(len(tracker2.pending()), 1)


@pytest.mark.unit
class TestExecutorHook(unittest.TestCase):
    def test_accepted_order_fires_callback(self):
        from tradingagents.broker import Order, OrderSide, PaperBroker
        from tradingagents.execution import OrderExecutor

        class _AcceptBroker(PaperBroker):
            """受理但不成交：模拟 easytrader 挂单。"""

            def place_order(self, order, last_price=None):
                from tradingagents.broker import OrderResult, OrderStatus

                return OrderResult(order_id="X1", status=OrderStatus.ACCEPTED,
                                   submitted_order=order)

        broker = _AcceptBroker(state_path=str(_tmp_json("accept_paper")))
        executor = OrderExecutor(broker=broker, config={"confirm_before_trade": False})
        seen = []
        executor.on_order_submitted = lambda order, result: seen.append(
            (order.symbol, order.side, result.order_id),
        )
        result = executor.execute(
            symbol="600519", action="buy", price=10.0, quantity=100,
            confirm=True,
        )
        self.assertEqual(result.decision, "EXECUTED")
        self.assertEqual(seen, [("600519", OrderSide.BUY, "X1")])

        # 回调抛异常不影响下单结果
        executor.on_order_submitted = lambda order, res: 1 / 0
        result = executor.execute(
            symbol="600519", action="buy", price=10.0, quantity=100,
            confirm=True,
        )
        self.assertEqual(result.decision, "EXECUTED")
