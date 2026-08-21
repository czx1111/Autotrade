"""一键选股：多因子规则筛选 + 打分排序（纯函数，无网络）。

因子全部来自全市场快照（东财 spot，一次拉取）：

- 动量   60日涨跌幅为正且不过热
- 量能   成交额下限（流动性门槛）+ 换手率适中（1%~15%，避开仙股与妖股）
- 估值   PE 0~80（剔除亏损与极端高估）
- 价格   3~300 元（剔除低价股与高价茅台类，可调）

综合得分 = 动量排名(40%) + 流动性排名(30%) + 换手适中度排名(30%)，
得分越高越靠前。输出只做「候选池」，最终买卖仍由 AI 分析 + 风控决定。
"""

from __future__ import annotations

import pandas as pd

DEFAULT_BOUNDS = {
    "pct": (0.2, 7.0),          # 当日涨幅：红盘但未涨停（未过热）
    "pct60d": (0.0, 60.0),      # 60日动量为正、未翻倍
    "turnover": (1.0, 15.0),    # 换手率适中
    "price": (3.0, 300.0),
    "amount_min": 1e8,          # 成交额 ≥ 1 亿（流动性）
    "pe": (0.0, 80.0),
}


def factor_screen(
    spot: pd.DataFrame,
    top_n: int = 20,
    *,
    bounds: dict | None = None,
) -> pd.DataFrame:
    """多因子筛选 + 打分。返回带 ``score`` 列的 DataFrame（降序）。

    ``spot`` 需含 code/name/price/pct/amount/turnover/pe/pct60d（来自
    ``ui.data.load_market_spot``）。无可选列时对应因子自动放宽。
    """
    b = dict(DEFAULT_BOUNDS)
    if bounds:
        b.update(bounds)

    out = spot.copy()
    name = out["name"].astype(str)
    out = out[~name.str.contains("ST", na=False) & ~name.str.contains("退", na=False)]
    out = out[out["price"].between(*b["price"])]
    out = out[out["pct"].between(*b["pct"])]
    if "pct60d" in out.columns:
        out = out[out["pct60d"].between(*b["pct60d"])]
    if "turnover" in out.columns:
        out = out[out["turnover"].between(*b["turnover"])]
    if "pe" in out.columns:
        out = out[out["pe"].between(*b["pe"])]
    if "amount" in out.columns:
        out = out[out["amount"] >= b["amount_min"]]
    if out.empty:
        return out

    # 打分：各因子排名归一化后加权（rank pct 越大越好）
    momentum = out.get("pct60d", out["pct"]).rank(pct=True)
    liquidity = out["amount"].rank(pct=True) if "amount" in out else 0.5
    # 换手适中度：越接近 5% 越好
    if "turnover" in out:
        fitness = (-(out["turnover"] - 5.0).abs()).rank(pct=True)
    else:
        fitness = 0.5
    out = out.assign(
        score=(0.4 * momentum + 0.3 * liquidity + 0.3 * fitness).round(4)
    )
    return out.sort_values("score", ascending=False).head(top_n).reset_index(drop=True)


def build_ai_review_prompt(rows: pd.DataFrame, pick_n: int = 5) -> str:
    """把筛选结果组装成 LLM 复核提示词（候选 → 精选 + 理由）。"""
    cols = ["code", "name", "price", "pct", "turnover", "amount", "pe", "pct60d", "score"]
    table = rows[[c for c in cols if c in rows.columns]].to_csv(index=False)
    return (
        "你是A股投资顾问。以下是通过多因子初筛的候选股票（CSV 数据）：\n\n"
        f"{table}\n\n"
        f"请从中精选不超过 {pick_n} 只最值得关注的股票，输出 markdown 表格"
        "（列：代码/名称/推荐理由/风险提示），表格后用 2-3 句话总结今日选股思路。"
        "理由需结合动量、流动性、估值具体数据，不要泛泛而谈。"
    )
