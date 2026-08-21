"""📐 K线看盘页 —— 多源 K 线 + 实时报价卡片（从旧版迁移）。"""

from __future__ import annotations

import streamlit as st

from tradingagents.ui import data as ui_data
from tradingagents.ui.charts import build_kline_figure
from tradingagents.ui.common import goto, symbol_picker
from tradingagents.ui.theme import DOWN, UP


def render() -> None:
    st.header("📐 K线看盘")

    with st.sidebar:
        st.subheader("图表设置")
        days = st.select_slider("回看范围", options=[60, 120, 250, 500], value=250)
        show_ma = st.toggle("显示均线 (MA5/10/20/60)", value=True)

    code, name = symbol_picker("chart_symbol", "查询")
    if not code:
        st.info("输入股票代码或名称开始看盘")
        return

    quote = None
    try:
        quote = ui_data.get_quote(code)
    except Exception:
        pass
    if quote:
        pct = quote.get("pct") or 0
        color = UP if pct >= 0 else DOWN
        st.markdown(
            f"<span style='font-size:1.35rem;font-weight:700'>{quote.get('name','')} "
            f"({code})</span>&nbsp;&nbsp;"
            f"<span style='font-size:1.7rem;font-weight:700;color:{color}'>"
            f"{quote.get('price','-')}</span>&nbsp;&nbsp;"
            f"<span style='color:{color};font-weight:600'>{pct:+.2f}%</span>",
            unsafe_allow_html=True,
        )
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("成交额", ui_data.fmt_amount(quote.get("amount") or 0))
        c2.metric("换手率", f"{quote.get('turnover') or 0:.2f}%")
        c3.metric("市盈率(动)", f"{quote.get('pe') or '-'}")
        c4.metric("市净率", f"{quote.get('pb') or '-'}")
        c5.metric("总市值", ui_data.fmt_amount(quote.get("mktcap") or 0))

    c1, c2, c3 = st.columns(3)
    if c1.button("🤖 AI 分析", use_container_width=True):
        goto("🤖 智能体工作室", analyze_symbol=code)
    if c2.button("⭐ 加自选", use_container_width=True):
        goto("🔍 发现选股", add_to_watchlist=code)
    if c3.button("⚡ 交易", use_container_width=True):
        goto("📈 持仓与交易")

    df = ui_data.load_kline(code, days=days)
    if df.empty:
        st.warning("暂无 K 线数据（三个数据源均失败）")
        return
    st.plotly_chart(
        build_kline_figure(df, title=f"{name or code} 日K（前复权·多源）", show_ma=show_ma),
        use_container_width=True,
    )
    with st.expander("最近 10 个交易日"):
        st.dataframe(df.tail(10).set_index("date"), use_container_width=True)
