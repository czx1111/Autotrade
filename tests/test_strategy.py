"""Tests for the exit-strategy engine (stop loss / take profit / trailing / MA cross)."""

import unittest

import pandas as pd
import pytest

from tradingagents.broker import Position
from tradingagents.strategy import Signal, StrategyConfig, evaluate_position


def _pos(cost=10.0, qty=1000, buy_date="2026-01-05") -> Position:
    return Position(symbol="600519", quantity=qty, available=qty,
                    avg_cost=cost, last_price=cost, buy_date=buy_date)


def _kline(closes, highs=None) -> pd.DataFrame:
    n = len(closes)
    dates = pd.date_range("2026-02-01", periods=n, freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates,
        "close": [float(c) for c in closes],
        "high": highs or [float(c) + 0.2 for c in closes],
        "low": [float(c) - 0.2 for c in closes],
    })


@pytest.mark.unit
class TestStrategyConfig(unittest.TestCase):
    def test_from_dict_overrides(self):
        cfg = StrategyConfig.from_dict({"stop_loss_pct": 0.05, "max_hold_days": 20})
        self.assertEqual(cfg.stop_loss_pct, 0.05)
        self.assertEqual(cfg.max_hold_days, 20)
        self.assertEqual(cfg.take_profit_pct, 0.15)   # 未覆盖用默认

    def test_from_dict_ignores_unknown(self):
        cfg = StrategyConfig.from_dict({"unknown_key": 1, None: 2})
        self.assertEqual(cfg.stop_loss_pct, 0.07)

    def test_lines(self):
        cfg = StrategyConfig()
        self.assertAlmostEqual(cfg.stop_line(10.0), 9.3)
        self.assertAlmostEqual(cfg.target_line(10.0), 11.5)


