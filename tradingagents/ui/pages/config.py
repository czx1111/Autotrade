"""页面四：策略配置 —— 分析师组合 / 风控阈值 / 盯盘策略 / 券商连接 / LLM。"""

from __future__ import annotations

import streamlit as st

from tradingagents.broker.path_helper import (
    auto_detect_qmt_install,
    auto_detect_ths_install,
    resolve_qmt_userdata,
    resolve_ths_xiadan,
)
from tradingagents.ui import store as ui_store
from tradingagents.ui.common import (
    ANALYST_REGISTRY,
    DEFAULT_ANALYSTS,
    RESULTS_DIR,
    account_names,
    get_trader,
    reset_trader,
)

_MODES = {"quick": "快速模式（辩论×1，约 5-8 分钟/只）", "deep": "深度模式（辩论×2，约 10-15 分钟/只）"}


def render() -> None:
    st.header("⚙️ 策略配置")
    st.caption("所有配置持久化到 accounts.json / ui_settings.json，自动交易守护进程共用。")

    tab_a, tab_r, tab_b, tab_l = st.tabs(
        ["🤖 Agent 参数", "🛡️ 风控与策略", "🏦 券商账号", "🧠 LLM 模型"]
    )

    with tab_a:
        _agent_settings()
    with tab_r:
        _risk_settings()
    with tab_b:
        _broker_settings()
    with tab_l:
        _llm_settings()


def _agent_settings() -> None:
    settings = ui_store.load_ui_settings(RESULTS_DIR)
    chosen = settings.get("analysts") or DEFAULT_ANALYSTS
    mode = settings.get("mode", "deep")

    st.subheader("分析师团队（7 位）")
    st.caption("越多越全面，token 与耗时线性增加；深度模式把多空/风控辩论轮数翻倍。")
    labels = [f"{info[1]} {info[0]}" for info in ANALYST_REGISTRY.values()]
    keys = list(ANALYST_REGISTRY.keys())
    defaults = [labels[keys.index(c)] for c in chosen if c in keys]
    picked = st.multiselect("参与的分析师", labels, default=defaults)
    mode_label = st.radio("分析模式", list(_MODES.values()), index=list(_MODES).index(mode))
    if st.button("💾 保存 Agent 配置", type="primary"):
        new_keys = [keys[labels.index(p)] for p in picked] or DEFAULT_ANALYSTS
        ui_store.save_ui_settings(RESULTS_DIR, {
            **settings, "analysts": new_keys,
            "mode": "quick" if _MODES["quick"] == mode_label else "deep",
        })
        st.session_state.graph = None      # 重建分析图
        st.toast("已保存，工作室将使用新组合", icon="✅")
        st.rerun()


def _risk_settings() -> None:
    st.subheader("风控阈值与盯盘策略（按账号）")
    accounts = ui_store.load_accounts()
    for account in accounts:
        name = account.get("name", "?")
        with st.expander(f"账号：{name}"):
            risk: dict = dict(account.get("risk") or {})
            strategy: dict = dict(account.get("strategy") or {})
            with st.form(f"risk_{name}"):
                r1, r2 = st.columns(2)
                max_pos = r1.slider("单只股票最大仓位 %", 5, 100,
                                    int(float(risk.get("max_single_position_pct", 0.20)) * 100), 5)
                daily_loss = r2.slider("单日最大亏损 %", 1, 20,
                                       int(float(risk.get("max_daily_loss_pct", 0.03)) * 100), 1)
                max_orders = r1.number_input("单日最大下单笔数", 1, 200,
                                             int(risk.get("max_orders_per_day", 10)), 1)
                sector_cap = r2.slider("单行业最大仓位 %", 10, 100,
                                       int(float(risk.get("max_sector_concentration_pct", 0.40)) * 100), 5)
                st.markdown("---")
                s1, s2, s3, s4 = st.columns(4)
                stop = s1.number_input("止损 %", 1.0, 50.0,
                                       float(strategy.get("stop_loss_pct", 0.07)) * 100, 0.5)
                take = s2.number_input("止盈 %", 2.0, 200.0,
                                       float(strategy.get("take_profit_pct", 0.15)) * 100, 1.0)
                trail = s3.number_input("移动止损 % (0=关)", 0.0, 50.0,
                                        float(strategy.get("trailing_stop_pct") or 0) * 100, 0.5)
                max_hold = s4.number_input("最大持有天数 (0=不限)", 0, 365,
                                           int(strategy.get("max_hold_days") or 0), 1)
                ma_exit = st.checkbox("均线死叉卖出（MA5↓MA10，短线）",
                                      bool(strategy.get("ma_cross_exit", False)))
                min_order = s1.number_input("最小下单金额 (¥)", 0.0, None,
                                            float(account.get("min_order_value", 5000)), 500.0)
                large = s2.number_input("大额审批线 (¥)", 1000.0, None,
                                        float(account.get("large_order_confirm_value", 50000)), 5000.0)
                if st.form_submit_button("💾 保存", type="primary"):
                    account["risk"] = {
                        "max_single_position_pct": max_pos / 100,
                        "max_daily_loss_pct": daily_loss / 100,
                        "max_orders_per_day": int(max_orders),
                        "max_sector_concentration_pct": sector_cap / 100,
                    }
                    account["strategy"] = {
                        "stop_loss_pct": stop / 100,
                        "take_profit_pct": take / 100,
                        "trailing_stop_pct": trail / 100 if trail > 0 else None,
                        "max_hold_days": max_hold if max_hold > 0 else None,
                        "ma_cross_exit": ma_exit,
                    }
                    account["min_order_value"] = float(min_order)
                    account["large_order_confirm_value"] = float(large)
                    ui_store.save_accounts(accounts)
                    reset_trader(name)
                    st.toast(f"{name} 风控已保存并即时生效", icon="✅")
                    st.rerun()


