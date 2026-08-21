"""🔍 发现选股页 —— 指数/板块/全市场筛选 + 一键选股 + 自选股管理（从旧版迁移）。"""

from __future__ import annotations

import streamlit as st

from tradingagents.dataflows.ashare_symbol_utils import normalize_ashare_symbol
from tradingagents.ui import data as ui_data
from tradingagents.ui import store as ui_store
from tradingagents.ui.charts import build_sector_bar
from tradingagents.ui.common import _quick_llm, goto
from tradingagents.ui.theme import DOWN, UP

_INDEX_NAMES = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指",
                "000300": "沪深300", "000905": "中证500", "000688": "科创50"}


def render() -> None:
    st.header("🔍 发现选股")

    try:
        indices = ui_data.load_indices()
    except Exception as exc:
        st.warning(f"指数数据获取失败：{exc}")
        indices = None

    if indices is not None and not indices.empty:
        cols = st.columns(len(indices))
        for col, (_, row) in zip(cols, indices.iterrows()):
            pct = row.get("pct") or 0
            col.metric(
                _INDEX_NAMES.get(str(row["code"]), str(row.get("name", ""))),
                f"{row.get('price', '-')}", f"{pct:+.2f}%",
            )

    tab_sector, tab_market, tab_one, tab_watch = st.tabs(
        ["行业板块", "全市场筛选", "⚡ 一键选股", "⭐ 自选股"]
    )

    with tab_sector:
        try:
            boards = ui_data.load_sector_boards()
        except Exception as exc:
            st.warning(f"板块数据获取失败：{exc}")
            boards = None
        if boards is not None and not boards.empty:
            st.plotly_chart(build_sector_bar(boards), use_container_width=True)
            with st.expander("板块明细"):
                st.dataframe(boards.head(30), use_container_width=True, hide_index=True)
        else:
            st.info("板块数据暂不可用，请稍后重试或点击侧边栏「刷新行情缓存」")

    with tab_market:
        _market_filter()

    with tab_one:
        _one_click()

    with tab_watch:
        _watchlist()


def _market_filter() -> None:
    with st.form("screener_filter"):
        fc1, fc2, fc3, fc4 = st.columns(4)
        pct_min = fc1.number_input("涨跌幅下限 %", value=-10.0, step=0.5)
        pct_max = fc2.number_input("涨跌幅上限 %", value=10.0, step=0.5)
        price_min = fc3.number_input("价格下限 ¥", value=0.0, step=1.0)
        price_max = fc4.number_input("价格上限 ¥", value=10000.0, step=100.0)
        exclude_st = st.checkbox("剔除 ST / 退市股", value=True)
        st.form_submit_button("应用筛选", type="primary")

    try:
        spot = ui_data.load_market_spot()
    except Exception as exc:
        st.error(f"行情数据获取失败：{exc}")
        return

    if spot.empty:
        st.warning("行情数据暂不可用（网络异常或非交易时段），请稍后重试或点击侧边栏「刷新行情缓存」")
        return

    query = st.text_input("🔍 按代码或名称搜索", "")
    base = ui_data.search_stocks(spot, query) if query else spot
    filtered = ui_data.filter_market(
        base, pct_min=pct_min, pct_max=pct_max,
        price_min=price_min or None, price_max=price_max if price_max < 10000 else None,
        exclude_st=exclude_st,
    )
    # 根据可用字段动态生成排序选项
    avail_cols = [c for c in ["pct", "amount", "turnover", "pe", "pct60d", "pctYtd", "mktcap"] if c in filtered.columns]
    sort_col = st.selectbox(
        "排序依据", avail_cols,
        format_func=lambda c: {"pct": "涨跌幅", "amount": "成交额", "turnover": "换手率",
                               "pe": "市盈率", "pct60d": "60日涨跌幅",
                               "pctYtd": "年初至今", "mktcap": "总市值"}.get(c, c),
    )
    top_n = st.select_slider("显示条数", options=[20, 50, 100, 200], value=50)
    view = filtered.dropna(subset=[sort_col]).sort_values(
        sort_col, ascending=st.toggle("升序", value=False)
    ).head(top_n)

    display = view.copy()
    if "amount" in display:
        display["amount"] = display["amount"].map(ui_data.fmt_amount)
    if "mktcap" in display:
        display["mktcap"] = display["mktcap"].map(ui_data.fmt_amount)
    ren = {"code": "代码", "name": "名称", "price": "现价", "pct": "涨跌幅%",
           "amount": "成交额", "turnover": "换手%", "pe": "市盈率", "pb": "市净率",
           "mktcap": "总市值", "pct60d": "60日%", "pctYtd": "年初%"}
    display = display.rename(columns={k: v for k, v in ren.items() if k in display.columns})

    event = st.dataframe(display, use_container_width=True, hide_index=True,
                         on_select="rerun", selection_mode="single-row", key="market_table")
    rows = getattr(getattr(event, "selection", None), "rows", []) or []
    if rows:
        picked = view.iloc[rows[0]]
        code = str(picked["code"])
        st.success(f"已选中：{picked['name']} ({code}) 现价 {picked['price']}")
        c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
        if c1.button("📐 K线", key="pick_chart", use_container_width=True):
            goto("📐 K线看盘", chart_symbol=code)
        if c2.button("🤖 AI 分析", key="pick_ai", use_container_width=True):
            goto("🤖 智能体工作室", analyze_symbol=code)
        if c3.button("⭐ 加自选", key="pick_watch", use_container_width=True):
            goto("🔍 发现选股", add_to_watchlist=code)
        if c4.button("📈 交易", key="pick_trade", use_container_width=True):
            goto("📈 持仓与交易")