@pytest.mark.unit
class TestEvaluatePosition(unittest.TestCase):
    def test_stop_loss_triggers(self):
        cfg = StrategyConfig(stop_loss_pct=0.07, take_profit_pct=0.15,
                             trailing_stop_pct=None)
        signals = evaluate_position(_pos(cost=10.0), price=9.2, cfg=cfg)
        kinds = [s.kind for s in signals]
        self.assertIn("stop_loss", kinds)
        self.assertNotIn("take_profit", kinds)
        self.assertEqual(signals[0].direction, "sell")

    def test_take_profit_triggers(self):
        cfg = StrategyConfig(stop_loss_pct=0.07, take_profit_pct=0.15,
                             trailing_stop_pct=None)
        signals = evaluate_position(_pos(cost=10.0), price=11.6, cfg=cfg)
        self.assertIn("take_profit", [s.kind for s in signals])

    def test_in_range_no_signal(self):
        cfg = StrategyConfig(trailing_stop_pct=None)
        signals = evaluate_position(_pos(cost=10.0), price=10.5, cfg=cfg)
        self.assertEqual(signals, [])

    def test_trailing_stop_uses_high_watermark(self):
        cfg = StrategyConfig(stop_loss_pct=0.01, take_profit_pct=0.50,
                             trailing_stop_pct=0.08)
        # 成本10，最高冲到12，现回落到 10.9（回撤>8% of 12=11.04 触发）
        kline = _kline([10.5, 11.0, 12.0, 11.5, 10.9],
                       highs=[10.6, 11.2, 12.0, 11.6, 11.0])
        signals = evaluate_position(_pos(cost=10.0), price=10.9, kline=kline, cfg=cfg)
        self.assertIn("trailing_stop", [s.kind for s in signals])

    def test_trailing_stop_not_triggered_when_hold_high(self):
        cfg = StrategyConfig(stop_loss_pct=0.01, take_profit_pct=0.50,
                             trailing_stop_pct=0.08)
        kline = _kline([10.5, 11.0, 11.2, 11.3, 11.4])
        signals = evaluate_position(_pos(cost=10.0), price=11.4, kline=kline, cfg=cfg)
        self.assertEqual([s for s in signals if s.kind == "trailing_stop"], [])

    def test_ma_cross_down_sells(self):
        cfg = StrategyConfig(stop_loss_pct=0.001, take_profit_pct=10.0,
                             trailing_stop_pct=None, ma_cross_exit=True)
        # 前13根稳步上升（MA5>MA10），最后一根急跌 → MA5 下穿 MA10
        closes = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9,
                  11.0, 11.1, 11.2, 8.0]
        signals = evaluate_position(_pos(cost=10.0, qty=100), price=8.0,
                                    kline=_kline(closes), cfg=cfg)
        kinds = [s.kind for s in signals]
        self.assertIn("ma_cross", kinds)

    def test_ma_cross_up_trend_no_signal(self):
        cfg = StrategyConfig(stop_loss_pct=0.001, take_profit_pct=10.0,
                             trailing_stop_pct=None, ma_cross_exit=True)
        closes = [10 + 0.1 * i for i in range(14)]   # 单边上升
        signals = evaluate_position(_pos(cost=10.0, qty=100), price=closes[-1],
                                    kline=_kline(closes), cfg=cfg)
        self.assertEqual([s for s in signals if s.kind == "ma_cross"], [])

    def test_max_hold_days(self):
        cfg = StrategyConfig(stop_loss_pct=0.001, take_profit_pct=10.0,
                             trailing_stop_pct=None, max_hold_days=30)
        signals = evaluate_position(_pos(), price=10.0, hold_days=31, cfg=cfg)
        self.assertIn("max_hold", [s.kind for s in signals])

    def test_zero_quantity_no_signal(self):
        pos = Position(symbol="600519", quantity=0, avg_cost=10.0)
        self.assertEqual(evaluate_position(pos, price=5.0), [])

    def test_missing_cost_falls_back_to_price(self):
        pos = Position(symbol="600519", quantity=100, avg_cost=0.0)
        # cost=price → 无止损/止盈触发，不崩溃
        signals = evaluate_position(pos, price=10.0, cfg=StrategyConfig(trailing_stop_pct=None))
        self.assertEqual(signals, [])

    def test_signal_dataclass(self):
        sig = Signal("stop_loss", "600519", 9.2, "test")
        self.assertEqual(sig.direction, "sell")

    def test_trailing_watermark_includes_buy_day(self):
        """买入当日（含）的高点必须计入水位线（曾用 > 漏掉买入日高点）。"""
        from tradingagents.strategy import _trailing_watermark
        # 买入日 2026-02-03，当天高点 12.0；其后的高点只有 11.6
        kline = _kline([10.5, 11.0, 12.0, 11.5, 10.9],
                       highs=[10.6, 11.2, 12.0, 11.6, 11.0])
        self.assertEqual(_trailing_watermark(kline, "2026-02-03"), 12.0)

    def test_trailing_watermark_buy_day_not_in_kline_uses_last_bar(self):
        """买入日晚于 K 线最后一根（当日建仓、K线未更新）时用最后一根高点，
        绝不回退到全窗口最高价——否则刚建仓就误触发移动止损。"""
        from tradingagents.strategy import _trailing_watermark
        # 全窗口最高 15.0（2026-02-02），最后一根（2026-02-05）高点 11.0
        kline = _kline([15.0, 10.5, 10.8, 11.0, 10.9],
                       highs=[15.0, 10.6, 10.9, 11.0, 11.0])
        self.assertEqual(_trailing_watermark(kline, "2026-02-06"), 11.0)

    def test_no_premature_trailing_stop_on_day0_position(self):
        """回归：当天买入的持仓不应拿建仓前的历史高点算回撤。"""
        cfg = StrategyConfig(stop_loss_pct=0.07, take_profit_pct=0.15,
                             trailing_stop_pct=0.08)
        # 买入日 = K 线最后一日；此前 120 日曾冲高 15.0，现价 10.9（成本 10.0）
        kline = _kline([15.0, 10.5, 10.8, 11.0, 10.9],
                       highs=[15.0, 10.6, 10.9, 11.0, 11.0])
        pos = _pos(cost=10.0, buy_date="2026-02-05")
        signals = evaluate_position(pos, price=10.9, kline=kline, cfg=cfg)
        self.assertEqual([s for s in signals if s.kind == "trailing_stop"], [])


if __name__ == "__main__":
    unittest.main()
