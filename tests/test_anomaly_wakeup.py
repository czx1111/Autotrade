"""Tests for event-driven LLM wakeup: anomaly detection → analysis re-arm."""

from __future__ import annotations

import tempfile
import os
from datetime import datetime

import pytest

from tradingagents.broker import Position
from tradingagents.monitor import PriceMonitor
from tradingagents.strategy import StrategyConfig


def _pos(cost=10.0, qty=1000, available=1000):
    return Position(
        symbol="600519", quantity=qty, available=available,
        avg_cost=cost, last_price=cost, buy_date="2026-08-01",
    )


class _Broker:
    """Minimal broker stub: one position, no real fills."""

    mode = "paper"

    def __init__(self, pos):
        self._pos = pos

    def get_positions(self):
        return {"600519": self._pos}

    def get_account(self):
        from tradingagents.broker import AccountInfo

        return AccountInfo(total_asset=100_000, available_cash=100_000)


class _Executor:
    def execute(self, **kw):
        from tradingagents.execution import ExecutionResult, EXECUTED

        return ExecutionResult(EXECUTED, "stubbed")


def _monitor(pos, quote, *, on_anomaly, strategy=None):
    return PriceMonitor(
        "test-acct",
        broker=_Broker(pos),
        executor=_Executor(),
        strategy=strategy or StrategyConfig(
            stop_loss_pct=0.07, take_profit_pct=0.15, trailing_stop_pct=None,
        ),
        quote_fn=lambda sym: quote,
        kline_fn=lambda sym, days=120: None,
        now_fn=lambda: datetime(2026, 8, 19, 10, 0),
        signal_dir=tempfile.mkdtemp(),
        on_anomaly=on_anomaly,
    )


class TestAnomalyDetection:
    def test_intraday_surge_triggers_callback(self):
        events = []
        # 昨收 10.0 → 现价 10.5 = +5%，超过 4% 阈值
        m = _monitor(_pos(cost=10.0), {"price": 10.5, "prev_close": 10.0, "name": "贵州茅台"},
                     on_anomaly=lambda s, k, d: events.append((s, k)))
        m.check_once()
        assert ("600519", "intraday_surge") in events

    def test_near_limit_takes_priority(self):
        events = []
        # +9.5% → 逼近涨停（而非普通急涨）
        m = _monitor(_pos(cost=10.0), {"price": 10.95, "prev_close": 10.0, "name": "x"},
                     on_anomaly=lambda s, k, d: events.append((s, k)))
        m.check_once()
        assert ("600519", "near_limit") in events
        assert ("600519", "intraday_surge") not in events

    def test_near_stop_loss_triggers(self):
        events = []
        # 成本 10，止损线 9.3；现价 9.35 距止损线 0.54%（<1% 余量）
        m = _monitor(_pos(cost=10.0), {"price": 9.35, "prev_close": 9.9, "name": "x"},
                     on_anomaly=lambda s, k, d: events.append((s, k)))
        m.check_once()
        assert ("600519", "near_stop_loss") in events

    def test_breached_stop_does_not_double_report(self):
        """已跌破止损线的走卖出信号，不再报「逼近止损」。"""
        events = []
        # 成本 10，止损线 9.3；现价 9.0 已跌破 → evaluate_position 触发卖出
        m = _monitor(_pos(cost=10.0, available=0), {"price": 9.0, "prev_close": 9.9, "name": "x"},
                     on_anomaly=lambda s, k, d: events.append((s, k)))
        m.check_once()
        assert ("600519", "near_stop_loss") not in events

    def test_quiet_market_no_events(self):
        events = []
        # 现价 10.1 = +1%，远离止损线
        m = _monitor(_pos(cost=10.0), {"price": 10.1, "prev_close": 10.0, "name": "x"},
                     on_anomaly=lambda s, k, d: events.append((s, k)))
        m.check_once()
        assert events == []

    def test_callback_exception_does_not_break_monitor(self):
        def boom(sym, kind, detail):
            raise RuntimeError("callback crashed")

        # 报价温和但触发逼近止损：callback 抛异常 → check_once 仍正常返回
        m = _monitor(_pos(cost=10.0), {"price": 9.35, "prev_close": 9.9, "name": "x"},
                     on_anomaly=boom)
        records = m.check_once()   # 不应抛异常
        assert isinstance(records, list)


class TestAutoTraderWakeup:
    def _trader(self, now):
        import json

        from tradingagents.auto_trader import AutoTrader

        state = os.path.join(tempfile.gettempdir(), f"wakeup_{os.getpid()}.json")
        if os.path.exists(state):
            os.remove(state)
        return AutoTrader(
            {
                "name": "wakeup-test",
                "broker_settings": {"broker": "paper", "state_path": state},
                "watchlist": [],
            },
            {},
            now_fn=lambda: now,
        )

    def test_anomaly_resets_analyzed_flag(self):
        from datetime import timedelta

        now = datetime(2026, 8, 19, 10, 0)
        trader = self._trader(now)
        trader._analyzed["600519"] = "2026-08-19"   # 已分析过
        trader._on_anomaly("600519", "intraday_surge", "日内急涨 +5%")
        assert "600519" not in trader._analyzed     # 标记被清除 → 可重新分析

    def test_wakeup_cooldown_blocks_rapid_refire(self):
        from datetime import timedelta

        now = datetime(2026, 8, 19, 10, 0)
        trader = self._trader(now)
        trader._on_anomaly("600519", "intraday_surge", "第一次")
        trader._analyzed["600519"] = "2026-08-19"   # 模拟重新分析完成
        # 40 分钟后再次异动：冷却期内，不重复唤醒
        trader._now_fn = lambda: now + timedelta(minutes=40)
        trader._on_anomaly("600519", "intraday_surge", "第二次")
        assert "600519" in trader._analyzed         # 标记未被清除

        # 70 分钟后：冷却结束，允许再次唤醒
        trader._now_fn = lambda: now + timedelta(minutes=70)
        trader._on_anomaly("600519", "intraday_surge", "第三次")
        assert "600519" not in trader._analyzed

    def test_monitor_is_wired_with_callback(self):
        now = datetime(2026, 8, 19, 10, 0)
        trader = self._trader(now)
        monitor = trader.get_monitor()
        # bound method 每次访问生成新对象，用 == 比较（比较 __self__/__func__）
        assert monitor.on_anomaly == trader._on_anomaly
        assert monitor.on_anomaly.__self__ is trader
