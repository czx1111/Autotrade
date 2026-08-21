"""周报/月报复盘（review.py）测试：聚合指标、报告渲染、月末判定。"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pytest

from tradingagents.review import (
    build_review,
    is_last_trading_day_of_month,
    load_daily_summaries,
    write_review,
)


def _daily(date: str, pnl: float, trades=None, asset=1_000_000.0) -> dict:
    return {
        "account": "acct",
        "date": date,
        "total_asset": asset + pnl,
        "available_cash": asset + pnl,
        "day_pnl": pnl,
        "positions": {},
        "trades_today": len(trades or []),
        "trades_detail": trades or [],
        "pending_approvals": 0,
    }


@pytest.mark.unit
class TestLoadDailySummaries(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="review_"))
        auto = self.dir / "auto"
        auto.mkdir(parents=True)

    def _write(self, date: str, payload: dict) -> None:
        path = self.dir / "auto" / f"acct_{date.replace('-', '')}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_load_filters_by_date_range(self):
        self._write("2026-03-02", _daily("2026-03-02", 100.0))
        self._write("2026-03-03", _daily("2026-03-03", -50.0))
        self._write("2026-03-04", _daily("2026-03-04", 200.0))
        self._write("2026-03-20", _daily("2026-03-20", 500.0))  # 区间外

        out = load_daily_summaries(self.dir, "acct",
                                   start_date="2026-03-01",
                                   end_date="2026-03-05")
        self.assertEqual([d["date"] for d in out],
                         ["2026-03-02", "2026-03-03", "2026-03-04"])

    def test_ignores_review_subfolder_and_other_accounts(self):
        self._write("2026-03-02", _daily("2026-03-02", 100.0))
        (self.dir / "auto" / "other_20260302.json").write_text("{}", encoding="utf-8")
        review_dir = self.dir / "auto" / "review"
        review_dir.mkdir()
        (review_dir / "acct_weekly_20260302.md").write_text("x", encoding="utf-8")

        out = load_daily_summaries(self.dir, "acct")
        self.assertEqual(len(out), 1)


@pytest.mark.unit
class TestBuildReview(unittest.TestCase):
    def test_metrics_aggregation(self):
        dailies = [
            _daily("2026-03-02", 1000.0),
            _daily("2026-03-03", -400.0, trades=[
                {"trade_id": "1", "order_id": "E1", "symbol": "600519",
                 "name": "", "side": "buy", "quantity": 100, "price": 10.0,
                 "traded_at": ""},
            ]),
            _daily("2026-03-04", 0.0, trades=[
                {"trade_id": "2", "order_id": "E2", "symbol": "600519",
                 "name": "", "side": "sell", "quantity": 100, "price": 11.0,
                 "traded_at": ""},
            ]),
        ]
        m = build_review(dailies, "acct", "weekly")

        self.assertEqual(m["trading_days"], 3)
        self.assertEqual(m["win_days"], 1)
        self.assertEqual(m["loss_days"], 1)
        self.assertAlmostEqual(m["period_pnl"], 600.0)
        self.assertAlmostEqual(m["day_win_rate"], 1 / 3)
        # 盈亏比 = 总盈利 / |总亏损| = 1000/400
        self.assertAlmostEqual(m["profit_loss_ratio"], 2.5)
        self.assertEqual(m["buy_count"], 1)
        self.assertEqual(m["sell_count"], 1)
        self.assertAlmostEqual(m["buy_amount"], 1000.0)
        self.assertAlmostEqual(m["sell_amount"], 1100.0)
        self.assertEqual(m["biggest_buy"]["symbol"], "600519")
        # 期初资产 = 首日总资产 - 首日盈亏
        self.assertAlmostEqual(m["start_equity"], 1_000_000.0)

    def test_no_loss_days_profit_ratio_none(self):
        m = build_review([_daily("2026-03-02", 100.0)], "acct", "weekly")
        self.assertIsNone(m["profit_loss_ratio"])

    def test_empty_dailies(self):
        m = build_review([], "acct", "monthly")
        self.assertEqual(m["trading_days"], 0)
        self.assertEqual(m["trade_count"], 0)


@pytest.mark.unit
class TestWriteReview(unittest.TestCase):
    def test_writes_markdown_report(self):
        dir_path = Path(tempfile.mkdtemp(prefix="review_write_"))
        dailies = [_daily("2026-03-02", 500.0), _daily("2026-03-03", -200.0)]
        metrics = write_review(dir_path, "acct", "weekly", dailies)

        path = Path(metrics["report_path"])
        self.assertTrue(path.exists())
        self.assertEqual(path.parent.name, "review")
        content = path.read_text(encoding="utf-8")
        self.assertIn("周报", content)
        self.assertIn("acct", content)
        self.assertIn("日胜率", content)
        self.assertIn("盈亏比", content)

    def test_no_dailies_returns_none(self):
        dir_path = Path(tempfile.mkdtemp(prefix="review_none_"))
        self.assertIsNone(write_review(dir_path, "acct", "weekly", []))


@pytest.mark.unit
class TestLastTradingDayOfMonth(unittest.TestCase):
    def test_mid_month_not_last(self):
        # 2026-03-11 周三，后面还有交易日
        cal = lambda dt: dt.weekday() < 5
        self.assertFalse(is_last_trading_day_of_month(datetime(2026, 3, 11), cal))

    def test_last_weekday_of_month(self):
        # 2026-03-31 周二，且其后无工作日
        cal = lambda dt: dt.weekday() < 5
        self.assertTrue(is_last_trading_day_of_month(datetime(2026, 3, 31), cal))

    def test_friday_before_weekend_month_end(self):
        # 2026-07-31 周五；7-31 是本月最后一天且是交易日
        cal = lambda dt: dt.weekday() < 5
        self.assertTrue(is_last_trading_day_of_month(datetime(2026, 7, 31), cal))

    def test_non_trading_day_never_triggers(self):
        cal = lambda dt: dt.weekday() < 5
        self.assertFalse(is_last_trading_day_of_month(datetime(2026, 3, 28), cal))  # 周六

    def test_holiday_calendar_respected(self):
        # 假设 3-30/3-31 都是节假日，则 3-27（周五）是最后交易日
        def cal(dt):
            return dt.weekday() < 5 and dt.day < 30
        self.assertTrue(is_last_trading_day_of_month(datetime(2026, 3, 27), cal))
        self.assertFalse(is_last_trading_day_of_month(datetime(2026, 3, 30), cal))
