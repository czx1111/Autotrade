"""临时诊断：逐页渲染列出异常。"""
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tradingagents.ui.data as ui_data

WEBUI = Path(__file__).resolve().parents[1] / "webui.py"


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


patches = [
    patch.object(ui_data, "load_market_spot", return_value=_spot()),
    patch.object(ui_data, "load_indices", return_value=_spot()),
    patch.object(ui_data, "load_sector_boards", return_value=_spot()),
    patch.object(ui_data, "load_kline", return_value=_kline()),
]
for p in patches:
    p.start()

at = AppTest.from_file(WEBUI, default_timeout=30)
at.run()
print("default ok:", at.exception == [])

for nav in ("📊 总览仪表盘", "🤖 智能体工作室", "📈 持仓与交易",
            "🔍 发现选股", "📐 K线看盘", "⚙️ 策略配置", "📋 历史报告"):
    try:
        at.radio(key="nav").set_value(nav).run()
        if at.exception:
            print(f"[FAIL] {nav}")
            for e in at.exception:
                print("   ", e.message)
                print("   ", (e.stack_trace or "")[-600:])
        else:
            print(f"[OK] {nav}")
    except Exception as exc:
        print(f"[ERROR] {nav}: {exc}")
        if at.exception:
            for e in at.exception:
                print("   ", e.message)
                print("   ", (e.stack_trace or "")[-800:])

for p in patches:
    p.stop()
