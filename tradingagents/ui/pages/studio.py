"""页面二：智能体工作室 —— 7 分析师实时活动轨 + 流式思考过程 + 决策面板。"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.ui import data as ui_data
from tradingagents.ui import store as ui_store
from tradingagents.ui.common import (
    ANALYST_REGISTRY,
    DEFAULT_ANALYSTS,
    NODE_LABELS,
    RESULTS_DIR,
    account_names,
    get_trader,
    parse_rating_text,
    prev_close_of,
    render_rating,
)
from tradingagents.ui.theme import ACCENT, DOWN, MUTED, UP

_REPORT_KEYS = {key: info[2] for key, info in ANALYST_REGISTRY.items()}


def _analysts_and_mode() -> tuple[tuple[str, ...], str, dict]:
    """从 ui_settings 读取分析师组合与分析模式 → (analysts, mode_label, config_overrides)。"""
    settings = ui_store.load_ui_settings(RESULTS_DIR)
    chosen = settings.get("analysts") or DEFAULT_ANALYSTS
    mode = settings.get("mode", "deep")
    overrides = {}
    if mode == "deep":
        overrides = {"max_debate_rounds": 2, "max_risk_discuss_rounds": 2}
    else:
        overrides = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
    return tuple(chosen), mode, overrides


def _get_graph(analysts: tuple[str, ...], overrides: dict):
    if st.session_state.graph is None or st.session_state.graph_analysts != (analysts, tuple(overrides.items())):
        with st.spinner("初始化多智能体图（加载 LLM 客户端）…"):
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            cfg = DEFAULT_CONFIG.copy()
            cfg.update(overrides)
            st.session_state.graph = TradingAgentsGraph(
                selected_analysts=analysts, debug=False, config=cfg,
            )
            st.session_state.graph_analysts = (analysts, tuple(overrides.items()))
    return st.session_state.graph


def _agent_row(icon: str, name: str, status: str, note: str = "") -> str:
    cls = {"done": "done", "running": "running"}.get(status, "")
    emoji = {"done": "✅", "running": "💭", "pending": "⏳", "error": "⚠️"}.get(status, "⏳")
    return (
        f"<div class='ta-agent {cls}'><div style='font-size:1.3rem'>{icon}</div>"
        f"<div><div class='who'>{emoji} {name}</div>"
        f"<div class='sub'>{note or ('分析中…' if status == 'running' else '等待中')}</div></div></div>"
    )


def render() -> None:
    st.header("🤖 智能体工作室")
    analysts, mode, overrides = _analysts_and_mode()
    mode_cn = "深度模式（多空辩论×2 / 风控辩论×2）" if mode == "deep" else "快速模式（辩论×1）"

    # ── 顶部控制栏 ──
    with st.form("studio_run", border=False):
        col_t, col_m, col_go = st.columns([2, 1, 1])
        targets = col_t.text_input(
            "目标股票（逗号分隔多只）",
            value=st.session_state.get("analyze_symbol", ""),
            placeholder="如 600519, 000858",
        )
        col_m.caption(f"当前组合：{len(analysts)} 位分析师 · {mode_cn}")
        go = col_go.form_submit_button("🔄 开始新一轮分析", type="primary",
                                       use_container_width=True)
    st.caption("分析师组合与模式在「⚙️ 策略配置」页调整；深度模式更准但耗时与 token 约翻倍。")

    if not go:
        st.info("输入目标股票后点击「开始新一轮分析」——全程可实时观看各智能体的思考过程。")
        _render_last_decision()
        return

    import re as _re

    from tradingagents.dataflows.ashare_symbol_utils import normalize_ashare_symbol

    symbols = [normalize_ashare_symbol(s) or s.strip()
               for s in _re.split(r"[，,\s]+", targets) if s.strip()]
    if not symbols:
        st.warning("请输入至少一只股票代码")
        return

    graph = _get_graph(analysts, overrides)
    trade_date = datetime.now().strftime("%Y-%m-%d")

    for symbol in symbols:
        st.divider()
        quote = None
        try:
            quote = ui_data.get_quote(symbol) or {}
        except Exception:
            quote = {}
        if quote:
            pct = quote.get("pct") or 0
            color = UP if pct >= 0 else DOWN
            st.markdown(
                f"### {quote.get('name','')}（{symbol}） 现价 {quote.get('price','-')} "
                f"<span style='color:{color}'>"
                f"{pct:+.2f}%</span>",
                unsafe_allow_html=True,
            )

        # ── 布局：左侧活动轨 / 右侧决策面板 ──
        trail, decision = st.columns([7, 3])
        reports: dict[str, str] = {}
        final_state: dict = {}
        progress = trail.progress(0.0, text="多智能体分析启动…")
        trail_html = trail.container()

        try:
            # 复刻 TradingAgentsGraph._run_graph 的流式循环，实时渲染活动轨
            past_context = graph.memory_log.get_past_context(symbol)
            instrument_context = graph.resolve_instrument_context(symbol, "stock")
            init_state = graph.propagator.create_initial_state(
                symbol, trade_date, asset_type="stock",
                past_context=past_context, instrument_context=instrument_context,
            )
            status_lines: list[str] = []
            for chunk in graph.graph.stream(init_state):
                for node, delta in chunk.items():
                    if not isinstance(delta, dict):
                        continue
                    final_state.update(delta)
                    label, icon = NODE_LABELS.get(node, (node, "🤖"))
                    # 分析师产出报告 → 完成
                    done_note = ""
                    for rk in _REPORT_KEYS.values():
                        if delta.get(rk):
                            reports[rk] = delta[rk]
                    for rk, rv in reports.items():
                        if any(k in delta for k in _REPORT_KEYS.values()) and delta.get(rk):
                            done_note = "报告已生成"
                    msgs = delta.get("messages") or []
                    thinking = ""
                    if msgs:
                        content = str(getattr(msgs[-1], "content", "") or "")
                        if len(msgs[-1].tool_calls if hasattr(msgs[-1], 'tool_calls') else []):
                            thinking = "调用数据工具中…"
                        elif content:
                            thinking = f"输出 {len(content)} 字"
                    status = "done" if done_note else "running"
                    status_lines.append(_agent_row(icon, label, status, done_note or thinking))
                with trail_html:
                    st.markdown("\n".join(status_lines[-12:]), unsafe_allow_html=True)
                progress.progress(
                    min(_completion(final_state, analysts), 1.0),
                    text=f"{symbol} 分析进行中… 已完成 {_completion_count(final_state, analysts)}/{len(analysts) + 6} 步",
                )
        except Exception as exc:
            trail.error(f"分析流程异常：{exc}")
            return

        progress.progress(1.0, text=f"{symbol} 分析完成 ✅")

        # 活动轨下方：各分析师报告（可展开）
        with trail:
            tabs = st.tabs([f"{ANALYST_REGISTRY[k][1]} {ANALYST_REGISTRY[k][0]}"
                            for k in analysts])
            for tab, key in zip(tabs, analysts):
                rk = ANALYST_REGISTRY[key][2]
                with tab:
                    report = reports.get(rk) or final_state.get(rk) or "（未产出，检查数据源）"
                    st.markdown(report)
            debate = final_state.get("investment_debate_state") or {}
            b1, b2 = st.tabs(["🐂 多空辩论", "⚖️ 研究经理裁决"])
            with b1:
                st.markdown(debate.get("bull_history", "") or "（无）")
                st.markdown("---")
                st.markdown(debate.get("bear_history", "") or "（无）")
            with b2:
                st.markdown(debate.get("judge_decision", "") or "（无）")

        # ── 右侧决策核心面板 ──
        with decision:
            st.markdown("#### 🎯 决策核心")
            decision_text = final_state.get("final_trade_decision", "") or ""
            rating = parse_rating_text(decision_text)
            render_rating(rating)
            st.markdown(decision_text[:600] + ("…" if len(decision_text) > 600 else ""))
            st.markdown(
                f"<div class='sub' style='color:{MUTED};font-size:.8rem'>"
                f"交易员计划：{str(final_state.get('trader_investment_plan', ''))[:150]}…</div>",
                unsafe_allow_html=True,
            )

            st.markdown("---")
            accounts = account_names()
            account = st.selectbox("执行账号", accounts, key=f"exec_acct_{symbol}")
            amount = st.number_input("买入金额 (¥)", 1000.0, None, 10000.0, 1000.0,
                                     key=f"exec_amt_{symbol}")
            if st.button("📤 执行交易", type="primary", key=f"exec_btn_{symbol}",
                         use_container_width=True):
                if rating in ("buy", "overweight"):
                    try:
                        trader = get_trader(account)
                        price = float(quote.get("price") or 0)
                        if price <= 0:
                            st.error("无实时价格，无法下单")
                        else:
                            qty = int(amount / price // 100) * 100
                            rec = trader.submit_manual_trade(
                                symbol, "buy", qty, price,
                                name=str(quote.get("name", "")),
                                prev_close=prev_close_of(quote),
                            )
                            _show_trade_result(rec)
                    except Exception as exc:
                        st.error(f"下单失败：{exc}")
                elif rating in ("sell", "underweight"):
                    st.warning("卖出信号请到「📈 持仓与交易」页对具体持仓操作（自动核算可卖数量）")
                else:
                    st.info("当前评级为持有，无需交易")
            st.markdown(
                "<div class='ta-risk'>⚠️ 风险提示：AI 决策仅供参考，可能出错；"
                "实盘下单将经过全部风控校验，大额订单仍需人工批准。</div>",
                unsafe_allow_html=True,
            )

        # 保存报告树
        try:
            path = graph.save_reports(final_state, symbol)
            st.toast(f"{symbol} 完整报告已保存", icon="📄")
        except Exception as exc:
            st.warning(f"报告保存失败：{exc}")


def _completion_count(state: dict, analysts: tuple[str, ...]) -> int:
    """粗略完成步数：已产出报告的分析师数 + 后段节点是否出现。"""
    count = sum(1 for k in analysts if state.get(ANALYST_REGISTRY[k][2]))
    if state.get("investment_plan"):
        count += 1
    if state.get("trader_investment_plan"):
        count += 1
    risk_state = state.get("risk_debate_state")
    if isinstance(risk_state, dict) and risk_state.get("history"):
        count += 1
    if state.get("final_trade_decision"):
        count += 3
    return count


def _completion(state: dict, analysts: tuple[str, ...]) -> float:
    return _completion_count(state, analysts) / (len(analysts) + 6)


def _show_trade_result(rec: dict) -> None:
    outcome = rec.get("outcome")
    if outcome == "EXECUTED":
        st.success(f"✅ 已提交：{rec.get('action')} {rec.get('symbol')} × {rec.get('quantity')} 股")
        st.toast("交易已提交", icon="✅")
    elif outcome == "PENDING_APPROVAL":
        st.warning(f"⏳ 需审批（{rec.get('detail')}）ID：`{rec.get('approval_id')}`")
    else:
        st.error(f"❌ 未成交：{rec.get('detail')}")


def _render_last_decision() -> None:
    """未运行分析时展示最近一次报告的结论。"""
    reports = ui_store.list_analysis_reports(RESULTS_DIR, limit=1)
    if not reports:
        return
    path = reports[0] / "complete_report.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    st.divider()
    st.caption(f"最近一次分析：{reports[0].name}")
    with st.expander("查看上次完整报告"):
        st.markdown(text)
