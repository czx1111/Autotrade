"""页面三：持仓与交易 —— 账号 Tab / 持仓操作 / 交易记录 / 待执行队列 / 盯盘信号 / AI 持仓分析。"""

from __future__ import annotations

import streamlit as st

from tradingagents.ui import data as ui_data
from tradingagents.ui import store as ui_store
from tradingagents.ui.common import (
    RESULTS_DIR,
    account_names,
    get_trader,
    prev_close_of,
    reset_trader,
)
from tradingagents.ui.theme import DOWN, UP

_MODE_CN = {"paper": "🧪 模拟盘", "qmt": "🏦 miniQMT 实盘", "easytrader": "🏦 券商客户端实盘"}


def render() -> None:
    st.header("📈 持仓与交易")

    names = account_names()
    tabs = st.tabs(["全部账号"] + names)

    with tabs[0]:
        _render_all_accounts(names)

    for name, tab in zip(names, tabs[1:]):
        with tab:
            _render_account(name)


def _render_all_accounts(names: list[str]) -> None:
    """全部账号汇总视图：总 KPI + 各账号持仓合并表。"""
    rows = []
    for name in names:
        try:
            trader = get_trader(name)
            info = trader.broker.get_account()
            positions = trader.broker.get_positions()
        except Exception as exc:
            st.error(f"[{name}] 连接失败：{exc}")
            continue
        for sym, p in positions.items():
            quote = None
            try:
                quote = ui_data.get_quote(sym) or {}
            except Exception:
                quote = {}
            price = quote.get("price") or p.last_price or p.avg_cost
            pnl = (price - p.avg_cost) * p.quantity if p.avg_cost else 0.0
            pnl_pct = (price / p.avg_cost - 1) * 100 if p.avg_cost else 0.0
            rows.append({
                "账号": name, "代码": sym, "名称": quote.get("name", "") or p.name,
                "持仓": p.quantity, "可卖": p.available,
                "成本": f"{p.avg_cost:.2f}", "现价": f"{price:.2f}",
                "盈亏¥": f"{pnl:+,.0f}", "盈亏%": f"{pnl_pct:+.1f}",
                "市值": f"{p.quantity * price:,.0f}",
                "仓位%": f"{p.quantity * price / max(info.total_asset, 1) * 100:.1f}",
            })
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.caption("红涨绿跌遵循 A 股惯例；排序点击表头。")
    else:
        st.info("全部账号暂无持仓")


