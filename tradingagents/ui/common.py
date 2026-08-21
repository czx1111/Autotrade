"""Web UI 公共组件：会话状态、trader 缓存、股票选择器、图表辅助。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tradingagents.dataflows.ashare_symbol_utils import normalize_ashare_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.ui import data as ui_data
from tradingagents.ui import store as ui_store
from tradingagents.ui.theme import PLOTLY_DARK, UP, DOWN, ACCENT, MUTED, TEXT, inject_theme

RESULTS_DIR = DEFAULT_CONFIG["results_dir"]

NAV_PAGES = [
    "📊 总览仪表盘", "🤖 智能体工作室", "📈 持仓与交易",
    "🔍 发现选股", "📐 K线看盘", "🩺 系统健康", "🔔 告警中心",
    "⚙️ 策略配置", "📋 历史报告",
]

# 7 分析师注册表（key → 中文名/图标/报告字段）
ANALYST_REGISTRY = {
    "market": ("市场分析师", "📈", "market_report"),
    "social": ("舆情分析师", "💬", "sentiment_report"),
    "news": ("新闻分析师", "📰", "news_report"),
    "fundamentals": ("基本面分析师", "📊", "fundamentals_report"),
    "policy": ("政策分析师", "🏛️", "policy_report"),
    "hotmoney": ("游资追踪", "🐉", "hotmoney_report"),
    "unlock": ("解禁监控", "🔓", "unlock_report"),
}
DEFAULT_ANALYSTS = ["market", "social", "news", "fundamentals", "policy", "hotmoney", "unlock"]

# 图节点名 → (展示名, 图标)
NODE_LABELS = {
    "Market Analyst": ("市场分析师", "📈"), "Sentiment Analyst": ("舆情分析师", "💬"),
    "News Analyst": ("新闻分析师", "📰"), "Fundamentals Analyst": ("基本面分析师", "📊"),
    "Policy Analyst": ("政策分析师", "🏛️"), "Hotmoney Analyst": ("游资追踪", "🐉"),
    "Unlock Analyst": ("解禁监控", "🔓"),
    "Bull Researcher": ("多方研究员", "🐂"), "Bear Researcher": ("空方研究员", "🐻"),
    "Research Manager": ("研究经理", "⚖️"), "Trader": ("交易员", "📋"),
    "Aggressive Analyst": ("激进风控", "🔥"), "Conservative Analyst": ("保守风控", "🧊"),
    "Neutral Analyst": ("中立风控", "🛡️"), "Portfolio Manager": ("组合经理", "🎯"),
}


def init_session() -> None:
    inject_theme()
    defaults = {
        "nav": NAV_PAGES[0],
        "chart_symbol": "600519",
        "analyze_symbol": None,
        "trade_symbol": None,
        "graph": None,
        "graph_analysts": None,
        "brokers": {},
        "traders": {},
        "pending_action": None,   # (account, symbol, action) 持仓页快捷下单
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def goto(page: str, **extra) -> None:
    # 不能直接修改 st.session_state.nav（widget 已实例化），
    # 用 _pending_nav 标记，main 循环会在下次 rerun 前处理
    st.session_state._pending_nav = page
    for key, value in extra.items():
        st.session_state[key] = value
    st.rerun()


def account_names() -> list[str]:
    return [a.get("name", "?") for a in ui_store.load_accounts()]


def get_trader(account_name: str):
    """按账号缓存 AutoTrader（broker/executor/风控/审批共用单例）。"""
    accounts = ui_store.load_accounts()
    account = next(a for a in accounts if a.get("name") == account_name)
    traders = st.session_state.traders
    if account_name not in traders:
        from pathlib import Path

        from tradingagents.auto_trader import AutoTrader
        from tradingagents.broker import get_broker

        brokers = st.session_state.brokers
        if account_name not in brokers:
            brokers[account_name] = get_broker({
                **(account.get("broker_settings") or {}),
                "account_name": account_name,
            })
        traders[account_name] = AutoTrader(
            account, DEFAULT_CONFIG.copy(), broker=brokers[account_name],
            state_dir=Path(DEFAULT_CONFIG["results_dir"]) / "state",
        )
    return traders[account_name]


def reset_trader(account_name: str) -> None:
    st.session_state.traders.pop(account_name, None)


def resolve_symbol(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    code = normalize_ashare_symbol(raw)
    if code:
        return code
    try:
        hits = ui_data.search_stocks(ui_data.load_market_spot(), raw)
        if len(hits) == 1:
            return str(hits.iloc[0]["code"])
        if len(hits) > 1:
            return f"AMBIGUOUS:{len(hits)}"
    except Exception:
        pass
    # 快照不可用时，直接尝试把输入当代码规范化
    code = normalize_ashare_symbol(raw)
    return code if code else None


def symbol_picker(default_key: str, action_label: str = "查询") -> tuple[str, str]:
    """通用股票选择器：输入 + 解析 + 多候选下拉。返回 (code, name)。"""
    raw = st.text_input(
        "股票代码或名称", value=st.session_state.get(default_key, ""),
        placeholder="如 600519 / 贵州茅台", key=f"{default_key}_input",
    )
    col1, col2 = st.columns([1, 2])
    if col1.button(action_label, key=f"{default_key}_btn", use_container_width=True):
        resolved = resolve_symbol(raw)
        if resolved is None:
            st.warning("未找到该股票，请检查代码或名称")
        elif resolved.startswith("AMBIGUOUS:"):
            st.info("匹配到多只股票，请在下方选择")
            try:
                st.session_state[f"{default_key}_candidates"] = ui_data.search_stocks(
                    ui_data.load_market_spot(), raw
                )
            except Exception:
                st.session_state[f"{default_key}_candidates"] = None
                st.warning("行情数据暂不可用，请直接输入股票代码")
        else:
            st.session_state[default_key] = resolved
            st.session_state.pop(f"{default_key}_candidates", None)

    candidates = st.session_state.get(f"{default_key}_candidates")
    if candidates is not None and not candidates.empty:
        options = {
            f"{r['code']} {r['name']}": str(r["code"])
            for _, r in candidates.head(20).iterrows()
        }
        picked = col2.selectbox("选择股票", list(options), key=f"{default_key}_select")
        if picked:
            st.session_state[default_key] = options[picked]
            st.session_state.pop(f"{default_key}_candidates", None)

    code = st.session_state.get(default_key) or ""
    name = ""
    if code and not code.startswith("AMBIGUOUS"):
        try:
            quote = ui_data.get_quote(code)
            if quote:
                name = str(quote.get("name", ""))
        except Exception:
            pass
    return code, name


def prev_close_of(quote: dict) -> float | None:
    if not quote:
        return None
    if quote.get("prev_close"):
        return float(quote["prev_close"])
    chg = quote.get("chg") or 0.0
    return float(quote.get("price", 0) - chg) or None


def render_rating(rating: str) -> None:
    from tradingagents.ui.theme import rating_badge

    if not rating:
        st.caption("未能解析评级，请查看决策全文")
        return
    st.markdown(rating_badge(rating), unsafe_allow_html=True)


def dark_figure(fig: go.Figure) -> go.Figure:
    """应用深色主题到 plotly 图。"""
    fig.update_layout(**PLOTLY_DARK)
    return fig


def equity_figure(history: list[dict]) -> go.Figure:
    """净值曲线（近 N 点）。"""
    fig = go.Figure()
    dates = [p["date"] for p in history]
    values = [float(p.get("total_asset") or 0) for p in history]
    fig.add_trace(go.Scatter(
        x=dates, y=values, mode="lines+markers",
        line={"color": ACCENT, "width": 2},
        marker={"size": 5, "color": ACCENT},
        fill="tozeroy", fillcolor=f"rgba(77,147,248,.06)",
    ))
    fig.update_layout(
        title="近 7 日总资产", height=300,
        margin={"l": 50, "r": 20, "t": 45, "b": 30},
        xaxis={"gridcolor": "rgba(255,255,255,.06)"},
        yaxis={"gridcolor": "rgba(255,255,255,.06)"},
        showlegend=False,
    )
    return dark_figure(fig)


def positions_pie(labels: list[str], values: list[float]) -> go.Figure:
    """持仓分布饼图。"""
    fig = go.Figure(go.Pie(
        labels=labels, values=values, hole=0.55,
        marker={"colors": _palette(len(labels))},
        textinfo="percent", textfont={"color": TEXT},
    ))
    fig.update_layout(height=300, margin={"l": 10, "r": 10, "t": 30, "b": 10},
                      showlegend=True,
                      legend={"font": {"size": 10, "color": MUTED}})
    return dark_figure(fig)


def _palette(n: int) -> list[str]:
    # DeepSeek 风格调色板：以品牌蓝为主，搭配辅助色
    base = ["#4D93F8", "#F25A5A", "#F7AD31", "#4ED17E", "#9467BD",
            "#F26B1F", "#60A5FA", "#FF8A9B", "#7E57C2", "#66BB6A"]
    return [base[i % len(base)] for i in range(max(n, 1))]


def parse_rating_text(decision_text: str) -> str | None:
    from tradingagents.auto_trader import parse_rating

    return parse_rating(decision_text)


def fmt_signed(value: float, suffix: str = "") -> tuple[str, str]:
    """带符号数值 + 颜色（涨红跌绿）。"""
    color = UP if value >= 0 else DOWN
    return f"{value:+,.2f}{suffix}", color


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _quick_llm():
    """轻量 LLM 客户端（选股复核 / 持仓建议）。"""
    from tradingagents.llm_clients import create_llm_client

    return create_llm_client(
        DEFAULT_CONFIG["llm_provider"], DEFAULT_CONFIG["quick_think_llm"],
        DEFAULT_CONFIG.get("backend_url"),
    ).get_llm()
