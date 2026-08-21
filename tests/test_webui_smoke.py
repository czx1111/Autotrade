"""Headless smoke test for the Streamlit web UI via ``streamlit.testing.AppTest``.

Network-bound loaders (akshare) are mocked so the smoke test asserts the UI
code itself renders without exceptions, independent of internet access.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import tradingagents.ui.data as ui_data

WEBUI = Path(__file__).resolve().parents[1] / "webui.py"


def _kline(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2026-03-01", periods=n, freq="D")
    close = pd.Series([10 + i * 0.1 for i in range(n)], index=idx)
    return pd.DataFrame({
        "date": idx.strftime("%Y-%m-%d"),
        "open": close - 0.05, "close": close,
        "high": close + 0.1, "low": close - 0.1,
        "volume": [1_000_000] * n, "amount": [1e7] * n,
        "pct": [0.1] * n, "turnover": [1.0] * n,
        "ma5": close.rolling(5).mean(), "ma10": close.rolling(10).mean(),
    })


def _spot() -> pd.DataFrame:
    return pd.DataFrame([
        {"code": "600519", "name": "贵州茅台", "price": 1500.0, "pct": 1.2,
         "chg": 17.8, "volume": 2.5e6, "amount": 3.8e9, "turnover": 0.2,
         "pe": 25.0, "pb": 8.0, "mktcap": 1.9e12, "pct60d": 5.0, "pctYtd": 12.0},
        {"code": "000858", "name": "五粮液", "price": 130.0, "pct": -0.5,
         "chg": -0.65, "volume": 4e6, "amount": 5.2e8, "turnover": 1.0,
         "pe": 18.0, "pb": 4.0, "mktcap": 5e11, "pct60d": -3.0, "pctYtd": 2.0},
    ])


def _mocks():
    import tradingagents.ui.pages.health as ui_health
    from tradingagents.ui.health_check import ProbeResult

    probes = [
        ProbeResult(name="腾讯行情", kind="quote", ok=True, latency_ms=120,
                    detail="600519 现价 1300.0"),
        ProbeResult(name="东财全市场快照", kind="quote", ok=False, latency_ms=5000,
                    detail="ConnectionError"),
    ]
    return (
        patch.object(ui_data, "load_market_spot", return_value=_spot()),
        patch.object(ui_data, "load_indices", return_value=_spot()),
        patch.object(ui_data, "load_sector_boards", return_value=_spot()),
        patch.object(ui_data, "load_kline", return_value=_kline()),
        # 健康页探针 mock：冒烟测试不打真实网络
        patch.object(ui_health, "run_all_probes", return_value=probes),
    )


def _run_app(timeout: float = 30.0) -> AppTest:
    at = AppTest.from_file(WEBUI, default_timeout=timeout)
    patches = _mocks()
    for p in patches:
        p.start()
    try:
        at.run()
    finally:
        for p in patches:
            p.stop()
    return at


@pytest.mark.smoke
class TestWebuiSmoke(unittest.TestCase):
    def test_default_page_renders(self):
        at = _run_app()
        self.assertEqual(at.exception, [])
        self.assertNotEqual(at.header, [])

    def test_all_pages_render(self):
        at = _run_app()
        patches = _mocks()
        for p in patches:
            p.start()
        try:
            for nav in ("📊 总览仪表盘", "🤖 智能体工作室", "📈 持仓与交易",
                        "🔍 发现选股", "📐 K线看盘", "🩺 系统健康", "🔔 告警中心",
                        "⚙️ 策略配置", "📋 历史报告"):
                # 按 key 定位侧边栏导航 radio（页面内可能还有其他 radio）
                at.radio(key="nav").set_value(nav).run()
                self.assertEqual(at.exception, [], f"page {nav} raised an exception")
        finally:
            for p in patches:
                p.stop()


if __name__ == "__main__":
    unittest.main()