def _render_account(name: str) -> None:
    try:
        trader = get_trader(name)
        broker = trader.broker
        info = broker.get_account()
        positions = broker.get_positions()
    except Exception as exc:
        st.error(f"连接失败：{exc}")
        st.info("实盘请先启动对应客户端并登录，然后刷新页面。")
        return

    st.markdown(f"**通道**：{_MODE_CN.get(broker.mode, broker.mode)}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("总资产 (¥)", f"{info.total_asset:,.2f}")
    c2.metric("可用资金 (¥)", f"{info.available_cash:,.2f}")
    c3.metric("持仓市值 (¥)", f"{info.market_value:,.2f}")
    day_pnl = info.total_asset - trader.executor.day_start_equity
    c4.metric("今日盈亏 (¥)", f"{day_pnl:+,.2f}")

    # ── AI 持仓分析 ──
    col_ai, col_patrol = st.columns([1, 1])
    if col_ai.button("🧠 AI 持仓分析", disabled=not positions,
                     type="primary", use_container_width=True,
                     key=f"ai_pos_{name}"):
        _ai_portfolio_advice(trader, positions)
    if col_patrol.button("🔍 立即盯盘巡检", disabled=not positions,
                         use_container_width=True,
                         key=f"patrol_{name}"):
        records = trader.get_monitor().check_once()
        if records:
            for r in records:
                if r.get("outcome") == "EXECUTED":
                    st.warning(f"⚠️ {r.get('ts')} {r.get('symbol')} {r.get('kind_cn')}"
                               f" → 已卖出 {r.get('quantity')} 股（{r.get('detail')}）")
                    st.toast(f"自动卖出执行：{r.get('symbol')}", icon="⚠️")
                else:
                    st.info(f"{r.get('ts')} {r.get('symbol')} {r.get('kind_cn')}"
                            f" → {r.get('outcome')}")
        else:
            st.success("巡检完成：持仓均在策略区间内")

    # ── 持仓表 + 快捷操作 ──
    st.subheader("持仓")
    if not positions:
        st.info("当前无持仓")
    else:
        rows = []
        for sym, p in positions.items():
            quote = None
            try:
                quote = ui_data.get_quote(sym) or {}
            except Exception:
                quote = {}
            price = quote.get("price") or p.last_price or p.avg_cost
            pnl_pct = (price / p.avg_cost - 1) * 100 if p.avg_cost else 0.0
            rows.append({
                "代码": sym, "名称": quote.get("name", "") or p.name,
                "持仓": p.quantity, "可卖": p.available,
                "成本": p.avg_cost, "现价": price,
                "盈亏%": pnl_pct, "市值": p.quantity * price,
            })
        rows.sort(key=lambda r: r["盈亏%"])
        for r in rows:
            color = UP if r["盈亏%"] >= 0 else DOWN
            cN, cP, cA = st.columns([5, 4, 3])
            cN.markdown(
                f"**{r['名称']}** ({r['代码']}) · {r['持仓']} 股 / 可卖 {r['可卖']} · "
                f"成本 {r['成本']:.2f} → 现价 <b>{r['现价']:.2f}</b> "
                f"<span style='color:{color}'>({r['盈亏%']:+.1f}%)</span>",
                unsafe_allow_html=True,
            )
            qa, qr, qc = cA.columns(3)
            if qa.button("＋加仓", key=f"add_{name}_{r['代码']}", use_container_width=True):
                st.session_state.pending_action = (name, r["代码"], "buy")
            if qr.button("－减仓", key=f"cut_{name}_{r['代码']}", disabled=r["可卖"] <= 0,
                         use_container_width=True):
                st.session_state.pending_action = (name, r["代码"], "sell_half")
            if qc.button("✖清仓", key=f"exit_{name}_{r['代码']}", disabled=r["可卖"] <= 0,
                         use_container_width=True):
                st.session_state.pending_action = (name, r["代码"], "sell_all")

    # 快捷下单表单（由上方按钮触发）
    pending = st.session_state.get("pending_action")
    if pending:
        acct, sym, act = pending
        if act == "buy":
            with st.form("quick_buy", border=True):
                st.markdown(f"#### ＋ 加仓 {sym}")
                amount = st.number_input("加仓金额 (¥)", 1000.0, None, 10000.0, 1000.0)
                if st.form_submit_button("确认买入", type="primary"):
                    _quick_trade(acct, sym, "buy", amount=amount)
                    st.session_state.pending_action = None
        else:
            pos = positions.get(sym)
            if pos:
                sellable = (pos.available // 100) * 100
                default_qty = sellable // 2 if act == "sell_half" else sellable
                with st.form("quick_sell", border=True):
                    st.markdown(f"#### {'－ 减仓' if act == 'sell_half' else '✖ 清仓'} {sym}")
                    qty = st.number_input("卖出股数", 100, sellable, max(default_qty, 100), 100)
                    if st.form_submit_button("确认卖出", type="primary"):
                        _quick_trade(acct, sym, "sell", quantity=qty)
                        st.session_state.pending_action = None

    # ── 手动下单 ──
    with st.expander("⚡ 手动下单（任意股票）"):
        code = st.text_input("股票代码", key=f"manual_code_{name}")
        col_b, col_s = st.columns(2)
        if col_b.button("查询并下单", key=f"manual_go_{name}"):
            st.session_state[f"manual_target_{name}"] = code.strip()
        target = st.session_state.get(f"manual_target_{name}")
        if target:
            quote = None
            try:
                quote = ui_data.get_quote(target) or {}
            except Exception:
                quote = {}
            if not quote:
                st.warning("未获取到行情")
            else:
                st.markdown(f"**{quote.get('name','')}** 现价 {quote.get('price')}")
                with st.form(f"manual_form_{name}"):
                    action = st.radio("方向", ["买入", "卖出"], horizontal=True)
                    amount = st.number_input("金额 (¥)", 100.0, None, 10000.0, 100.0)
                    if st.form_submit_button("下单", type="primary"):
                        price = float(quote.get("price") or 0)
                        qty = int(amount / price // 100) * 100
                        rec = trader.submit_manual_trade(
                            target, "buy" if action == "买入" else "sell",
                            qty, price, name=str(quote.get("name", "")),
                            prev_close=prev_close_of(quote),
                        )
                        _show_result(rec)

    # ── 交易记录 ──
    st.subheader("今日成交")
    trades = broker.get_trades()
    if trades:
        st.dataframe([{
            "时间": t.traded_at.strftime("%H:%M:%S"), "代码": t.symbol,
            "方向": "🔴买入" if t.side.value == "buy" else "🟢卖出",
            "数量": t.quantity, "价格": t.price, "佣金": t.commission,
        } for t in trades], use_container_width=True, hide_index=True)
    else:
        st.caption("今日暂无成交")

    # ── 待执行订单（审批队列）──
    st.subheader("⏳ 待执行订单（大额审批队列）")
    store = trader.approvals
    open_entries = store.list("pending") + store.list("approved")
    if open_entries:
        for e in open_entries:
            with st.container(border=True):
                c1, c2, c3 = st.columns([4, 1, 1])
                c1.markdown(
                    f"**{e.action.upper()} {e.symbol}** × {e.quantity} 股 "
                    f"≈ ¥{e.estimate_value:,.0f} · {e.created_at}"
                )
                c1.caption(f"来源：{e.reason}")
                if e.status == "approved":
                    c2.success("已批准", icon="✅")
                elif c2.button("批准", key=f"ok_{name}_{e.id}", type="primary"):
                    store.set_status(e.id, "approved")
                    st.toast("已批准，下轮自动执行", icon="✅")
                    st.rerun()
                if c3.button("取消", key=f"no_{name}_{e.id}"):
                    store.set_status(e.id, "rejected")
                    st.toast("已取消", icon="🚫")
                    st.rerun()
    else:
        st.caption("无待执行订单")

    # ── 盯盘策略状态与信号历史 ──
    with st.expander("📡 盯盘策略状态（止损/止盈线）"):
        monitor = trader.get_monitor()
        statuses = monitor.position_status()
        if statuses:
            st.dataframe([{
                "代码": s["symbol"], "名称": s["name"], "持仓": s["quantity"],
                "成本": f"{s['cost']:.2f}", "现价": f"{s['price']:.2f}",
                "浮盈%": f"{s['pnl_pct']:+.1f}",
                "止损线": f"{s['stop_line']:.2f}", "止盈线": f"{s['target_line']:.2f}",
                "持有天数": s["hold_days"],
            } for s in statuses], use_container_width=True, hide_index=True)
        else:
            st.caption("无持仓")
        history = monitor.load_history(50)
        if history:
            st.dataframe([{
                "时间": r.get("ts"), "代码": r.get("symbol"), "信号": r.get("kind_cn"),
                "执行": {"EXECUTED": "✅已卖出", "SKIPPED_T1": "⏳T+1明日重试"}.get(
                    r.get("outcome"), r.get("outcome", "")),
                "说明": r.get("detail"),
            } for r in history], use_container_width=True, hide_index=True)


def _quick_trade(acct: str, sym: str, action: str, *, amount: float | None = None,
                 quantity: int | None = None) -> None:
    trader = get_trader(acct)
    quote = None
    try:
        quote = ui_data.get_quote(sym) or {}
    except Exception:
        quote = {}
    price = float(quote.get("price") or 0)
    if price <= 0:
        st.error("无实时价格，无法下单")
        return
    if quantity is None:
        quantity = int((amount or 10000) / price // 100) * 100
    rec = trader.submit_manual_trade(
        sym, action, quantity, price,
        name=str(quote.get("name", "")), prev_close=prev_close_of(quote),
    )
    _show_result(rec)


def _show_result(rec: dict) -> None:
    outcome = rec.get("outcome")
    if outcome == "EXECUTED":
        st.success(f"✅ 已提交：{rec.get('action')} {rec.get('symbol')} × {rec.get('quantity')} 股")
        st.toast("交易已提交", icon="✅")
    elif outcome == "PENDING_APPROVAL":
        st.warning(f"⏳ 需审批：{rec.get('detail')}（ID `{rec.get('approval_id')}`）")
    else:
        st.error(f"❌ 未成交：{rec.get('detail')}")
        checks = rec.get("checks") or []
        if checks:
            with st.expander("风控明细"):
                st.table({
                    "检查项": [c[0] for c in checks],
                    "结果": ["✅" if c[1] else "❌" for c in checks],
                    "说明": [c[2] for c in checks],
                })


def _ai_portfolio_advice(trader, positions) -> None:
    with st.spinner("AI 正在分析持仓组合…"):
        try:
            from tradingagents.default_config import DEFAULT_CONFIG as DC
            from tradingagents.llm_clients import create_llm_client

            monitor = trader.get_monitor()
            statuses = monitor.position_status()
            lines = ["代码,名称,持仓,可卖,成本,现价,浮盈%,持有天数"]
            for s in statuses:
                lines.append(
                    f"{s['symbol']},{s['name']},{s['quantity']},{s['available']},"
                    f"{s['cost']:.2f},{s['price']:.2f},{s['pnl_pct']:+.1f},{s['hold_days']}"
                )
            recent = monitor.load_history(10)
            sig_txt = "\n".join(
                f"- {r['ts']} {r['symbol']} {r['kind_cn']}: {r['detail']}" for r in recent
            ) or "（无）"
            prompt = (
                "你是A股持仓顾问。当前持仓（CSV）：\n\n" + "\n".join(lines) +
                f"\n\n策略: 止损{trader.strategy.stop_loss_pct:.0%}/止盈"
                f"{trader.strategy.take_profit_pct:.0%}/移动止损"
                f"{trader.strategy.trailing_stop_pct or 0:.0%}\n\n近期盯盘信号:\n{sig_txt}\n\n"
                "输出：1) 组合整体评估（集中度/风格/风险）；2) 每只持仓建议"
                "（持有/减仓/清仓/补仓+理由）；3) 组合优化建议。markdown 格式，注明不构成投资建议。"
            )
            llm = create_llm_client(
                DC["llm_provider"], DC["quick_think_llm"], DC.get("backend_url"),
            ).get_llm()
            st.markdown(str(llm.invoke(prompt).content))
            st.caption("AI 生成，仅供参考，不构成投资建议。")
        except Exception as exc:
            st.error(f"AI 分析失败：{exc}")
