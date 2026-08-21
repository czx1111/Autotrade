"""数据源健康探活：逐源实测可用性，供 UI 健康页展示。

独立于 Streamlit（纯函数），健康页用 ``st.cache_data`` 控制 TTL，
也可在终端单独运行自检::

    python -m tradingagents.ui.health_check

探针设计原则：每源一次最小请求（报价用 600519 / K 线取 5 天），
超时短（5 秒），返回结构化结果而非抛异常——健康检查本身不能成为
新的故障点。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_PROBE_SYMBOL = "600519"   # 贵州茅台：全天有报价、多源覆盖


@dataclass
class ProbeResult:
    """单个数据源的探活结果。"""

    name: str            # 展示名（腾讯行情 / 新浪行情 / 东财快照 …）
    kind: str            # quote / kline / calendar / news
    ok: bool
    latency_ms: int = 0
    detail: str = ""     # 成功时的摘要或失败原因


def _timed(fn) -> ProbeResult:
    """执行一次探针调用并计时；异常转为 ok=False 的结果。"""
    start = time.perf_counter()
    try:
        detail = fn()
        latency = int((time.perf_counter() - start) * 1000)
        return ProbeResult(name="", kind="", ok=True, latency_ms=latency, detail=detail)
    except Exception as exc:  # noqa: BLE001 — 探针必须吞掉一切异常
        latency = int((time.perf_counter() - start) * 1000)
        return ProbeResult(name="", kind="", ok=False, latency_ms=latency,
                           detail=f"{type(exc).__name__}: {exc}"[:160])


def probe_tencent_quote() -> ProbeResult:
    from tradingagents.dataflows.quote_sources import fetch_tencent_quote

    r = _timed(lambda: fetch_tencent_quote(_PROBE_SYMBOL))
    r.name, r.kind = "腾讯行情", "quote"
    if r.ok:
        r.detail = f"600519 现价 {r.detail.get('price')}"
    return r


def probe_sina_quote() -> ProbeResult:
    from tradingagents.dataflows.quote_sources import fetch_sina_quote

    r = _timed(lambda: fetch_sina_quote(_PROBE_SYMBOL))
    r.name, r.kind = "新浪行情", "quote"
    if r.ok:
        r.detail = f"600519 现价 {r.detail.get('price')}"
    return r


def probe_em_spot() -> ProbeResult:
    """东财全市场快照（akshare）——盘中下单定价主源。"""

    def _fetch():
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            raise RuntimeError("empty spot table")
        return f"全市场 {len(df)} 只"

    r = _timed(_fetch)
    r.name, r.kind = "东财全市场快照", "quote"
    if r.ok:
        r.detail = f"{r.detail} 只股票"
    return r


def probe_kline_sources() -> list[ProbeResult]:
    """K 线三源：东财(akshare) / 腾讯 / 新浪。"""
    from tradingagents.dataflows.quote_sources import (
        fetch_em_kline,
        fetch_sina_kline,
        fetch_tencent_kline,
    )

    results = []
    for name, fn in (
        ("东财日K", fetch_em_kline),
        ("腾讯日K", fetch_tencent_kline),
        ("新浪日K", fetch_sina_kline),
    ):
        def _fetch(f=fn):
            df = f(_PROBE_SYMBOL, days=5)
            if df is None or df.empty:
                raise RuntimeError("empty kline")
            return df.iloc[-1]["date"]

        r = _timed(_fetch)
        r.name, r.kind = name, "kline"
        if r.ok:
            r.detail = f"最新 bar {r.detail}"
        results.append(r)
    return results


def probe_trading_calendar() -> ProbeResult:
    """交易日历（新浪 trade-date 表，含缓存命中路径）。"""

    def _fetch():
        from tradingagents.dataflows.trading_calendar import _load_calendar

        days = _load_calendar()
        if not days:
            raise RuntimeError("calendar empty")
        return f"{len(days)} 个交易日（{min(days)}~{max(days)}）"

    r = _timed(_fetch)
    r.name, r.kind = "交易日历(新浪)", "calendar"
    return r


def probe_llm() -> ProbeResult:
    """LLM 端点探活：发送一个 1-token 请求验证密钥有效。

    只在健康页手动触发（成本考虑），不在常规探测集里。
    """
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients import create_llm_client

    def _fetch():
        provider = DEFAULT_CONFIG.get("llm_provider", "openai")
        model = DEFAULT_CONFIG.get("quick_think_llm", "gpt-4o-mini")
        llm = create_llm_client(provider, model).get_llm()
        resp = llm.invoke("回复一个字：好")
        return f"{provider}/{model} → {str(resp.content)[:20]!r}"

    r = _timed(_fetch)
    r.name, r.kind = f"LLM({DEFAULT_CONFIG.get('llm_provider', '?')})", "llm"
    return r


def run_all_probes(include_llm: bool = False) -> list[ProbeResult]:
    """执行全部快速探针（quote/kline/calendar），返回结果列表。"""
    results = [
        probe_tencent_quote(),
        probe_sina_quote(),
        probe_em_spot(),
    ]
    results.extend(probe_kline_sources())
    results.append(probe_trading_calendar())
    if include_llm:
        results.append(probe_llm())
    return results


# ── 终端自检入口 ──

if __name__ == "__main__":
    print("数据源健康自检（含 LLM）…")
    for r in run_all_probes(include_llm=True):
        mark = "✅" if r.ok else "❌"
        print(f"{mark} {r.name:<14} {r.latency_ms:>5}ms  {r.detail}")
