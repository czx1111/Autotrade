"""Tests for the one-click factor screener (pure functions)."""

import unittest

import pandas as pd
import pytest

from tradingagents.ui.screener import build_ai_review_prompt, factor_screen


def _spot() -> pd.DataFrame:
    rows = [
        # 好样本：动量正、流动性好、换手适中、估值合理
        {"code": "600001", "name": "好股票A", "price": 20.0, "pct": 2.0,
         "turnover": 5.0, "amount": 5e8, "pe": 25.0, "pct60d": 12.0},
        {"code": "600002", "name": "好股票B", "price": 50.0, "pct": 1.0,
         "turnover": 3.0, "amount": 2e8, "pe": 30.0, "pct60d": 8.0},
        # 坏样本
        {"code": "600003", "name": "ST差股", "price": 5.0, "pct": 1.0,
         "turnover": 5.0, "amount": 5e8, "pe": 25.0, "pct60d": 10.0},
        {"code": "600004", "name": "亏损股", "price": 10.0, "pct": 1.0,
         "turnover": 5.0, "amount": 5e8, "pe": -5.0, "pct60d": 10.0},
        {"code": "600005", "name": "过热股", "price": 10.0, "pct": 9.5,
         "turnover": 5.0, "amount": 5e8, "pe": 25.0, "pct60d": 70.0},
        {"code": "600006", "name": "缩量股", "price": 10.0, "pct": 1.0,
         "turnover": 5.0, "amount": 1e7, "pe": 25.0, "pct60d": 10.0},
    ]
    return pd.DataFrame(rows)


@pytest.mark.unit
class TestFactorScreen(unittest.TestCase):
    def test_filters_out_bad_rows(self):
        out = factor_screen(_spot(), top_n=10)
        codes = set(out["code"])
        self.assertEqual(codes, {"600001", "600002"})

    def test_score_ranking(self):
        out = factor_screen(_spot(), top_n=10)
        # 好股票A 各因子均更优 → 排第一
        self.assertEqual(out.iloc[0]["code"], "600001")
        self.assertTrue(out["score"].is_monotonic_decreasing)

    def test_top_n_limit(self):
        out = factor_screen(_spot(), top_n=1)
        self.assertEqual(len(out), 1)

    def test_custom_bounds_relax(self):
        # 放宽条件后 ST 之外更多股票入选（亏损股 PE 放开）
        out = factor_screen(_spot(), top_n=10, bounds={"pe": (-1000.0, 1000.0)})
        self.assertIn("600004", set(out["code"]))

    def test_empty_when_no_match(self):
        out = factor_screen(_spot(), top_n=10, bounds={"price": (9999.0, 10000.0)})
        self.assertTrue(out.empty)

    def test_prompt_contains_csv(self):
        out = factor_screen(_spot(), top_n=2)
        prompt = build_ai_review_prompt(out, pick_n=2)
        self.assertIn("600001", prompt)
        self.assertIn("推荐理由", prompt)


if __name__ == "__main__":
    unittest.main()
