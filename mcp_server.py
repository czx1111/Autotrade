"""autotrade MCP server — 把交易系统暴露为 DSH 可调用的工具集。

架构定位：DeepSeek Harness（DSH）做主 agent（对话与决策编排），autotrade
降级为工具库。本 server 通过 MCP stdio 暴露以下能力::

    DSH (主 agent)  ←MCP bridge→  本 server  →  AutoTrader / LangGraph / broker

工具分组::

    只读数据   get_quote / get_market_snapshot / list_accounts
    深度分析   analyze_symbol（LangGraph 多智能体全管线，耗时数分钟）
    账户查询   get_account / get_positions / get_pending_orders / get_daily_summary
    交易动作   place_order / approve_order / reject_order
    流程触发   run_intraday_once / run_monitor_once

安全设计（DSH 无法绕过）::

- 下单走 ``AutoTrader.submit_manual_trade``：executor 全量风控（涨跌停/
  手数/T+1/仓位/日亏）+ 实盘大额自动进审批队列，批准前不发单；
- 环境变量 ``AUTOTRADE_MCP_ALLOW_TRADE=0`` 可整体禁用交易类工具（只读
  模式，用于先跑通分析再放开交易）；
- paper 账号的状态文件与 run_auto 守护进程共享：同账号避免双进程同时
  下单（easytrader 实盘以柜台为准，无此问题）。

用法::

    python mcp_server.py                # stdio 模式（由 MCP 客户端拉起）
    python mcp_server.py --list-tools   # 列出已注册工具（自检）
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import threading
from pathlib import Path

# stdio 传输下 stdout 属于协议信道，日志必须走 stderr。
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("mcp_server")

_REPO_ROOT = Path(__file__).resolve().parent

fastmcp = None  # 惰性导入，保持 --list-tools 之外的启动路径简单
mcp = None

_TRADERS: dict = {}          # account_name -> AutoTrader（惰性构造）
_TRADERS_LOCK = threading.Lock()

_ACCOUNTS_FILE = os.environ.get(
    "AUTOTRADE_ACCOUNTS", str(_REPO_ROOT / "accounts.json")
)


def _trade_allowed() -> bool:
    return os.environ.get("AUTOTRADE_MCP_ALLOW_TRADE", "1").strip().lower() not in (
        "0", "false", "no",
    )


# ── 基础设施 ─────────────────────────────────────────────────────────────


def _dump(obj) -> str:
    """统一 JSON 输出（dataclass 自动展开，中文不转义）。"""

    def _expand(v):
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            return dataclasses.asdict(v)
        if isinstance(v, Path):
            return str(v)
        return v

    def _default(v):
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            return dataclasses.asdict(v)
        if isinstance(v, Path):
            return str(v)
        return str(v)

    return json.dumps(obj, ensure_ascii=False, indent=2, default=_default)


def _load_account_dicts() -> list[dict]:
    file = Path(_ACCOUNTS_FILE)
    if not file.exists():
        return []
    data = json.loads(file.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("accounts", [])
    return data


def _get_trader(account: str = ""):
    """按账号名取/建 AutoTrader（惰性，easytrader 账号构造时自动拉起客户端）。"""
    with _TRADERS_LOCK:
        if not account:
            if not _TRADERS:
                _TRADERS[_first_account_name()] = _build_trader(None)
            return next(iter(_TRADERS.values()))
        if account in _TRADERS:
            return _TRADERS[account]
        trader = _build_trader(account)
        _TRADERS[account] = trader
        return trader


def _first_account_name() -> str:
    accounts = _load_account_dicts()
    if not accounts:
        return "paper-default"
    return str(accounts[0].get("name") or "paper-default")


def _build_trader(account: str | None):
    from tradingagents.auto_trader import AutoTrader
    from tradingagents.default_config import DEFAULT_CONFIG

    accounts = _load_account_dicts()
    if not accounts:
        accounts = [{
            "name": "paper-default",
            "broker_settings": {"broker": "paper"},
            "watchlist": ["600519", "000858", "300750"],
            "screening_enabled": False,
        }]
        logger.info("no accounts file found — using single paper account")
    if account:
        match = [a for a in accounts if a.get("name") == account]
        if not match:
            names = ", ".join(str(a.get("name", "")) for a in accounts)
            raise KeyError(f"账号不存在: {account}（可用: {names}）")
        acc = match[0]
    else:
        acc = accounts[0]
    state_dir = Path(DEFAULT_CONFIG["results_dir"]) / "state"
    return AutoTrader(acc, DEFAULT_CONFIG.copy(), state_dir=state_dir)


def _latest_report(symbol: str) -> str | None:
    """该标的最近一次分析报告（complete_report.md）路径。"""
    from tradingagents.default_config import DEFAULT_CONFIG

    reports = Path(DEFAULT_CONFIG["results_dir"]) / "reports"
    if not reports.is_dir():
        return None
    candidates = sorted(
        reports.glob(f"{symbol}_*/complete_report.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(candidates[0]) if candidates else None


# ── 工具实现（纯函数，便于直接测试）──────────────────────────────────────


def list_accounts_impl() -> str:
    """账号清单（只读 JSON，不连接交易客户端）。"""
    accounts = _load_account_dicts()
    if not accounts:
        accounts = [{"name": "paper-default", "broker_settings": {"broker": "paper"}}]
    return _dump({
        "accounts": [
            {
                "name": str(a.get("name") or "paper-default"),
                "broker": (a.get("broker_settings") or {}).get("broker", "paper"),
                "watchlist": a.get("watchlist", []),
            }
            for a in accounts
        ],
        "trade_enabled": _trade_allowed(),
    })


def get_quote_impl(symbols: str) -> str:
    """实时行情快照。symbols: 逗号分隔的6位A股代码。"""
    from tradingagents.auto_trader import fetch_quotes_ashare

    codes = [s.strip() for s in symbols.replace("，", ",").split(",") if s.strip()]
    if not codes:
        return _dump({"error": "symbols 为空，示例: '600519,000858'"})
    quotes = fetch_quotes_ashare(codes)
    return _dump({
        "quotes": {
            code: {
                "name": q.name,
                "price": q.price,
                "prev_close": q.prev_close,
                "is_st": q.is_st,
            }
            for code, q in quotes.items()
        },
        "missing": [c for c in codes if c not in quotes],
    })


def get_market_snapshot_impl() -> str:
    """A股大盘快照（指数/涨跌家数/市场情绪），文本形式。"""
    from tradingagents.dataflows.interface import route_to_vendor

    return str(route_to_vendor("get_ashare_market_snapshot"))


def analyze_symbol_impl(symbol: str, account: str = "") -> str:
    """单标的深度分析：LangGraph 多智能体全管线（分析→研究辩论→风控→交易员）。

    耗时数分钟（多轮 LLM 调用），仅在需要深度研究时使用；快速看盘用
    get_quote / get_market_snapshot。返回最终决策与完整报告路径。
    """
    symbol = symbol.strip()
    if not symbol:
        return _dump({"error": "symbol 为空"})
    trader = _get_trader(account)
    decision = trader._decide(symbol)  # noqa: SLF001 — AutoTrader 的稳定内部入口
    return _dump({
        "symbol": symbol,
        "decision": decision,
        "report": _latest_report(symbol),
    })


def get_account_impl(account: str = "") -> str:
    """账户资产（总资产/可用/市值/冻结）。"""
    trader = _get_trader(account)
    info = trader.broker.get_account()
    return _dump({
        "account": trader.account.name,
        "mode": trader.broker.mode,
        "total_asset": info.total_asset,
        "available_cash": info.available_cash,
        "market_value": info.market_value,
        "frozen_cash": info.frozen_cash,
    })


def get_positions_impl(account: str = "") -> str:
    """当前持仓（数量/可用/成本/现价）。"""
    trader = _get_trader(account)
    positions = trader.broker.get_positions()
    return _dump({
        "account": trader.account.name,
        "positions": [dataclasses.asdict(p) for p in positions.values()],
    })


def get_pending_orders_impl(account: str = "") -> str:
    """待审批的大额订单（pending + 已批准待执行）。"""
    trader = _get_trader(account)
    entries = trader.approvals.list("pending") + trader.approvals.list("approved")
    return _dump({
        "account": trader.account.name,
        "pending": [e.to_dict() for e in entries],
    })


def get_daily_summary_impl(account: str = "") -> str:
    """当日交易汇总（成交/盈亏/风控状态）。"""
    trader = _get_trader(account)
    summary = trader._daily_summary()  # noqa: SLF001 — 同上
    return _dump({"account": trader.account.name, "summary": summary})


def place_order_impl(
    symbol: str, action: str, quantity: int, price: float, account: str = "",
) -> str:
    """提交委托。走全量风控（涨跌停/手数/T+1/仓位/日亏）；实盘大额
    自动进审批队列（返回 PENDING_APPROVAL + approval_id），需
    approve_order 批准后执行。action: buy/sell；quantity: 股数（买入
    须 100 整数倍）；price: 限价委托价。
    """
    if not _trade_allowed():
        return _dump({
            "error": "交易工具已被禁用（AUTOTRADE_MCP_ALLOW_TRADE=0），"
                     "仅提供只读分析能力",
        })
    trader = _get_trader(account)
    record = trader.submit_manual_trade(
        symbol=symbol.strip(), action=action, quantity=int(quantity),
        price=float(price),
    )
    logger.info("[MCP] place_order %s %s x%d @%.2f → %s",
                trader.account.name, symbol, quantity, price,
                record.get("outcome"))
    return _dump(record)


def approve_order_impl(order_id: str, account: str = "") -> str:
    """批准一笔待审批订单（下一轮盘中流程自动执行）。"""
    trader = _get_trader(account)
    ok = trader.approvals.set_status(order_id.strip(), "approved")
    return _dump({"order_id": order_id, "approved": ok,
                  "account": trader.account.name})


def reject_order_impl(order_id: str, account: str = "") -> str:
    """拒绝一笔待审批订单。"""
    trader = _get_trader(account)
    ok = trader.approvals.set_status(order_id.strip(), "rejected")
    return _dump({"order_id": order_id, "rejected": ok,
                  "account": trader.account.name})


def run_intraday_once_impl(account: str = "") -> str:
    """立即跑一轮盘中分析下单流程（force，忽略当日已分析标记）。

    对 watchlist 全部标的做分析→决策→（小额）下单/（大额）入审批队列。
    耗时随 watchlist 大小线性增长。
    """
    trader = _get_trader(account)
    records = trader.run_intraday(force=True)
    return _dump({"account": trader.account.name, "records": records})


def run_monitor_once_impl(account: str = "") -> str:
    """立即跑一轮盯盘巡检（止损/止盈/异动信号）。"""
    trader = _get_trader(account)
    signals = trader.run_monitor()
    return _dump({"account": trader.account.name, "signals": signals})


# ── 注册与入口 ───────────────────────────────────────────────────────────


def _warm_imports() -> None:
    """主线程预热 C 扩展重库（numpy/pandas）。

    实测 Windows + fastmcp：pandas 的 C 扩展首次加载若发生在 anyio
    worker 线程（工具调用）里会死锁挂起，主线程预 import 后命中模块
    缓存即可规避。pandas 是 quote_sources / akshare / yfinance 的公共
    依赖，预热它一行覆盖整条数据链。
    """
    import time as _time

    t0 = _time.time()
    import numpy  # noqa: F401
    import pandas  # noqa: F401

    logger.info("warm imports (numpy/pandas) done in %.1fs", _time.time() - t0)


def _register_tools() -> None:
    """显式注册（实现保持纯函数，便于单元测试直接调用）。"""
    mcp.tool(list_accounts_impl, name="list_accounts")
    mcp.tool(get_quote_impl, name="get_quote")
    mcp.tool(get_market_snapshot_impl, name="get_market_snapshot")
    mcp.tool(analyze_symbol_impl, name="analyze_symbol")
    mcp.tool(get_account_impl, name="get_account")
    mcp.tool(get_positions_impl, name="get_positions")
    mcp.tool(get_pending_orders_impl, name="get_pending_orders")
    mcp.tool(get_daily_summary_impl, name="get_daily_summary")
    mcp.tool(place_order_impl, name="place_order")
    mcp.tool(approve_order_impl, name="approve_order")
    mcp.tool(reject_order_impl, name="reject_order")
    mcp.tool(run_intraday_once_impl, name="run_intraday_once")
    mcp.tool(run_monitor_once_impl, name="run_monitor_once")


def main(argv: list[str] | None = None) -> int:
    global fastmcp, mcp

    # MCP stdio 规范要求 UTF-8；Windows 子进程默认 GBK 会把中文响应变乱码。
    # （pytest 等宿主会替换 stdin/stdout 对象，防御性 reconfigure。）
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="autotrade MCP server")
    parser.add_argument("--accounts", default=None,
                        help="账号配置 JSON 路径（默认环境变量或仓库根 accounts.json）")
    parser.add_argument("--list-tools", action="store_true", help="列出已注册工具后退出")
    args = parser.parse_args(argv)

    global _ACCOUNTS_FILE
    if args.accounts:
        _ACCOUNTS_FILE = str(Path(args.accounts).resolve())

    # 所有相对路径（.env / accounts.json / results_dir）以仓库根为基准，
    # 与 run_auto.py 的运行环境保持一致。
    os.chdir(_REPO_ROOT)

    try:
        from fastmcp import FastMCP
    except ImportError:
        print("fastmcp 未安装: pip install fastmcp", file=sys.stderr)
        return 1

    fastmcp = FastMCP  # noqa: F841 — 便于测试 patch
    mcp = FastMCP(
        "autotrade",
        instructions=(
            "A股自动交易系统工具集。只读工具随时可用；"
            "place_order 走全量风控，实盘大额自动进审批队列，"
            "批准前不会发单。analyze_symbol 耗时数分钟，仅深度研究时调用。"
        ),
    )
    _register_tools()
    _warm_imports()

    if args.list_tools:
        import asyncio

        for tool in asyncio.run(mcp.list_tools()):
            print(tool.name)
        return 0

    logger.info("autotrade MCP server starting (stdio), accounts=%s", _ACCOUNTS_FILE)
    mcp.run()  # stdio 传输，由 MCP 客户端（DSH 等）拉起
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
