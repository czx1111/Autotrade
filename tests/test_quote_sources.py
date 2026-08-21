"""Tests for multi-source quotes: parsers (pure) and vendor failover."""

import unittest
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.dataflows import quote_sources as qs


# ── 腾讯实时：真实格式样本（字段间以 ~ 分隔，索引见 parse_tencent_quote）──


def _tencent_quote_text(price="1688.00", prev="1680.00") -> str:
    fields = ["1"] * 50
    fields[1] = "贵州茅台"
    fields[2] = "600519"
    fields[3] = price
    fields[4] = prev
    fields[5] = "1675.00"     # open
    fields[33] = "1690.00"    # high
    fields[34] = "1670.00"    # low
    fields[36] = "25000"      # volume 手
    fields[37] = "423000"     # amount 万
    fields[38] = "0.20"       # turnover
    fields[39] = "25.5"       # PE
    return 'v_sh600519="' + "~".join(fields) + '";'


@pytest.mark.unit
class TestParsers(unittest.TestCase):
    def test_exchange_prefix(self):
        self.assertEqual(qs.exchange_prefix("600519"), "sh")
        self.assertEqual(qs.exchange_prefix("000001"), "sz")
        self.assertEqual(qs.exchange_prefix("300750"), "sz")
        self.assertEqual(qs.exchange_prefix("830799"), "bj")
        self.assertIsNone(qs.exchange_prefix("12345"))

    def test_parse_tencent_quote(self):
        quote = qs.parse_tencent_quote(_tencent_quote_text())
        self.assertEqual(quote["name"], "贵州茅台")
        self.assertEqual(quote["price"], 1688.0)
        self.assertEqual(quote["prev_close"], 1680.0)
        self.assertAlmostEqual(quote["pct"], 0.48, places=2)
        self.assertEqual(quote["volume"], 2_500_000)      # 手→股
        self.assertEqual(quote["amount"], 4.23e9)         # 万→元

    def test_parse_tencent_quote_garbage(self):
        self.assertIsNone(qs.parse_tencent_quote("pv_none_match=1"))
        short = 'v_sh600519="1~贵州茅台~600519"'
        self.assertIsNone(qs.parse_tencent_quote(short))

    def test_parse_sina_quote(self):
        text = (
            'var hq_str_sh600519="贵州茅台,1675.00,1680.00,1688.00,1690.00,1670.00,'
            '1687.00,1689.00,2500000,4230000000, BEST, BEST, 100,100,200,200,2026-03-02,15:00:00";'
        )
        quote = qs.parse_sina_quote(text)
        self.assertEqual(quote["name"], "贵州茅台")
        self.assertEqual(quote["price"], 1688.0)
        self.assertEqual(quote["prev_close"], 1680.0)
        self.assertEqual(quote["volume"], 2_500_000)
        self.assertEqual(quote["amount"], 4.23e9)

    def test_parse_tencent_kline(self):
        payload = {"data": {"sh600519": {"qfqday": [
            ["2026-03-01", "10.0", "10.5", "10.8", "9.9", "1000"],
            ["2026-03-02", "10.5", "10.2", "10.6", "10.1", "800"],
        ]}}}
        df = qs.parse_tencent_kline(payload, "600519")
        self.assertEqual(list(df.columns), ["date", "open", "close", "high", "low", "volume"])
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]["volume"], 100_000)   # 手→股

    def test_parse_sina_kline(self):
        text = (
            'var _=[{"day":"2026-03-01","open":"10.0","high":"10.8","low":"9.9","close":"10.5","volume":"100000"},'
            '{"day":"2026-03-02","open":"10.5","high":"10.6","low":"10.1","close":"10.2","volume":"80000"}];'
        )
        df = qs.parse_sina_kline(text)
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[1]["close"], 10.2)

    def test_parse_sina_kline_garbage(self):
        with self.assertRaises(ValueError):
            qs.parse_sina_kline("no json here")


@pytest.mark.unit
class TestFailover(unittest.TestCase):
    def setUp(self):
        qs._quote_cache.clear()
        qs._kline_cache.clear()

    def test_get_quote_first_vendor_wins(self):
        with patch.object(qs, "fetch_tencent_quote", return_value={"price": 10.0, "name": "T"}):
            quote = qs.get_quote("600519", vendors=("tencent", "sina"))
        self.assertEqual(quote["price"], 10.0)

    def test_get_quote_falls_back_to_second(self):
        def _boom(code):
            raise ConnectionError("tencent down")

        with patch.object(qs, "fetch_tencent_quote", side_effect=_boom), \
             patch.object(qs, "fetch_sina_quote", return_value={"price": 11.0, "name": "S"}):
            quote = qs.get_quote("600519", vendors=("tencent", "sina"))
        self.assertEqual(quote["price"], 11.0)

    def test_get_quote_all_fail_returns_none(self):
        def _boom(code):
            raise ConnectionError("down")

        with patch.object(qs, "fetch_tencent_quote", side_effect=_boom), \
             patch.object(qs, "fetch_sina_quote", side_effect=_boom):
            self.assertIsNone(qs.get_quote("600519"))

    def test_get_quote_uses_cache(self):
        calls = {"n": 0}

        def _fake(code):
            calls["n"] += 1
            return {"price": 10.0}

        with patch.object(qs, "fetch_tencent_quote", side_effect=_fake):
            qs.get_quote("600519", vendors=("tencent",))
            qs.get_quote("600519", vendors=("tencent",))
        self.assertEqual(calls["n"], 1)

    def test_get_kline_fallback_to_tencent(self):
        kline = pd.DataFrame({
            "date": ["2026-03-01"], "open": [10.0], "close": [10.5],
            "high": [10.8], "low": [9.9], "volume": [1000],
        })

        def _em_boom(code, days):
            raise ValueError("em down")

        with patch.object(qs, "fetch_em_kline", side_effect=_em_boom), \
             patch.object(qs, "fetch_tencent_kline", return_value=kline):
            df = qs.get_kline("600519", vendors=("em", "tencent"))
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["close"], 10.5)


if __name__ == "__main__":
    unittest.main()
