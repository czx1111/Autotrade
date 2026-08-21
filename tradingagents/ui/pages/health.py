"""页面：系统健康 —— 数据源探活 / 守护进程心跳 / 交易日历 / LLM 连通。"""

from __future__ import annotations

import json
from datetime import datetime

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.ui.common import RESULTS_DIR
from tradingagents.ui.health_check import run_all_probes
from tradingagents.ui.theme import MUTED, TEXT, UP, status_dot

_PROBE_TTL = 60  # 探活结果缓存秒数（避免每次 rerun 都打数据源）


@st.cache_data(ttl=_PROBE_TTL, show_spinner=False)
def _cached_probes() -> list[dict]:
    return [
        {
            "name": r.name, "kind": r.kind, "ok": r.ok,
            "latency_ms": r.latency_ms, "detail": r.detail,
        }
        for r in run_all_probes()
    ]


def _probe_row(p: dict) -> None:
    """单行探活结果：状态灯 + 名称 + 延迟 + 详情。"""
    dot = "green" if p["ok"] else "red"
    latency = p.get("latency_ms", 0)
    latency_html = (
        f"<span style='color:{MUTED};'>{latency}ms</span>" if p["ok"] else ""
    )
    detail_color = TEXT if p["ok"] else "#F25A5A"
    st.markdown(
        f"<div style='display:flex; align-items:center; gap:10px; "
        f"padding:8px 14px; margin-bottom:4px; font-size:13px;'>"
        f"{status_dot(dot)}"
        f"<span style='min-width:130px; font-weight:600; color:{TEXT};'>{p['name']}</span>"
        f"<span style='min-width:70px;'>{latency_html}</span>"
        f"<span style='color:{detail_color}; opacity:.85; flex:1;'>{p['detail']}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_datasource_tab() -> None:
    probes = _cached_probes()
    ok_count = sum(1 for p in probes if p["ok"])

    c1, c2, c3 = st.columns(3)
    c1.metric("可用数据源", f"{ok_count}/{len(probes)}")
    c2.metric("报价源", f"{sum(1 for p in probes if p['kind'] == 'quote' and p['ok'])}"
             f"/{sum(1 for p in probes if p['kind'] == 'quote')}")
    c3.metric("K线源", f"{sum(1 for p in probes if p['kind'] == 'kline' and p['ok'])}"
             f"/{sum(1 for p in probes if p['kind'] == 'kline')}")

    if ok_count < len(probes):
        failed = [p["name"] for p in probes if not p["ok"]]
        st.warning(f"不可用：{'、'.join(failed)} —— 故障转移已接管对应链路", icon="⚠️")

    st.subheader("数据源探活明细")
    st.caption(f"每 {_PROBE_TTL} 秒自动刷新；探针标的 600519，单源超时 5 秒")
    for p in probes:
        _probe_row(p)

    if st.button("🔄 立即重新探测", use_container_width=False):
        _cached_probes.clear()
        st.rerun()


def _render_llm_tab() -> None:
    st.subheader("LLM 端点连通性")
    st.caption("发送 1-token 请求验证密钥有效性（每次探测消耗极少量额度）")

    if st.button("🔌 测试 LLM 连通", type="primary"):
        from tradingagents.ui.health_check import probe_llm

        with st.spinner("正在请求 LLM 端点…"):
            r = probe_llm()
        if r.ok:
            st.success(f"{r.name} · {r.latency_ms}ms · {r.detail}")
        else:
            st.error(f"{r.name} 失败：{r.detail}")

    provider = DEFAULT_CONFIG.get("llm_provider", "?")
    deep = DEFAULT_CONFIG.get("deep_think_llm", "?")
    quick = DEFAULT_CONFIG.get("quick_think_llm", "?")
    c1, c2, c3 = st.columns(3)
    c1.metric("Provider", provider)
    c2.metric("深度模型", deep)
    c3.metric("快速模型", quick)


def _load_heartbeat() -> dict:
    import pathlib

    path = pathlib.Path(RESULTS_DIR) / "daemon_heartbeat.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _render_daemon_tab() -> None:
    st.subheader("守护进程心跳")
    st.caption("由 `python run_auto.py` 写入；显示每账号每阶段最近一次执行")

    hb = _load_heartbeat()
    if not hb:
        st.info(
            "尚未检测到守护进程心跳 —— 在终端运行 `python run_auto.py` 后，"
            "每个阶段（盘前/盘中/盯盘/盘后）执行完毕都会在这里留下记录",
            icon="📡",
        )
        return

    daemon = hb.get("_daemon", {})
    last_seen = daemon.get("last_seen", "?")

    # 守护进程在线判定：最近 10 分钟内有心跳
    try:
        seen_dt = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
        online = (datetime.now() - seen_dt).total_seconds() < 600
    except ValueError:
        online = False

    c1, c2 = st.columns(2)
    c1.metric(
        "守护进程",
        "在线" if online else "离线/未运行",
        delta=f"最后心跳 {last_seen}",
        delta_color="normal" if online else "inverse",
    )
    c2.metric("托管账号", len(daemon.get("accounts", [])))

    phases = {k: v for k, v in hb.items() if k != "_daemon"}
    if not phases:
        st.caption("心跳文件已创建，等待第一个阶段执行…")
        return

    st.markdown("")
    for key, rec in sorted(phases.items(), key=lambda kv: kv[1].get("ts", "")):
        status = rec.get("status", "?")
        dot = {"ok": "green", "error": "red"}.get(status, "amber")
        color = {"ok": UP, "error": "#F25A5A"}.get(status, MUTED)
        st.markdown(
            f"<div style='display:flex; align-items:center; gap:10px; "
            f"padding:8px 14px; margin-bottom:4px; font-size:13px; "
            f"background:rgba(255,255,255,.02); border-radius:10px;'>"
            f"{status_dot(dot)}"
            f"<span style='min-width:110px; font-weight:600; color:{TEXT};'>"
            f"{rec.get('account', '?')}</span>"
            f"<span style='min-width:110px; color:{MUTED};'>{rec.get('phase_cn', '')}</span>"
            f"<span style='min-width:150px;'>{rec.get('ts', '')}</span>"
            f"<span style='min-width:70px; color:{MUTED};'>{rec.get('duration_s', '')}s</span>"
            f"<span style='color:{color}; opacity:.85; flex:1;'>{rec.get('detail', '')}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )


def _render_calendar_tab() -> None:
    st.subheader("交易日历")
    st.caption("新浪交易日历表（本地缓存，每周刷新）；节假日自动跳过交易")

    try:
        from tradingagents.dataflows.trading_calendar import (
            _load_calendar,
            is_trading_day,
            next_trading_day,
        )

        days = _load_calendar()
        if not days:
            st.error("交易日历不可用（网络失败且无缓存）—— 节假日保护退化为仅过滤周末")
            return

        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        is_td = is_trading_day(today)
        nxt = next_trading_day()

        c1, c2, c3 = st.columns(3)
        c1.metric("今日", f"{'交易日' if is_td else '休市'}",
                  delta=today_str, delta_color="normal")
        c2.metric("日历覆盖", f"{len(days)} 天",
                  delta=f"{min(days)} ~ {max(days)}")
        c3.metric("下一交易日", nxt.strftime("%Y-%m-%d (%a)") if nxt else "-",
                  delta="" if is_td else "今日休市", delta_color="inverse")

        if not is_td:
            st.info("今日休市 —— 调度器与盘中/盯盘门控将自动跳过全部交易动作", icon="🏖️")
    except Exception as exc:  # noqa: BLE001
        st.error(f"日历状态读取失败：{exc}")


def render() -> None:
    st.header("🩺 系统健康")

    tab_src, tab_daemon, tab_cal, tab_llm = st.tabs(
        ["📡 数据源", "💓 守护进程", "📅 交易日历", "🧠 LLM"]
    )
    with tab_src:
        _render_datasource_tab()
    with tab_daemon:
        _render_daemon_tab()
    with tab_cal:
        _render_calendar_tab()
    with tab_llm:
        _render_llm_tab()