def _broker_settings() -> None:
    st.subheader("券商账号")
    st.caption("easytrader=同花顺客户端/银河网页（无需量化权限）；qmt=miniQMT；paper=模拟盘。")
    accounts = ui_store.load_accounts()
    for account in accounts:
        name = account.get("name", "?")
        bs: dict = dict(account.get("broker_settings") or {})
        with st.expander(f"账号：{name}（当前 {bs.get('broker', 'paper')}）"):
            # ── 自动检测区域（form 外部，可用 st.button）──
            _render_path_detector(bs, name)

            # ── 配置表单 ──
            with st.form(f"broker_{name}"):
                b = st.selectbox("通道", ["paper", "easytrader", "qmt"],
                                 index=["paper", "easytrader", "qmt"].index(bs.get("broker", "paper")))
                new_bs: dict = {}
                if b == "easytrader":
                    client = st.selectbox(
                        "客户端类型",
                        ["universal", "yh"],
                        index=["universal", "yh"].index(
                            bs.get("easytrader_client", "universal")
                            if bs.get("easytrader_client") in ("universal", "yh")
                            else "universal"
                        ),
                        format_func=lambda x: (
                            "同花顺通用客户端 (universal)"
                            if x == "universal"
                            else "银河证券网页交易 (yh)"
                        ),
                    )
                    new_bs["easytrader_client"] = client
                    if client == "universal":
                        # 从 session_state 读取检测结果（由 form 外的检测按钮写入）
                        ths_key = f"ths_detected_{name}"
                        default_path = (
                            st.session_state.get(ths_key)
                            or bs.get("easytrader_client_path", "")
                        )
                        ths_dir = st.text_input(
                            "同花顺安装目录",
                            value=_strip_xiadan_exe(default_path),
                            key=f"ths_dir_{name}",
                            placeholder="如 D:\\ths\\同花顺",
                        )
                        _show_ths_validation(ths_dir)
                        new_bs["easytrader_client_path"] = ths_dir
                    else:
                        c1, c2 = st.columns(2)
                        user = c1.text_input(
                            "资金账号", bs.get("easytrader_user", ""),
                            key=f"yh_user_{name}",
                        )
                        pwd = c2.text_input(
                            "交易密码", bs.get("easytrader_password", ""),
                            type="password", key=f"yh_pwd_{name}",
                        )
                        new_bs["easytrader_user"] = user
                        new_bs["easytrader_password"] = pwd
                    new_bs["broker"] = b
                elif b == "qmt":
                    qmt_key = f"qmt_detected_{name}"
                    default_path = (
                        st.session_state.get(qmt_key)
                        or bs.get("qmt_mini_path", "")
                    )
                    c_path, c_acct = st.columns([3, 2])
                    qmt_dir = c_path.text_input(
                        "miniQMT 安装目录",
                        value=default_path,
                        key=f"qmt_dir_{name}",
                        placeholder="如 D:\\国金证券QMT交易端",
                    )
                    acct_id = c_acct.text_input(
                        "资金账号", bs.get("qmt_account_id", ""),
                        key=f"qmt_acct_{name}",
                    )
                    _show_qmt_validation(qmt_dir)
                    new_bs = {
                        "broker": b,
                        "qmt_mini_path": qmt_dir,
                        "qmt_account_id": acct_id,
                    }
                else:
                    cap = st.number_input(
                        "模拟盘初始资金 (¥)", 10_000.0, None,
                        float(bs.get("paper_initial_capital", 1_000_000)),
                        10_000.0,
                    )
                    new_bs = {"broker": b, "paper_initial_capital": cap}
                if st.form_submit_button("💾 保存券商配置", type="primary"):
                    account["broker_settings"] = new_bs
                    ui_store.save_accounts(accounts)
                    reset_trader(name)
                    st.session_state.brokers.pop(name, None)
                    st.toast("已保存", icon="✅")
                    st.rerun()

            if st.button("⚡ 测试连接", key=f"test_{name}"):
                try:
                    st.session_state.brokers.pop(name, None)
                    trader = get_trader(name)
                    info = trader.broker.get_account()
                    st.success(f"✅ 连接成功：总资产 ¥{info.total_asset:,.2f}")
                except Exception as exc:
                    st.error(f"❌ 连接失败：{exc}")
                    st.info("easytrader 需先登录同花顺客户端；qmt 需 miniQMT 运行中。")


