"""周报/月报自动复盘：聚合每日明细 JSON → 结构化复盘报告。

数据源是盘后复盘写入的 ``results_dir/auto/<account>_<YYYYMMDD>.json``
（净值、日盈亏、持仓、成交明细、审批遗留）。报告写到
``results_dir/auto/review/`` 子目录，不混入日报列表。

生成时机：

- 周报：周五盘后（``run_post_market`` 自动触发），覆盖当周；也可
  ``python run_auto.py --review weekly`` 手动生成。
- 月报：当月最后一个交易日盘后自动触发；``--review monthly`` 手动生成。

指标面向「渐进式放手」的实盘数据积累：日胜率、盈亏比、成交活跃度、
审批遗留——两三个月后这些序列就是提炼个人策略的底料。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_KIND_CN = {"weekly": "周报", "monthly": "月报"}


def load_daily_summaries(
    results_dir: str | Path,
    account: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """读取某账号区间内的每日明细 JSON（旧→新）。

    日期比较用 ISO 字符串（``YYYY-MM-DD`` 字典序即时间序）。
    """
    folder = Path(results_dir) / "auto"
    if not folder.exists():
        return []
    pattern = re.compile(rf"^{re.escape(account)}_(\d{{8}})\.json$")
    out: list[dict] = []
    for path in folder.iterdir():
        m = pattern.match(path.name)
        if not m:
            continue
        day = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            logger.warning("daily summary unreadable, skipped: %s", path.name)
    out.sort(key=lambda s: s.get("date", ""))
    return out


def build_review(dailies: list[dict], account: str, kind: str) -> dict:
    """聚合每日明细 → 复盘指标（纯计算，不写盘）。"""
    days = [d for d in dailies if d.get("day_pnl") is not None]
    pnl_list = [float(d["day_pnl"]) for d in days]
    win_days = sum(1 for p in pnl_list if p > 0)
    loss_days = sum(1 for p in pnl_list if p < 0)
    total_win = sum(p for p in pnl_list if p > 0)
    total_loss = abs(sum(p for p in pnl_list if p < 0))

    start_equity = days[0]["total_asset"] - days[0]["day_pnl"] if days else 0.0
    end_equity = days[-1]["total_asset"] if days else 0.0
    period_pnl = sum(pnl_list)
    period_return = period_pnl / start_equity if start_equity > 0 else 0.0

    trades = [t for d in dailies for t in d.get("trades_detail", [])]
    buys = [t for t in trades if t.get("side") == "buy"]
    sells = [t for t in trades if t.get("side") == "sell"]
    buy_amount = sum(t["quantity"] * t["price"] for t in buys)
    sell_amount = sum(t["quantity"] * t["price"] for t in sells)
    biggest_buy = max(
        buys, key=lambda t: t["quantity"] * t["price"], default=None,
    )

    last = dailies[-1] if dailies else {}
    return {
        "account": account,
        "kind": kind,
        "kind_cn": _KIND_CN.get(kind, kind),
        "start_date": dailies[0].get("date") if dailies else "",
        "end_date": dailies[-1].get("date") if dailies else "",
        "trading_days": len(days),
        "start_equity": start_equity,
        "end_equity": end_equity,
        "period_pnl": period_pnl,
        "period_return": period_return,
        "win_days": win_days,
        "loss_days": loss_days,
        "day_win_rate": win_days / len(days) if days else 0.0,
        "profit_loss_ratio": (total_win / total_loss) if total_loss > 0 else None,
        "best_day_pnl": max(pnl_list, default=0.0),
        "worst_day_pnl": min(pnl_list, default=0.0),
        "trade_count": len(trades),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "biggest_buy": biggest_buy,
        "positions": last.get("positions", {}),
        "pending_approvals": last.get("pending_approvals", 0),
    }


def render_review_md(m: dict) -> str:
    """复盘指标 → markdown 报告。"""
    plr = m["profit_loss_ratio"]
    bb = m["biggest_buy"]
    lines = [
        f"# 自动交易{m['kind_cn']} — {m['account']} "
        f"{m['start_date']} ~ {m['end_date']}",
        "",
        "## 净值与盈亏",
        "",
        f"- 区间初资产: {m['start_equity']:,.2f} CNY",
        f"- 区间末资产: {m['end_equity']:,.2f} CNY",
        f"- 区间盈亏: {m['period_pnl']:+,.2f} CNY（收益率 {m['period_return']:+.2%}）",
        f"- 交易日数: {m['trading_days']}（盈利 {m['win_days']} / 亏损 {m['loss_days']}"
        f" / 日胜率 {m['day_win_rate']:.0%}）",
        f"- 盈亏比: {plr:.2f}" if plr is not None else "- 盈亏比: —（区间内无亏损日）",
        f"- 最佳单日: {m['best_day_pnl']:+,.2f} CNY；最差单日: {m['worst_day_pnl']:+,.2f} CNY",
        "",
        "## 交易统计",
        "",
        f"- 成交笔数: {m['trade_count']}（买入 {m['buy_count']} / 卖出 {m['sell_count']}）",
        f"- 累计买入金额: {m['buy_amount']:,.2f} CNY",
        f"- 累计卖出金额: {m['sell_amount']:,.2f} CNY",
        f"- 最大单笔买入: "
        + (f"{bb['symbol']} ×{bb['quantity']} @ {bb['price']:.2f}"
           f"（{bb['quantity'] * bb['price']:,.0f} CNY）" if bb else "无"),
        "",
        "## 期末持仓",
        "",
    ]
    if m["positions"]:
        for sym, info in m["positions"].items():
            lines.append(
                f"- {sym}: {info.get('quantity', 0)} 股, "
                f"市值 {info.get('market_value', 0):,.2f}"
            )
    else:
        lines.append("- 空仓")
    lines += [
        "",
        f"- 遗留待审批订单: {m['pending_approvals']}",
        "",
        "> 数据来源：每日盘后明细 JSON；连续积累 2-3 个月后可据此提炼/回测个人策略。",
        "",
    ]
    return "\n".join(lines)


def write_review(results_dir: str | Path, account: str, kind: str,
                 dailies: list[dict]) -> dict | None:
    """聚合 + 渲染 + 写盘；返回指标 dict（无数据时返回 None）。"""
    if not dailies:
        logger.info("[%s] no daily summaries for %s review — skipped", account, kind)
        return None
    metrics = build_review(dailies, account, kind)
    folder = Path(results_dir) / "auto" / "review"
    folder.mkdir(parents=True, exist_ok=True)
    end_day = metrics["end_date"].replace("-", "")
    path = folder / f"{account}_{kind}_{end_day}.md"
    path.write_text(render_review_md(metrics), encoding="utf-8")
    metrics["report_path"] = str(path)
    logger.info("[%s] %s review written: %s", account, kind, path)
    return metrics


def is_last_trading_day_of_month(now: datetime, is_trading_day) -> bool:
    """今天是否本月最后一个交易日（月报触发条件）。

    月末非交易日（周末/节假日）不算——那天盘后流程本来就不会跑，
    月报在真正的最后一个交易日盘后生成。
    """
    if not is_trading_day(now):
        return False
    d = now.date() + timedelta(days=1)
    while d.month == now.month:
        if is_trading_day(datetime.combine(d, dtime(15, 0))):
            return False
        d += timedelta(days=1)
    return True
