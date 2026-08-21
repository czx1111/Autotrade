"""K 线图绘制：plotly 蜡烛图 + 均线 + 成交量副图。

只做图表对象组装，不触碰网络/缓存（数据由 ``ui.data.load_kline`` 提供）。
颜色常量统一引用 :mod:`tradingagents.ui.theme`，避免分散硬编码走偏。
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from tradingagents.ui.theme import (
    ACCENT,
    BG,
    DOWN,
    MUTED,
    PLOTLY_DARK,
    TEXT,
    UP,
)

MA_COLORS = {5: "#F7AD31", 10: ACCENT, 20: UP, 60: "#9467BD"}


def build_kline_figure(df: pd.DataFrame, title: str = "", show_ma: bool = True) -> go.Figure:
    """K 线主图（蜡烛+均线）+ 成交量副图。df 需含 date/open/high/low/close/volume。"""
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.72, 0.28], vertical_spacing=0.02,
        x_title="日期",
    )

    fig.add_trace(
        go.Candlestick(
            x=df["date"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"],
            increasing_line_color=UP, decreasing_line_color=DOWN,
            increasing_fillcolor=UP, decreasing_fillcolor=DOWN,
            name="K线",
        ),
        row=1, col=1,
    )

    if show_ma:
        for window, color in MA_COLORS.items():
            col = f"ma{window}"
            if col in df.columns and df[col].notna().any():
                fig.add_trace(
                    go.Scatter(
                        x=df["date"], y=df[col], mode="lines",
                        line={"width": 1.2, "color": color},
                        name=f"MA{window}", connectgaps=False,
                    ),
                    row=1, col=1,
                )

    volume_colors = [
        UP if c >= o else DOWN
        for c, o in zip(df["close"], df["open"])
    ]
    fig.add_trace(
        go.Bar(
            x=df["date"], y=df["volume"], marker_color=volume_colors,
            name="成交量", showlegend=False,
        ),
        row=2, col=1,
    )

    fig.update_layout(
        title=title,
        height=560,
        margin={"l": 40, "r": 20, "t": 50, "b": 30},
        legend={"orientation": "h", "y": 1.02},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        **PLOTLY_DARK,
    )
    fig.update_xaxes(gridcolor="rgba(255,255,255,.06)", row=1, col=1)
    fig.update_xaxes(gridcolor="rgba(255,255,255,.06)", row=2, col=1)
    fig.update_yaxes(gridcolor="rgba(255,255,255,.06)", row=1, col=1)
    fig.update_yaxes(gridcolor="rgba(255,255,255,.06)", row=2, col=1)
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    return fig


def build_sector_bar(df: pd.DataFrame, top_n: int = 15) -> go.Figure:
    """行业板块涨跌幅横向条形图（红涨绿跌）。"""
    if df.empty:
        return go.Figure()
    top = df.head(top_n)
    colors = [UP if p >= 0 else DOWN for p in top["pct"]]
    fig = go.Figure(
        go.Bar(x=top["pct"], y=top["name"], orientation="h", marker_color=colors)
    )
    fig.update_layout(
        title=f"行业板块涨跌幅 Top{top_n}",
        height=max(360, 28 * top_n),
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        **PLOTLY_DARK,
    )
    fig.update_xaxes(gridcolor="rgba(255,255,255,.06)")
    fig.update_yaxes(gridcolor="rgba(255,255,255,.06)")
    fig.update_xaxes(title_text="涨跌幅 %")
    fig.update_yaxes(autorange="reversed")
    return fig
