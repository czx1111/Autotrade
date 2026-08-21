"""TradingAgents 控制台 —— DeepSeek 风格深色交易终端（Streamlit 入口）。

启动::

    streamlit run webui.py

九个页面：
📊 总览仪表盘   状态栏 / 四列 KPI / 双账号卡片 / 持仓饼图 / 收益曲线 / 通知流
🤖 智能体工作室 7 位分析师实时思考过程（流式活动轨）+ 决策面板 + 一键执行
📈 持仓与交易   多账号持仓 / 加减清仓 / 交易记录 / 待执行审批 / 盯盘信号 / AI 持仓分析
🔍 发现选股     指数板块 / 全市场筛选 / 一键多因子选股 + AI 复核 / 自选股管理
📐 K线看盘      多源 K 线（东财→腾讯→新浪）+ 均线成交量
🩺 系统健康     数据源探活 / 守护进程心跳 / 交易日历 / LLM 连通测试
🔔 告警中心     告警事件流（熔断/审批/止损/故障）/ 盯盘信号历史 / 待审批订单
⚙️ 策略配置     分析师组合 / 风控阈值 / 盯盘策略 / 券商连接测试 / LLM 查看
📋 历史报告     分析报告与交易日报归档、导出

自动交易守护进程在终端单独运行：python run_auto.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.ui import data as ui_data
from tradingagents.ui.common import NAV_PAGES, init_session
from tradingagents.ui.pages import PAGES
from tradingagents.ui.theme import ACCENT, MUTED, MUTED_LIGHT, TEXT

st.set_page_config(
    page_title="TradingAgents 控制台", page_icon="📈",
    layout="wide", initial_sidebar_state="expanded",
)


def _sidebar_logo() -> None:
    """DeepSeek 风格侧边栏品牌区域：Logo + 标题 + 版本号。"""
    st.markdown(
        f"<div style='padding:8px 16px 16px; margin-bottom:8px;'>"
        f"<div style='display:flex; align-items:center; gap:10px;'>"
        f"<div style='font-size:1.5rem;'>📈</div>"
        f"<div>"
        f"<div style='font-size:1.1rem; font-weight:700; color:{TEXT}; "
        f"letter-spacing:-.01em;'>TradingAgents</div>"
        f"<div style='color:{MUTED}; font-size:.75rem; margin-top:2px;'>"
        f"多智能体 LLM · A股交易终端 · v0.3.1</div>"
        f"</div></div></div>",
        unsafe_allow_html=True,
    )


def _sidebar_footer() -> None:
    """侧边栏底部信息区：系统状态 + 刷新按钮。"""
    st.divider()
    provider = DEFAULT_CONFIG.get("llm_provider", "?")
    deep_model = DEFAULT_CONFIG.get("deep_think_llm", "?")
    quick_model = DEFAULT_CONFIG.get("quick_think_llm", "?")
    backend = DEFAULT_CONFIG.get("backend_url") or "官方默认"
    st.markdown(
        f"<div style='padding:0 16px; color:{MUTED}; font-size:12px; "
        f"line-height:1.8;'>"
        f"<div>🧠 <b style='color:{MUTED_LIGHT}'>LLM</b>　{provider}</div>"
        f"<div style='padding-left:1.4em'>深度 <code style='color:{ACCENT};'>{deep_model}</code></div>"
        f"<div style='padding-left:1.4em'>快速 <code style='color:{ACCENT};'>{quick_model}</code></div>"
        f"<div>🌐 <b style='color:{MUTED_LIGHT}'>端点</b>　{backend}</div>"
        f"<div>📊 <b style='color:{MUTED_LIGHT}'>行情</b>　东财+腾讯+新浪</div>"
        f"<div style='margin-top:4px; color:#61666b;'>"
        f"自动交易: 终端运行 <code style='color:{ACCENT};'>python run_auto.py</code></div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.write("")
    if st.button("🔄 刷新行情缓存", use_container_width=True):
        ui_data.load_market_spot.clear()
        ui_data.load_indices.clear()
        ui_data.load_sector_boards.clear()
        st.toast("行情缓存已刷新", icon="🔄")


def main() -> None:
    init_session()

    # 处理 pending navigation（goto 函数通过 _pending_nav 传递）
    pending = st.session_state.pop("_pending_nav", None)
    if pending and pending in NAV_PAGES:
        st.session_state.nav = pending

    with st.sidebar:
        _sidebar_logo()
        st.radio("导航", NAV_PAGES, key="nav", label_visibility="collapsed")
        _sidebar_footer()

    PAGES[st.session_state.nav]()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    main()
