"""页面：告警中心 —— 通知/告警事件流 / 盯盘信号 / 待审批订单聚合。"""

from __future__ import annotations

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.notifier import load_notification_history
from tradingagents.ui.common import get_trader, account_names
from tradingagents.ui.theme import DOWN, MUTED, TEXT, UP

_LEVEL_STYLE = {
    "critical": ("#F25A5A", "🚨"),
    "warning": ("#F7AD31", "⚠️"),
    "info": ("#4D93F8", "✅"),
}


def _notification_card(rec: dict) -> None:
    """单条通知事件卡片。"""
    level = rec.get("level", "info")
    color, icon = _LEVEL_STYLE.get(level, (MUTED, "•"))
    text = rec.get("text", "")
    # markdown 加粗/换行转成 HTML 展示
    text_html = (
        text.replace("**", "")
            .replace("\n\n", "<br>")
            .replace("`", "")
    )
    st.markdown(
        f"<div style='border-left:3px solid {color}; background:rgba(255,255,255,.02);"
        f" border-radius:0 10px 10px 0; padding:10px 14px; margin-bottom:8px;"
        f" font-size:13px;'>"
        f"<div style='display:flex; justify-content:space-between; margin-bottom:4px;'>"
        f"<span style='font-weight:600; color:{TEXT};'>{icon} {rec.get('title', '')}</span>"
        f"<span style='color:{MUTED};'>{rec.get('ts', '')}</span>"
        f"</div>"
        f"<div style='color:{MUTED}; line-height:1.7;'>{text_html}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_notifications_tab() -> None:
    webhook_configured = bool(DEFAULT_CONFIG.get("dingtalk_webhook"))
    if not webhook_configured:
        st.caption(
            "ℹ️ 钉钉 webhook 未配置 —— 事件仅在此处记录。"
            "在 `.env` 填入 `TRADINGAGENTS_DINGTALK_WEBHOOK` 可同步推送到钉钉群"
        )

    history = load_notification_history(100)
    if not history:
        st.info(
            "暂无告警事件 —— 系统运行正常时这里保持安静；"
            "日亏熔断 / 大额待审批 / 止损执行 / 数据源故障 / 守护进程异常都会出现在这里",
            icon="🔔",
        )
        return

    counts = {
        lvl: sum(1 for r in history if r.get("level") == lvl)
        for lvl in ("critical", "warning", "info")
    }
    c1, c2, c3 = st.columns(3)
    c1.metric("严重", counts["critical"])
    c2.metric("警告", counts["warning"])
    c3.metric("通知", counts["info"])

    for rec in history:
        _notification_card(rec)


def _render_signals_tab() -> None:
    st.subheader("盯盘信号历史")
    st.caption("止损/止盈/移动止损/均线死叉触发记录（跨账号聚合，新→旧）")

    any_rows = False
    for acct in account_names():
        try:
            trader = get_trader(acct)
            history = trader.get_monitor().load_history(100)
        except Exception:
            continue
        if not history:
            continue
        any_rows = True
        with st.expander(f"📈 {acct}（最近 {len(history)} 条）"):
            st.dataframe([{
                "时间": r.get("ts"),
                "代码": r.get("symbol"),
                "名称": r.get("name"),
                "信号": r.get("kind_cn"),
                "现价": r.get("price"),
                "执行": {
                    "EXECUTED": "✅已卖出", "SKIPPED_T1": "⏳T+1明日重试",
                }.get(r.get("outcome"), r.get("outcome", "")),
                "说明": r.get("detail"),
            } for r in history], use_container_width=True, hide_index=True)

    if not any_rows:
        st.info("暂无盯盘信号 —— 持仓都在策略区间内", icon="📡")


def _render_approvals_tab() -> None:
    st.subheader("待审批订单")
    st.caption("大额订单（≥ 审批阈值）等待人工批准；批准后下一轮盘中扫描自动执行")

    any_pending = False
    for acct in account_names():
        try:
            trader = get_trader(acct)
            store = trader.approvals
        except Exception:
            continue
        open_entries = store.list("pending") + store.list("approved")
        if not open_entries:
            continue
        any_pending = True
        for e in open_entries:
            with st.container(border=True):
                c1, c2, c3 = st.columns([4, 1, 1])
                c1.markdown(
                    f"**{e.action.upper()} {e.symbol}** × {e.quantity} 股 "
                    f"≈ ¥{e.estimate_value:,.0f} · {e.created_at}"
                )
                c1.caption(f"账号：{acct} · 来源：{e.reason}")
                if e.status == "approved":
                    c2.success("已批准", icon="✅")
                elif c2.button("批准", key=f"aok_{acct}_{e.id}", type="primary"):
                    store.set_status(e.id, "approved")
                    st.toast("已批准，下轮自动执行", icon="✅")
                    st.rerun()
                if c3.button("取消", key=f"ano_{acct}_{e.id}"):
                    store.set_status(e.id, "rejected")
                    st.toast("已取消", icon="🚫")
                    st.rerun()

    if not any_pending:
        st.success("无待审批订单", icon="✅")


def render() -> None:
    st.header("🔔 告警中心")

    tab_notif, tab_sig, tab_appr = st.tabs(
        ["📨 告警事件流", "📡 盯盘信号", "⏳ 待审批订单"]
    )
    with tab_notif:
        _render_notifications_tab()
    with tab_sig:
        _render_signals_tab()
    with tab_appr:
        _render_approvals_tab()