def _render_path_detector(bs: dict, account_name: str) -> None:
    """在 form 外部渲染 THS / QMT 路径自动检测按钮（form 内不允许 st.button）。"""
    broker = bs.get("broker", "paper")
    if broker == "easytrader" and bs.get("easytrader_client", "universal") == "universal":
        col_path, col_btn = st.columns([4, 1])
        with col_btn:
            if st.button("🔍 自动检测同花顺", key=f"ths_detect_{account_name}",
                         use_container_width=True):
                found = auto_detect_ths_install()
                if found:
                    st.session_state[f"ths_detected_{account_name}"] = found[0]
                    st.toast(f"检测到：{found[0]}", icon="🔍")
                    st.rerun()
                else:
                    st.warning("未找到同花顺安装，请手动输入安装目录")
    elif broker == "qmt":
        col_path, col_btn = st.columns([4, 1])
        with col_btn:
            if st.button("🔍 自动检测QMT", key=f"qmt_detect_{account_name}",
                         use_container_width=True):
                found = auto_detect_qmt_install()
                if found:
                    st.session_state[f"qmt_detected_{account_name}"] = found[0]
                    st.toast(f"检测到：{found[0]}", icon="🔍")
                    st.rerun()
                else:
                    st.warning("未找到 miniQMT 安装，请手动输入安装目录")


def _strip_xiadan_exe(path: str) -> str:
    """如果路径是 xiadan.exe 完整路径，提取目录部分。"""
    if path and path.lower().endswith("xiadan.exe"):
        sep = "\\" if "\\" in path else "/"
        return path.rsplit(sep, 1)[0]
    return path


def _show_ths_validation(install_dir: str) -> None:
    """在 form 内显示 THS 路径验证结果（只读信息，不用 button）。"""
    if install_dir:
        exe = resolve_ths_xiadan(install_dir)
        if exe:
            import os
            if os.path.isfile(exe):
                st.success(f"✅ 找到 xiadan.exe：{exe}")
            else:
                st.warning(f"⚠️ 目录下未找到 xiadan.exe（预期：{exe}）")
        else:
            st.warning("⚠️ 请输入同花顺安装目录")
    st.caption("💡 也可直接填写 xiadan.exe 的完整路径，如 C:\\同花顺软件\\同花顺\\xiadan.exe")


def _show_qmt_validation(install_dir: str) -> None:
    """在 form 内显示 QMT 路径验证结果（只读信息，不用 button）。"""
    if install_dir:
        ud = resolve_qmt_userdata(install_dir)
        if ud:
            import os
            if os.path.isdir(ud):
                st.success(f"✅ 找到 userdata_mini：{ud}")
            else:
                st.warning(f"⚠️ 目录下未找到 userdata_mini（预期：{ud}）")
        else:
            st.warning("⚠️ 请输入 miniQMT 安装目录")
    st.caption("💡 也可直接填写 userdata_mini 的完整路径，如 D:\\国金证券QMT交易端\\userdata_mini")


def _llm_settings() -> None:
    from tradingagents.default_config import DEFAULT_CONFIG as DC

    st.subheader("LLM 模型")
    st.caption("模型与密钥由 `.env` 环境变量管理（改后重启 UI 生效），此处查看当前生效配置。")
    rows = [
        {"配置项": "提供商 (llm_provider)", "当前值": str(DC["llm_provider"])},
        {"配置项": "深度思考模型 (deep_think_llm)", "当前值": str(DC["deep_think_llm"])},
        {"配置项": "快速思考模型 (quick_think_llm)", "当前值": str(DC["quick_think_llm"])},
        {"配置项": "自定义端点 (backend_url)", "当前值": str(DC.get("backend_url") or "官方默认")},
        {"配置项": "输出语言", "当前值": str(DC["output_language"])},
    ]
    st.table(rows)
    st.markdown(
        "切换方式：编辑项目根目录 `.env`，例如\n"
        "```\n"
        "TRADINGAGENTS_LLM_PROVIDER=openai\n"
        "TRADINGAGENTS_DEEP_THINK_LLM=gpt-5.5\n"
        "TRADINGAGENTS_QUICK_THINK_LLM=gpt-5.4-mini\n"
        "# DeepSeek/Qwen 等 OpenAI 兼容端点：\n"
        "TRADINGAGENTS_LLM_BACKEND_URL=https://api.deepseek.com/v1\n"
        "```"
    )
