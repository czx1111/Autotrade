"""页面五：历史报告 —— 分析报告归档 / 交易日报 / 导出。"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.ui import store as ui_store
from tradingagents.ui.common import RESULTS_DIR


def render() -> None:
    st.header("📋 历史报告")

    tab_a, tab_d = st.tabs(["🤖 分析报告", "📅 交易日报"])

    with tab_a:
        reports = ui_store.list_analysis_reports(RESULTS_DIR, limit=100)
        if not reports:
            st.info("暂无分析报告 —— 在「🤖 智能体工作室」运行一次分析即可生成")
            return
        options = {
            f"{p.name}（{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}）": p
            for p in reports
        }
        picked = st.selectbox("选择报告", list(options))
        path = options[picked]
        text = path.joinpath("complete_report.md").read_text(encoding="utf-8")

        c1, c2 = st.columns([4, 1])
        c1.markdown(text)
        with c2:
            st.download_button(
                "⬇️ 导出 Markdown", data=text,
                file_name=f"{path.name}.md", mime="text/markdown",
                use_container_width=True,
            )
            # 简易 PDF 导出：浏览器打印（无需额外依赖）
            st.markdown(
                "<div class='ta-risk'>如需 PDF：点击上方导出 Markdown 后用浏览器打印"
                "（Ctrl+P → 另存为 PDF）。</div>",
                unsafe_allow_html=True,
            )
            subs = [d.name for d in path.iterdir() if d.is_dir()]
            if subs:
                st.caption(f"分节文件：{', '.join(subs)}")

    with tab_d:
        summaries = ui_store.list_daily_summaries(RESULTS_DIR)
        if not summaries:
            st.info("暂无日报 —— 由 `run_auto.py` 盘后阶段自动生成")
            return
        picked = st.selectbox(
            "选择日报",
            [p.name for p in summaries],
            format_func=lambda n: n.replace(".md", ""),
        )
        path = next(p for p in summaries if p.name == picked)
        text = path.read_text(encoding="utf-8")
        c1, c2 = st.columns([4, 1])
        c1.markdown(text)
        c2.download_button(
            "⬇️ 导出 Markdown", data=text,
            file_name=path.name, mime="text/markdown",
            use_container_width=True,
        )
