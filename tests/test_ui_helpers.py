"""Tests for Web UI helpers: pure data functions and chart builders."""

import unittest
from datetime import datetime

import pandas as pd
import pytest

from tradingagents.ui.charts import build_kline_figure, build_sector_bar
from tradingagents.ui.data import (
    add_ma,
    filter_market,
    fmt_amount,
    kline_range,
    rename_columns,
    search_stocks,
)


def _spot_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"code": "600519", "name": "贵州茅台", "price": 1500.0, "pct": 1.2},
        {"code": "000858", "name": "五粮液", "price": 130.0, "pct": -0.5},
        {"code": "000001", "name": "平安银行", "price": 10.5, "pct": 0.3},
        {"code": "600000", "name": "ST浦发", "price": 8.0, "pct": -2.0},
    ])


@pytest.mark.unit
class TestDataHelpers(unittest.TestCase):
    def test_rename_columns_partial(self):
        df = pd.DataFrame({"代码": ["600519"], "名称": ["贵州茅台"], "不存在": [1]})
        out = rename_columns(df, {"代码": "code", "名称": "name", "缺失": "x"})
        self.assertEqual(list(out.columns), ["code", "name", "不存在"])

    def test_add_ma(self):
        df = pd.DataFrame({"close": [float(i) for i in range(1, 11)]})
        out = add_ma(df, windows=(5,))
        self.assertIn("ma5", out)
        # 前 4 个不足窗口 → NaN，第 5 个 = (1+2+3+4+5)/5
        self.assertTrue(out["ma5"].head(4).isna().all())
        self.assertEqual(out["ma5"].iloc[4], 3.0)

    def test_search_by_code_prefix_and_name(self):
        spot = _spot_df()
        self.assertEqual(len(search_stocks(spot, "6005")), 1)
        self.assertEqual(len(search_stocks(spot, "茅台")), 1)
        self.assertEqual(len(search_stocks(spot, "")), 4)
        self.assertEqual(len(search_stocks(spot, "不存在")), 0)

    def test_filter_market_excludes_st(self):
        out = filter_market(_spot_df(), pct_min=-10, pct_max=10)
        names = set(out["name"])
        self.assertNotIn("ST浦发", names)
        self.assertEqual(len(out), 3)

    def test_filter_market_pct_band(self):
        out = filter_market(_spot_df(), pct_min=0.0, pct_max=10.0, exclude_st=False)
        # ST浦发(-2.0) 和 五粮液(-0.5) 被过滤
        self.assertEqual(sorted(out["name"]), ["平安银行", "贵州茅台"])

    def test_fmt_amount(self):
        self.assertEqual(fmt_amount(1.5e9), "15.00亿")
        self.assertEqual(fmt_amount(2.3e4), "2.3万")
        self.assertEqual(fmt_amount(999.0), "999")
        self.assertEqual(fmt_amount(float("nan")), "-")

    def test_kline_range(self):
        start, end = kline_range(250, today=datetime(2026, 3, 2))
        self.assertEqual(end, "2026-03-02")
        self.assertEqual(start, "2025-06-25")


@pytest.mark.unit
class TestCharts(unittest.TestCase):
    def _kline_df(self) -> pd.DataFrame:
        return pd.DataFrame({
            "date": ["2026-03-01", "2026-03-02"],
            "open": [10.0, 10.5], "high": [11.0, 11.2],
            "low": [9.8, 10.3], "close": [10.5, 11.0],
            "volume": [1_000_000, 1_200_000],
            "ma5": [10.2, 10.4],
        })

    def test_kline_figure_traces(self):
        fig = build_kline_figure(self._kline_df(), title="测试")
        kinds = [t.type for t in fig.data]
        self.assertIn("candlestick", kinds)
        self.assertIn("bar", kinds)      # 成交量
        self.assertIn("scatter", kinds)  # MA5

    def test_kline_figure_without_ma(self):
        fig = build_kline_figure(self._kline_df(), show_ma=False)
        kinds = [t.type for t in fig.data]
        self.assertNotIn("scatter", kinds)

    def test_sector_bar(self):
        df = pd.DataFrame({
            "name": ["白酒", "银行"], "pct": [2.5, -1.0],
        })
        fig = build_sector_bar(df, top_n=2)
        self.assertEqual(len(fig.data), 1)
        self.assertEqual(fig.data[0].type, "bar")

    def test_sector_bar_empty(self):
        self.assertEqual(len(build_sector_bar(pd.DataFrame()).data), 0)


if __name__ == "__main__":
    unittest.main()
