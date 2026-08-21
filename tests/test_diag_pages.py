"""逐页渲染 Streamlit Web UI，诊断各页面是否正常。

标记为 integration：依赖 streamlit 测试框架，CI 中默认跳过。
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tradingagents.ui.data as ui_data

WEBUI = Path(__file__).resolve().parents[1] / "webui.py"

_NAV_PAGES = (
    "📊 总览仪表盘",
    "🤖 智能体工作室",
    "📈 持仓与交易",
    "🔍 发现选股",
    "📐 K线看盘",
    "⚙️ 策略配置",
    "📋 历史报告",
)


def _kline(n=30):
    idx = pd.date_range("2026-03-01", periods=n, freq="D")
    close = pd.Series([10 + i * 0.1 for i in range(n)], index=idx)
    return pd.DataFrame({
        "date": idx.strftime("%Y-%m-%d"), "open": close - 0.05, "close": close,
        "high": close + 0.1, "low": close - 0.1, "volume": [1e6] * n,
        "amount": [1e7] * n, "pct": [0.1] * n, "turnover": [1.0] * n,
        "ma5": close.rolling(5).mean(), "ma10": close.rolling(10).mean(),
    })


def _spot():
    return pd.DataFrame([
        {"code": "600519", "name": "贵州茅台", "price": 1500.0, "pct": 1.2,
         "chg": 17.8, "volume": 2.5e6, "amount": 3.8e9, "turnover": 0.2,
         "pe": 25.0, "pb": 8.0, "mktcap": 1.9e12, "pct60d": 5.0, "pctYtd": 12.0},
        {"code": "000858", "name": "五粮液", "price": 130.0, "pct": -0.5,
         "chg": -0.65, "volume": 4e6, "amount": 5.2e8, "turnover": 1.0,
         "pe": 18.0, "pb": 4.0, "mktcap": 5e11, "pct60d": -3.0, "pctYtd": 2.0},
    ])


@pytest.fixture
def patched_ui():
    patches = [
        patch.object(ui_data, "load_market_spot", return_value=_spot()),
        patch.object(ui_data, "load_indices", return_value=_spot()),
        patch.object(ui_data, "load_sector_boards", return_value=_spot()),
        patch.object(ui_data, "load_kline", return_value=_kline()),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


@pytest.mark.integration
class TestWebUIPages:
    """逐页渲染 Web UI，确保无异常。"""

    def test_default_page(self, patched_ui):
        at = AppTest.from_file(WEBUI, default_timeout=30)
        at.run()
        assert at.exception == []

    @pytest.mark.parametrize("nav", _NAV_PAGES)
    def test_page_renders(self, patched_ui, nav):
        at = AppTest.from_file(WEBUI, default_timeout=30)
        at.run()
        at.radio(key="nav").set_value(nav).run()
        assert at.exception == [], f"页面 {nav} 渲染异常"
