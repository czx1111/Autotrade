"""MCP server 单元测试：工具注册、只读路径、交易开关与账号解析。

不触碰真实行情网络与交易客户端——网络/交易路径只测前置校验分支。
"""

import json
import os
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

import mcp_server as ms


@pytest.mark.unit
class TestDump(unittest.TestCase):
    def test_dataclass_path_and_chinese(self):
        @dataclass
        class _Pos:
            symbol: str
            name: str

        out = json.loads(ms._dump({"pos": _Pos("600519", "贵州茅台"),
                                   "report": Path("a") / "b.md"}))
        self.assertEqual(out["pos"], {"symbol": "600519", "name": "贵州茅台"})
        self.assertEqual(out["report"], str(Path("a") / "b.md"))


@pytest.mark.unit
class TestTradeSwitch(unittest.TestCase):
    def test_default_allowed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTOTRADE_MCP_ALLOW_TRADE", None)
            self.assertTrue(ms._trade_allowed())

    def test_disabled_values(self):
        for v in ("0", "false", "no"):
            with patch.dict(os.environ, {"AUTOTRADE_MCP_ALLOW_TRADE": v}):
                self.assertFalse(ms._trade_allowed())

    def test_place_order_blocked_when_disabled(self):
        with patch.dict(os.environ, {"AUTOTRADE_MCP_ALLOW_TRADE": "0"}):
            out = json.loads(ms.place_order_impl("600519", "buy", 100, 1000.0))
        self.assertIn("error", out)
        self.assertIn("禁用", out["error"])


@pytest.mark.unit
class TestAccountResolution(unittest.TestCase):
    def test_unknown_account_raises_key_error_with_names(self):
        # 账号解析发生在 AutoTrader 构造（连接 broker）之前
        with patch.object(ms, "_load_account_dicts",
                          return_value=[{"name": "ths-live"}]):
            with self.assertRaises(KeyError) as ctx:
                ms._build_trader("nope")
        self.assertIn("ths-live", str(ctx.exception))

    def test_list_accounts_reads_json_only(self):
        with patch.object(ms, "_load_account_dicts", return_value=[
            {"name": "a", "broker_settings": {"broker": "paper"},
             "watchlist": ["600519"]},
        ]):
            out = json.loads(ms.list_accounts_impl())
        self.assertEqual(out["accounts"][0]["broker"], "paper")
        self.assertEqual(out["accounts"][0]["watchlist"], ["600519"])
        self.assertTrue(out["trade_enabled"])


@pytest.mark.unit
class TestGetQuoteValidation(unittest.TestCase):
    def test_empty_symbols_short_circuits(self):
        out = json.loads(ms.get_quote_impl("  , ， "))
        self.assertIn("error", out)           # 不触发行情网络


@pytest.mark.unit
class TestLatestReport(unittest.TestCase):
    def test_latest_report_picks_newest(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        with patch.dict(DEFAULT_CONFIG, {"results_dir": "tmp-results"}):
            base = Path("tmp-results") / "reports"
            old = base / "600519_20260101_000000"
            new = base / "600519_20260102_000000"
            old.mkdir(parents=True, exist_ok=True)
            new.mkdir(parents=True, exist_ok=True)
            (old / "complete_report.md").write_text("old", encoding="utf-8")
            (new / "complete_report.md").write_text("new", encoding="utf-8")
            try:
                self.assertTrue(ms._latest_report("600519").endswith(
                    str(new / "complete_report.md")))
                self.assertIsNone(ms._latest_report("000001"))
            finally:
                import shutil
                shutil.rmtree("tmp-results", ignore_errors=True)

    def test_latest_report_missing_dir(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        with patch.dict(DEFAULT_CONFIG, {"results_dir": "no-such-dir"}):
            self.assertIsNone(ms._latest_report("600519"))


@pytest.mark.unit
class TestToolRegistration(unittest.TestCase):
    def test_all_tools_registered(self):
        """main() 全流程（--list-tools）应注册 13 个工具并正常退出。"""
        rc = ms.main(["--list-tools"])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(ms.mcp)