def _one_click() -> None:
    from tradingagents.ui.screener import build_ai_review_prompt, factor_screen

    st.markdown(
        "多因子规则筛选（动量 40% + 流动性 30% + 换手适中 30%）：60日动量为正、"
        "成交额≥1亿、换手 1%~15%、PE 0~80、当日红盘未过热、剔除 ST。\n\n"
        "⚠️ 当前数据源为新浪（东财不可用），部分因子如 60日动量可能不可用，"
        "系统会自动以当日涨跌幅替代。"
    )
    try:
        spot = ui_data.load_market_spot()
    except Exception as exc:
        st.error(f"行情数据获取失败：{exc}")
        return

    if spot.empty:
        st.warning("行情数据暂不可用（网络异常或非交易时段），请稍后重试或点击侧边栏「刷新行情缓存」")
        return

    top_n = st.select_slider("候选数量", options=[10, 20, 30, 50], value=20)
    if not st.button("⚡ 立即选股", type="primary"):
        st.caption("点击「立即选股」开始筛选")
        return

    with st.spinner("多因子筛选中…"):
        picked_df = factor_screen(spot, top_n=top_n)
    if picked_df.empty:
        st.warning("当前无符合条件的股票")
        return

    display = picked_df.copy()
    if "amount" in display:
        display["amount"] = display["amount"].map(ui_data.fmt_amount)
    ren = {"code": "代码", "name": "名称", "price": "现价", "pct": "涨跌%",
           "amount": "成交额", "turnover": "换手%", "pe": "PE",
           "pct60d": "60日%", "score": "综合分"}
    st.dataframe(display.rename(columns={k: v for k, v in ren.items() if k in display.columns}),
                 use_container_width=True, hide_index=True)

    st.divider()
    if st.button("🤖 AI 复核候选股"):
        with st.spinner("AI 复核中…"):
            try:
                prompt = build_ai_review_prompt(picked_df, pick_n=5)
                st.markdown(str(_quick_llm().invoke(prompt).content))
                st.caption("AI 生成，仅供参考，不构成投资建议。")
            except Exception as exc:
                st.error(f"AI 复核失败：{exc}")

    st.divider()
    event = st.dataframe(
        picked_df[["code", "name", "price", "pct", "score"]],
        use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row", key="pick_table",
    )
    rows = getattr(getattr(event, "selection", None), "rows", []) or []
    if rows:
        row = picked_df.iloc[rows[0]]
        code = str(row["code"])
        st.success(f"已选中：{row['name']} ({code}) 综合分 {row['score']}")
        b1, b2, b3 = st.columns([1, 1, 1])
        if b1.button("📐 K线", key="oc_chart", use_container_width=True):
            goto("📐 K线看盘", chart_symbol=code)
        if b2.button("🤖 深度分析", key="oc_ai", use_container_width=True):
            goto("🤖 智能体工作室", analyze_symbol=code)
        if b3.button("⭐ 加自选", key="oc_watch", use_container_width=True):
            goto("🔍 发现选股", add_to_watchlist=code)


def _watchlist() -> None:
    st.caption("自选股 = 自动交易股票池，与 run_auto.py 共用 accounts.json；盘前 LLM 从中筛选当日重点。")
    accounts = ui_store.load_accounts()
    account_names = [a.get("name", "?") for a in accounts]
    account_name = st.selectbox("账号", account_names, key="wl_acct")
    account = next(a for a in accounts if a.get("name") == account_name)
    watchlist: list[str] = list(account.get("watchlist") or [])

    pending = st.session_state.pop("add_to_watchlist", None)
    if pending:
        pending = normalize_ashare_symbol(pending) or pending
        if pending in watchlist:
            st.info(f"{pending} 已在自选中")
        else:
            watchlist.append(pending)
            accounts = ui_store.set_watchlist(accounts, account_name, watchlist)
            ui_store.save_accounts(accounts)
            st.toast(f"{pending} 已加入自选", icon="⭐")
            st.rerun()

    col_edit, col_view = st.columns([1, 2])
    with col_edit:
        with st.form("watchlist_edit"):
            text = st.text_area("股票代码（每行一个）", value="\n".join(watchlist), height=260)
            if st.form_submit_button("💾 保存自选股", type="primary"):
                symbols = [normalize_ashare_symbol(s) or s.strip()
                           for s in text.splitlines() if s.strip()]
                accounts = ui_store.set_watchlist(accounts, account_name, symbols)
                ui_store.save_accounts(accounts)
                st.success(f"已保存 {len(symbols)} 只自选股")
                st.rerun()

    with col_view:
        if not watchlist:
            st.info("自选股为空，可在上方直接添加代码")
            return
        try:
            spot = ui_data.load_market_spot()
        except Exception as exc:
            st.warning(f"行情获取失败：{exc}")
            return
        if spot.empty:
            st.warning("行情数据暂不可用，请稍后重试或点击侧边栏「刷新行情缓存」")
            return
        rows = spot[spot["code"].isin(watchlist)]
        if rows.empty:
            st.info("自选股暂无行情")
            return
        for _, row in rows.iterrows():
            pct = row.get("pct") or 0
            color = UP if pct >= 0 else DOWN
            c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
            c1.markdown(f"**{row['name']}** ({row['code']})")
            c2.markdown(f"**{row['price']}**")
            c3.markdown(f":{color}[{pct:+.2f}%]")
            if c4.button("🤖分析", key=f"wl_{row['code']}", use_container_width=True):
                goto("🤖 智能体工作室", analyze_symbol=str(row["code"]))
