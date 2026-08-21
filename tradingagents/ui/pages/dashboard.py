"""页面一：总览仪表盘 —— 状态栏 / KPI / 双账号 / 持仓分布 / 收益曲线 / 通知流。"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.rules import trading_phase
from tradingagents.ui import data as ui_data
from tradingagents.ui import store as ui_store
from tradingagents.ui.common import (
    RESULTS_DIR,
    account_names,
    equity_figure,
    get_trader,
    positions_pie,
)
from tradingagents.ui.theme import ACCENT, DOWN, GALAXY, PINGAN, UP, status_dot

_PHASE_CN = {
    "pre_market": ("amber", "盘前"), "morning": ("green", "盘中(上午)"),
    "lunch_break": ("amber", "午间休市"), "afternoon": ("green", "盘中(下午)"),
    "post_market": ("red", "已收盘"), "closed": ("red", "闭市"),
}


def _phase_info():
    try:
        phase = trading_phase()
    except Exception:
        return ("red", "未知")
    return _PHASE_CN.get(phase, ("red", phase))


def _acct_card(name: str, info, positions_count: int, day_pnl: float | None, brand: str):
    total = f"{info.total_asset:,.2f}"
    pnl_html = ""
    if day_pnl is not None:
        color = UP if day_pnl >= 0 else DOWN
        pnl_html = (f"<div class='row'><span class='k'>今日盈亏</span>"
                    f"<span style='color:{color}'>{day_pnl:+,.2f}</span></div>")
    icon = "🟠" if brand == "pingan" else "🔴" if brand == "galaxy" else "🔵"
    st.markdown(f"""
    <div class='ta-acct {brand}'>
      <h4>{icon} {name}</h4>
      <div class='row'><span class='k'>总资产</span><span>{total}</span></div>
      <div class='row'><span class='k'>持仓市值</span><span>{info.market_value:,.2f}</span></div>
      <div class='row'><span class='k'>可用资金</span><span>{info.available_cash:,.2f}</span></div>
      {pnl_html}
      <div class='row'><span class='k'>持仓只数</span><span>{positions_count}</span></div>
    </div>
    """, unsafe_allow_html=True)


def _notifications() -> list[tuple[str, str, str]]:
    """合并通知流：盯盘信号 + 审批待办 + 成交，新→旧，最多 5 条。"""
    items: list[tuple[float, str, str, str]] = []   # (ts, icon, text, kind)

    for account in account_names():
        try:
            trader = get_trader(account)
        except Exception:
            continue
        try:
            for r in trader.get_monitor().load_history(5):
                ts = r.get("ts", "")
                stamp = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() if ts else 0
                icon = "⚠️" if r.get("outcome") == "EXECUTED" else "ℹ️"
                items.append((stamp, icon,
                              f"[{account}] {r.get('symbol')} {r.get('kind_cn')} → "
                              f"{r.get('outcome')}：{str(r.get('detail'))[:60]}", "signal"))
        except Exception:
            pass
        try:
            for e in trader.approvals.list("pending"):
                items.append((0.5, "⏳",
                              f"[{account}] 待审批：{e.action.upper()} {e.symbol} ×{e.quantity}"
                              f" ≈ ¥{e.estimate_value:,.0f}", "approval"))
        except Exception:
            pass
        try:
            for t in trader.broker.get_trades()[-3:]:
                stamp = t.traded_at.timestamp() if t.traded_at else 0
                icon = "🔴" if t.side.value == "buy" else "🟢"
                items.append((stamp, icon,
                              f"[{account}] {'买入' if t.side.value == 'buy' else '卖出'} "
                              f"{t.symbol} ×{t.quantity} @ {t.price:.2f}", "trade"))
        except Exception:
            pass

    items.sort(key=lambda x: x[0], reverse=True)
    return [(i, text) for _, i, text, _ in items[:5]]


def render() -> None:
    st.header("📊 总览仪表盘")

    # ── 顶部状态栏 ──
    dot, phase_cn = _phase_info()
    reports = ui_store.list_analysis_reports(RESULTS_DIR, limit=1)
    last_decision = (
        datetime.fromtimestamp(reports[0].stat().st_mtime).strftime("%m-%d %H:%M")
        if reports else "—"
    )
    today = datetime.now().strftime("%Y-%m-%d")
    buy_signals = sell_signals = 0
    for account in account_names():
        try:
            for r in get_trader(account).get_monitor().load_history(20):
                if r.get("ts", "").startswith(today) and r.get("outcome") == "EXECUTED":
                    if r.get("action") == "sell":
                        sell_signals += 1
        except Exception:
            pass
    st.markdown(f"""
    <div class='ta-statusbar'>
      <span>{status_dot(dot)}<b>{phase_cn}</b></span>
      <span class='muted'>|</span>
      <span>📈 今日卖出信号：<b style="color:{UP}">{sell_signals}</b></span>
      <span class='muted'>|</span>
      <span>上次决策：<b>{last_decision}</b></span>
      <span class='muted'>|</span>
      <span>守护进程：终端运行 <code style='color:{ACCENT};background:rgba(77,147,248,.08);padding:2px 6px;border-radius:4px;font-size:12px;'>python run_auto.py</code></span>
    </div>
    """, unsafe_allow_html=True)

    # ── 账号数据收集 ──
    totals = {"asset": 0.0, "cash": 0.0, "mv": 0.0}
    day_pnls: dict[str, float] = []
    acct_infos, position_counts = [], []
    all_positions = []
    errors = []
    for account in account_names():
        try:
            trader = get_trader(account)
            info = trader.broker.get_account()
            positions = trader.broker.get_positions()
            acct_infos.append((account, info, len(positions)))
            totals["asset"] += info.total_asset
            totals["cash"] += info.available_cash
            totals["mv"] += info.market_value
            day_pnls.append(info.total_asset - trader.executor.day_start_equity)
            for sym, p in positions.items():
                quote = None
                try:
                    quote = ui_data.get_quote(sym) or {}
                except Exception:
                    quote = {}
                price = quote.get("price") or p.last_price or p.avg_cost
                all_positions.append((sym, quote.get("name", "") or p.name, p.quantity * price))
        except Exception as exc:
            errors.append(f"{account}: {exc}")

    if errors and not acct_infos:
        st.error("无法连接任何账号：" + "；".join(errors))
        st.info("实盘请先启动同花顺/miniQMT 客户端并登录；模拟盘无需任何客户端。")
        return

    # ── 四列 KPI ──
    total_pnl = sum(day_pnls) if day_pnls else None
    pending_count = 0
    for account in account_names():
        try:
            trader = get_trader(account)
            pending_count += len(trader.approvals.list("pending")) \
                + len(trader.approvals.list("approved"))
        except Exception:
            pass

    # 昨日总资产（净值历史倒数第二个点，跨账号求和）
    prev_total = 0.0
    for account in account_names():
        hist = ui_store.load_equity_history(RESULTS_DIR, account)
        if len(hist) >= 2:
            prev_total += float(hist[-2].get("total_asset") or 0)
    delta_txt = "—"
    if prev_total > 0 and totals["asset"] > 0:
        delta_txt = f"{(totals['asset'] / prev_total - 1) * 100:+.2f}% vs 昨日收盘"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("总资产 (¥)", f"{totals['asset']:,.0f}", delta_txt)
    if total_pnl is not None:
        c2.metric("今日盈亏 (¥)", f"{total_pnl:+,.0f}",
                  f"{total_pnl / max(totals['asset'] - total_pnl, 1) * 100:+.2f}%",
                  delta_color="normal")
    else:
        c2.metric("今日盈亏 (¥)", "—")
    c3.metric("持仓股票数", len(all_positions),
              f"仓位 {totals['mv'] / max(totals['asset'], 1):.0%}")
    c4.metric("待执行订单", pending_count, "含审批队列")

    # ── 双账号卡片 ──
    st.subheader("账户总览")
    _brand_cycle = ["pingan", "galaxy"]
    cols = st.columns(len(acct_infos) or 1)
    for i, (account, info, count) in enumerate(acct_infos):
        with cols[i]:
            day_pnl = day_pnls[i] if i < len(day_pnls) else None
            _acct_card(account, info, count, day_pnl, _brand_cycle[i % len(_brand_cycle)])

    # ── 中部：饼图 + 收益曲线 ──
    col_l, col_r = st.columns([6, 4])
    with col_l:
        if all_positions:
            labels = [f"{n or s}" for s, n, _ in all_positions]
            values = [v for _, _, v in all_positions]
            st.plotly_chart(positions_pie(labels, values), use_container_width=True)
        else:
            st.info("暂无持仓 —— 持仓后此处显示仓位分布")
    with col_r:
        merged: dict[str, float] = {}
        for account in account_names():
            for p in ui_store.load_equity_history(RESULTS_DIR, account)[-7:]:
                merged[p["date"]] = merged.get(p["date"], 0.0) + float(p.get("total_asset") or 0)
        if len(merged) >= 2:
            st.plotly_chart(
                equity_figure([{"date": d, "total_asset": v} for d, v in sorted(merged.items())]),
                use_container_width=True,
            )
        else:
            st.info("收益曲线将在每日收盘后积累（运行 `run_auto.py` 或手动复盘）")

    # ── 底部：系统通知流 ──
    st.subheader("🔔 系统通知")
    notes = _notifications()
    if notes:
        for icon, text in notes:
            st.markdown(f"- {icon} {text}")
    else:
        st.caption("暂无通知 —— 盯盘信号、成交与审批事项会显示在这里")
    if errors:
        with st.expander(f"连接异常（{len(errors)}）"):
            for e in errors:
                st.error(e)
