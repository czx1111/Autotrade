"""自动化交易入口：python run_auto.py [选项]

常用用法::

    python run_auto.py                      # 常驻运行：盘前筛选 / 盘中分析下单 / 盘后复盘
    python run_auto.py --once               # 立即跑一轮盘中流程（忽略交易时段，试跑用）
    python run_auto.py --account pingan     # 只跑指定账号
    python run_auto.py --list-pending       # 查看待审批的大额订单
    python run_auto.py --approve <ID>       # 批准一笔待审批订单（下一轮盘中自动执行）
    python run_auto.py --reject <ID>        # 拒绝一笔待审批订单
    python run_auto.py --review weekly      # 手动生成周报（monthly 为月报）

账号配置读取 ``--accounts`` 指定的 JSON（默认 ``accounts.json``，模板见
``accounts.example.json``）。没有配置文件时退回 DEFAULT_CONFIG 的单账号
（paper 模拟盘），便于先用模拟盘验证闭环。

LLM 密钥等环境变量沿用 ``.env``（TRADINGAGENTS_* 系列）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _load_accounts(path: str) -> list[dict]:
    file = Path(path)
    if not file.exists():
        return []
    data = json.loads(file.read_text(encoding="utf-8"))
    if isinstance(data, dict):          # 允许 {"accounts": [...]} 或直接 [...]
        data = data.get("accounts", [])
    return data


def _build_traders(account_names: list[str] | None, accounts_path: str):
    from tradingagents.auto_trader import AutoTrader
    from tradingagents.default_config import DEFAULT_CONFIG

    accounts = _load_accounts(accounts_path)
    if not accounts:
        accounts = [{
            "name": "paper-default",
            "broker_settings": {"broker": "paper"},
            "watchlist": ["600519", "000858", "300750"],
            "screening_enabled": False,
        }]
        logging.info("no accounts file found — using single paper account for dry run")

    if account_names:
        accounts = [a for a in accounts if a.get("name") in account_names]
        missing = set(account_names) - {a.get("name") for a in accounts}
        if missing:
            raise SystemExit(f"account(s) not found in {accounts_path}: {', '.join(missing)}")

    # 状态目录：当日已分析标记 / 日亏基线 / 挂单看护都落盘在这里，
    # 盘中进程重启不丢当日状态。
    state_dir = Path(DEFAULT_CONFIG["results_dir"]) / "state"
    return [AutoTrader(acc, DEFAULT_CONFIG.copy(), state_dir=state_dir)
            for acc in accounts]


def _run_daemon(traders) -> None:
    from tradingagents.dataflows.trading_calendar import is_trading_day
    from tradingagents.process_lock import AccountProcessLock
    from tradingagents.scheduler import TradingScheduler

    # 每账号一把锁：同一账号双开会让风控预算翻倍、审批状态互相覆盖。
    locks = [AccountProcessLock(t.account.name) for t in traders]
    acquired = []
    for lock in locks:
        if not lock.acquire():
            raise SystemExit(lock.conflict_message)
        acquired.append(lock)

    from tradingagents.notifier import notify

    _PHASE_CN = {
        "pre_market": "盘前筛选", "intraday": "盘中分析下单",
        "monitor": "盯盘巡检", "post_market": "盘后复盘",
    }

    def _write_heartbeat(phase: str, account: str, status: str,
                         started: float, detail: str = "") -> None:
        """守护进程心跳：每账号每阶段最近一次执行记录（UI 健康页读取）。

        文件小且覆盖写，即使每分钟写一次也几乎无 IO 开销。
        """
        import json
        from pathlib import Path

        from tradingagents.default_config import DEFAULT_CONFIG

        try:
            path = Path(DEFAULT_CONFIG["results_dir"]) / "daemon_heartbeat.json"
            data = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            data[f"{account}:{phase}"] = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "phase": phase,
                "phase_cn": _PHASE_CN.get(phase, phase),
                "account": account,
                "status": status,             # ok | error | skipped
                "duration_s": round(time.time() - started, 1),
                "detail": detail[:200],
            }
            data["_daemon"] = {
                "started_at": data.get("_daemon", {}).get(
                    "started_at", time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
                "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
                "accounts": [t.account.name for t in traders],
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except Exception:
            logging.debug("heartbeat write failed", exc_info=True)

    def run_phase(phase: str) -> None:
        for trader in traders:
            started = time.time()
            try:
                if phase == "pre_market":
                    trader.run_pre_market()
                elif phase == "intraday":
                    for record in trader.run_intraday():
                        logging.info("[%s] %s", trader.account.name, record)
                elif phase == "monitor":
                    for sig in trader.run_monitor():
                        logging.info("[%s] monitor: %s", trader.account.name, sig)
                elif phase == "post_market":
                    summary = trader.run_post_market()
                    logging.info("[%s] post-market: %s", trader.account.name, summary)
                _write_heartbeat(phase, trader.account.name, "ok", started)
            except Exception as exc:
                logging.exception("[%s] %s phase failed", trader.account.name, phase)
                _write_heartbeat(
                    phase, trader.account.name, "error", started,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                notify(
                    "交易流程异常",
                    f"**账号**：{trader.account.name}\n\n"
                    f"**阶段**：{_PHASE_CN.get(phase, phase)}\n\n"
                    f"**时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    "详情见进程日志（logging.exception 已记录堆栈）。",
                    level="critical",
                    key=f"phase-error:{trader.account.name}:{phase}:"
                        f"{time.strftime('%Y%m%d')}",
                )

    scheduler = TradingScheduler(run_phase, is_trading_day=is_trading_day)
    scheduler.start()
    logging.info(
        "auto trading daemon running with %d account(s): %s — Ctrl+C to stop",
        len(traders), ", ".join(t.account.name for t in traders),
    )
    notify(
        "交易守护进程已启动",
        f"**账号**：{', '.join(t.account.name for t in traders)}\n\n"
        f"**交易日历**：交易所日历已启用（节假日自动跳过）\n\n"
        f"**启动时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        level="info",
    )

    def _guard_brokers() -> None:
        """交易日 08:45–15:30 每分钟巡检交易客户端进程，掉线自动拉起。

        easytrader 账号由 health_check 拉起 xiadan.exe 并重连；paper/QMT
        无进程守护需求（no-op）。时段外不巡检——用户可随时手动关闭
        客户端而不会被反复拉起。
        """
        now = datetime.now()
        if not is_trading_day(now):
            return
        if not (dtime(8, 45) <= now.time() <= dtime(15, 30)):
            return
        for trader in traders:
            try:
                trader.broker.health_check()
            except Exception:
                logging.debug(
                    "[%s] broker health check failed",
                    trader.account.name, exc_info=True,
                )

    try:
        while True:
            time.sleep(60)
            _guard_brokers()
    except (KeyboardInterrupt, SystemExit):
        logging.info("shutting down…")
        notify("交易守护进程已停止", "收到退出信号，调度器已停止。", level="warning")
        scheduler.stop()
        for trader in traders:
            trader.broker.close()
    finally:
        for lock in acquired:
            lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradingAgents 自动化交易")
    parser.add_argument("--accounts", default="accounts.json", help="账号配置 JSON 路径")
    parser.add_argument("--account", action="append", help="只跑指定账号（可多次）")
    parser.add_argument("--once", action="store_true", help="立即跑一轮盘中流程（试跑）")
    parser.add_argument("--monitor-once", action="store_true", help="立即盯盘巡检一次（止损/止盈检查）")
    parser.add_argument("--pre-market", action="store_true", help="立即跑盘前筛选")
    parser.add_argument("--post-market", action="store_true", help="立即跑盘后复盘")
    parser.add_argument("--list-pending", action="store_true", help="列出待审批订单")
    parser.add_argument("--approve", metavar="ORDER_ID", help="批准一笔待审批订单")
    parser.add_argument("--reject", metavar="ORDER_ID", help="拒绝一笔待审批订单")
    parser.add_argument("--review", choices=["weekly", "monthly"],
                        help="手动生成周报/月报复盘报告")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    names = args.account
    traders = _build_traders(names, args.accounts)

    # 审批管理（不需要行情/LLM，直接操作审批存储）
    if args.list_pending or args.approve or args.reject:
        from tradingagents.auto_trader import ApprovalStore

        for trader in traders:
            store = trader.approvals
            if args.approve:
                if store.set_status(args.approve, "approved"):
                    print(f"approved: {args.approve}")
                else:
                    print(f"not found: {args.approve}")
            if args.reject:
                if store.set_status(args.reject, "rejected"):
                    print(f"rejected: {args.reject}")
                else:
                    print(f"not found: {args.reject}")
            if args.list_pending:
                open_entries = store.list("pending") + store.list("approved")
                if not open_entries:
                    print(f"[{trader.account.name}] no pending orders")
                for e in open_entries:
                    print(
                        f"[{trader.account.name}] {e.id}  {e.status:8s} "
                        f"{e.action} {e.symbol} x{e.quantity} "
                        f"≈{e.estimate_value:,.0f} CNY  ({e.reason})"
                    )
        return 0

    if args.review:
        for trader in traders:
            metrics = trader.run_review(args.review)
            if metrics:
                print(f"[{trader.account.name}] {metrics['kind_cn']}已生成: "
                      f"{metrics.get('report_path')}")
                print(f"  区间 {metrics['start_date']} ~ {metrics['end_date']}, "
                      f"盈亏 {metrics['period_pnl']:+,.2f} CNY "
                      f"({metrics['period_return']:+.2%}), "
                      f"日胜率 {metrics['day_win_rate']:.0%}")
            else:
                print(f"[{trader.account.name}] 区间内无每日明细，未生成"
                      f"{args.review}报告（需先跑盘后复盘）")
        return 0

    if args.once or args.monitor_once or args.pre_market or args.post_market:
        for trader in traders:
            if args.pre_market:
                print(f"[{trader.account.name}] watchlist: {trader.run_pre_market()}")
            if args.once:
                for record in trader.run_intraday(force=True):
                    print(f"[{trader.account.name}] {record}")
            if args.monitor_once:
                signals = trader.get_monitor().check_once()   # 手动巡检不受交易时段限制
                if signals:
                    for sig in signals:
                        print(f"[{trader.account.name}] {sig}")
                else:
                    print(f"[{trader.account.name}] 无信号，持仓均在策略区间内")
            if args.post_market:
                print(f"[{trader.account.name}] {trader.run_post_market()}")
        return 0

    _run_daemon(traders)
    return 0


if __name__ == "__main__":
    sys.exit(main())
