"""盯盘监控：定时巡检持仓 → 策略评估 → 触发信号自动卖出 → 信号日志。

由 :mod:`tradingagents.scheduler` 以分钟级间隔驱动（交易时段内），
每个账号一个 :class:`PriceMonitor`：

    持仓 → 多源实时报价(quote_sources) → strategy.evaluate_position
        → 信号 → 可卖数量(T+1) → executor（完整风控链）→ JSONL 信号日志

卖出信号属保护性操作，直接执行（不进大额审批）；T+1 当日买入的股份
``available=0`` 会跳过并记录 SKIPPED_T1，次日自动可卖。

broker 会话失效（如 xiadan.exe 掉线）时持仓查询会抛异常——这不是可静默
跳过的场景：止损保护会整体失效。因此查询失败必须升级为 critical 告警
（QUERY_FAILED），而不是被误记成 T+1 跳过。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .dataflows import quote_sources
from .execution import EXECUTED, OrderExecutor
from .strategy import Signal, StrategyConfig, evaluate_position

logger = logging.getLogger(__name__)

_SIGNAL_KINDS_CN = {
    "stop_loss": "止损", "take_profit": "止盈",
    "trailing_stop": "移动止损", "ma_cross": "均线死叉", "max_hold": "持有到期",
}


class PriceMonitor:
    """单账号盯盘器：退出策略 + 异动检测（事件驱动 LLM 唤醒入口）。"""

    def __init__(
        self,
        account_name: str,
        broker,
        executor: OrderExecutor,
        strategy: StrategyConfig | None = None,
        signal_dir: str | Path | None = None,
        quote_fn=quote_sources.get_quote,
        kline_fn=quote_sources.get_kline,
        now_fn=datetime.now,
        on_anomaly=None,
        anomaly_pct: float = 0.04,
        near_stop_pct: float = 0.01,
        near_limit_pct: float = 0.09,
    ):
        self.account = account_name
        self.broker = broker
        self.executor = executor
        self.strategy = strategy or StrategyConfig()
        self._quote_fn = quote_fn
        self._kline_fn = kline_fn
        self._now_fn = now_fn
        # 异动回调：on_anomaly(symbol, kind, detail)。AutoTrader 用它清掉
        # 「今日已分析」标记，让下一轮盘中扫描对异动标的重新跑 LLM 分析。
        self.on_anomaly = on_anomaly
        self.anomaly_pct = anomaly_pct          # 日内涨跌幅阈值（±4%）
        self.near_stop_pct = near_stop_pct      # 距止损线余量（1%）
        self.near_limit_pct = near_limit_pct    # 逼近涨跌停（9%）

        if signal_dir is None:
            from .default_config import DEFAULT_CONFIG

            signal_dir = Path(DEFAULT_CONFIG.get("results_dir", ".")) / "monitor"
        self.signal_dir = Path(signal_dir)
        self.signal_dir.mkdir(parents=True, exist_ok=True)
        self.signal_file = self.signal_dir / f"{account_name}.jsonl"

    # ── 主循环 ──

    def check_once(self) -> list[dict]:
        """巡检全部持仓一次；返回本次产生的信号记录（含执行结果）。"""
        records: list[dict] = []
        try:
            positions = self.broker.get_positions()
        except Exception as exc:
            logger.exception("[%s] monitor: query positions failed: %s", self.account, exc)
            self._notify_broker_failure("持仓巡检", exc)
            return records

        needs_kline = self.strategy.trailing_stop_pct or self.strategy.ma_cross_exit

        for symbol, pos in positions.items():
            if pos.quantity <= 0:
                continue
            try:
                quote = self._quote_fn(symbol)
            except Exception as exc:
                logger.debug("[%s] monitor: quote failed for %s: %s", self.account, symbol, exc)
                quote = None
            if not quote or quote.get("price", 0) <= 0:
                continue

            price = float(quote["price"])
            kline = None
            if needs_kline:
                try:
                    kline = self._kline_fn(symbol, 120)
                except Exception as exc:
                    logger.debug("[%s] monitor: kline failed for %s: %s", self.account, symbol, exc)

            hold_days = self._hold_days(pos)
            signals = evaluate_position(pos, price, kline, hold_days, self.strategy)
            for sig in signals:
                records.append(self._act(sig, quote, hold_days))

            # 异动检测（退出信号之外的事件层；去抖由回调方/通知层负责）
            self._check_anomaly(symbol, pos, quote)

        if records:
            self._append_log(records)
        return records

    # ── 异动检测（事件驱动 LLM 唤醒） ──

    def _check_anomaly(self, symbol: str, pos, quote: dict) -> None:
        """纯规则异动检测：急涨急跌 / 逼近止损线 / 逼近涨跌停。

        复用本次巡检已拿到的报价，不产生额外网络请求；触发即调用
        ``on_anomaly``（AutoTrader 重置分析标记 → 下一轮重新 LLM 分析）
        并推送钉钉通知。任何异常都只记日志——盯盘主流程不受影响。
        """
        try:
            price = float(quote.get("price") or 0)
            prev_close = float(quote.get("prev_close") or 0)
            if price <= 0 or prev_close <= 0:
                return

            day_pct = price / prev_close - 1.0
            anomalies: list[tuple[str, str]] = []

            if abs(day_pct) >= self.near_limit_pct:
                direction = "涨停" if day_pct > 0 else "跌停"
                anomalies.append((
                    "near_limit",
                    f"日内 {day_pct:+.1%}，逼近{direction}限制",
                ))
            elif abs(day_pct) >= self.anomaly_pct:
                anomalies.append((
                    "intraday_surge",
                    f"日内急{'涨' if day_pct > 0 else '跌'} {day_pct:+.1%}（阈值 ±{self.anomaly_pct:.0%}）",
                ))

            cost = pos.avg_cost or 0
            if cost > 0:
                stop_line = self.strategy.stop_line(cost)
                if stop_line < price <= stop_line * (1.0 + self.near_stop_pct):
                    gap = (price / stop_line - 1.0) * 100
                    anomalies.append((
                        "near_stop_loss",
                        f"现价 {price:.2f} 距止损线 {stop_line:.2f} 仅 {gap:.1f}%",
                    ))

            if not anomalies:
                return

            name = quote.get("name") or symbol
            for kind, detail in anomalies:
                logger.info("[%s] anomaly %s %s: %s", self.account, symbol, kind, detail)
                if self.on_anomaly is not None:
                    try:
                        self.on_anomaly(symbol, kind, detail)
                    except Exception:
                        logger.exception("[%s] anomaly callback failed for %s", self.account, symbol)
                self._notify_anomaly(symbol, name, kind, detail)
        except Exception:
            logger.exception("[%s] anomaly check failed for %s", self.account, symbol)

    def _notify_anomaly(self, symbol: str, name: str, kind: str, detail: str) -> None:
        """异动 → 钉钉通知；按 标的+类型 每小时去重（notifier 全局 TTL 30 分钟
        内同 key 直接跳过，这里 key 粒度加小时桶）。"""
        try:
            from .notifier import notify

            hour_bucket = self._now_fn().strftime("%Y%m%d%H")
            notify(
                "持仓异动 · 已触发重新分析",
                f"**账号**：{self.account}\n\n"
                f"**标的**：{name}（{symbol}）\n\n"
                f"**事件**：{detail}\n\n"
                f"**动作**：该标的已重置分析状态，下一轮盘中扫描将重新运行多代理分析",
                level="warning",
                key=f"anomaly:{self.account}:{symbol}:{kind}:{hour_bucket}",
            )
        except Exception:  # noqa: BLE001
            logger.debug("anomaly notify failed", exc_info=True)

    # ── 信号执行 ──

    def _act(self, sig: Signal, quote: dict, hold_days: int | None) -> dict:
        """执行一个卖出信号（可卖数量、整手、走 executor 风控链）。"""
        record = {
            "ts": self._now_fn().strftime("%Y-%m-%d %H:%M:%S"),
            "account": self.account,
            "symbol": sig.symbol,
            "name": quote.get("name", ""),
            "kind": sig.kind,
            "kind_cn": _SIGNAL_KINDS_CN.get(sig.kind, sig.kind),
            "price": sig.price,
            "detail": sig.detail,
            "action": "none",
            "outcome": "",
            "hold_days": hold_days,
        }

        try:
            positions = self.broker.get_positions()
        except Exception as exc:
            # 持仓查询失败 ≠ 无持仓：信号已触发却无法执行，必须显式暴露，
            # 不能被误记成 T+1 跳过（那看起来像"一切正常，明天再卖"）。
            logger.exception(
                "[%s] monitor: position query failed while acting on %s: %s",
                self.account, sig.symbol, exc,
            )
            record.update(action="sell", outcome="QUERY_FAILED",
                          note=f"持仓查询失败，保护性卖出未执行："
                               f"{type(exc).__name__}: {exc}")
            self._notify_broker_failure(f"信号执行（{_SIGNAL_KINDS_CN.get(sig.kind, sig.kind)}）", exc)
            return record
        pos = positions.get(sig.symbol)
        available = (pos.available or 0) if pos else 0
        quantity = (available // 100) * 100
        if quantity <= 0:
            record.update(action="sell", outcome="SKIPPED_T1",
                          note="当日买入 T+1 不可卖，次日巡检自动重试")
            return record

        result = self.executor.execute(
            symbol=sig.symbol,
            action="sell",
            price=sig.price,
            quantity=quantity,
            name=quote.get("name", ""),
            prev_close=quote.get("prev_close"),
            is_st="ST" in str(quote.get("name", "")).upper(),
            last_price=sig.price,
            confirm=True,                     # 保护性卖出直接执行
            tag=f"monitor:{sig.kind}",
        )
        record.update(action="sell", quantity=quantity, outcome=result.decision,
                      note=result.reason)
        if result.decision == EXECUTED:
            logger.info("[%s] monitor executed %s sell %d x %s",
                        self.account, sig.symbol, quantity, sig.kind)
            self._notify_exit(sig, quantity, quote)
        return record

    def _notify_broker_failure(self, context: str, exc: Exception) -> None:
        """broker 会话失效（xiadan 掉线等）→ critical 告警。

        此时止损保护整体失效，属于必须人工介入的事件；按 标的+小时桶
        去重，最多每小时提醒一次（巡检是分钟级的）。
        """
        try:
            from .notifier import notify

            hour_bucket = self._now_fn().strftime("%Y%m%d%H")
            notify(
                "盯盘通道异常，保护性卖出可能失效",
                f"**账号**：{self.account}\n\n"
                f"**环节**：{context}\n\n"
                f"**原因**：{type(exc).__name__}: {exc}\n\n"
                "**动作**：请检查交易客户端（如 xiadan.exe）是否在线并已登录；"
                "恢复后下一轮巡检自动继续",
                level="critical",
                key=f"monitor-broker:{self.account}:{hour_bucket}",
            )
        except Exception:  # noqa: BLE001 — 通知失败不改变主流程
            logger.debug("broker-failure notify failed", exc_info=True)

    def _notify_exit(self, sig: Signal, quantity: int, quote: dict) -> None:
        """保护性退出已成交 → 钉钉通知（止损/止盈是必须知道的事件）。"""
        try:
            from .notifier import notify

            kind_cn = _SIGNAL_KINDS_CN.get(sig.kind, sig.kind)
            notify(
                f"{kind_cn}已执行",
                f"**账号**：{self.account}\n\n"
                f"**标的**：{quote.get('name', '')}（{sig.symbol}）\n\n"
                f"**动作**：卖出 {quantity} 股 @ {sig.price:.2f}\n\n"
                f"**原因**：{sig.detail}",
                level="warning" if sig.kind in ("stop_loss", "trailing_stop") else "info",
                key=f"exit:{self.account}:{sig.symbol}:{sig.kind}:"
                    f"{self._now_fn().strftime('%Y%m%d')}",
            )
        except Exception:  # noqa: BLE001 — 通知失败不影响卖出
            logger.debug("exit notify failed", exc_info=True)

    # ── 辅助 ──

    def _hold_days(self, pos) -> int | None:
        buy_date = getattr(pos, "buy_date", None)
        if not buy_date:
            return None
        try:
            bought = datetime.strptime(str(buy_date)[:10], "%Y-%m-%d")
            return (self._now_fn() - bought).days
        except ValueError:
            return None

    def _append_log(self, records: list[dict]) -> None:
        try:
            with open(self.signal_file, "a", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("failed to append monitor log: %s", exc)

    def load_history(self, limit: int = 100) -> list[dict]:
        """读取本账号信号历史（新→旧），供 UI 展示。"""
        if not self.signal_file.exists():
            return []
        rows = []
        try:
            with open(self.signal_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError as exc:
            logger.warning("failed to read monitor log: %s", exc)
        return list(reversed(rows[-limit:]))

    # ── 持仓策略状态（UI 展示用，不产生交易） ──

    def position_status(self) -> list[dict]:
        """每只持仓的策略状态：现价/止损线/止盈线/距触发空间/持有天数。"""
        statuses = []
        try:
            positions = self.broker.get_positions()
        except Exception:
            return statuses
        for symbol, pos in positions.items():
            if pos.quantity <= 0:
                continue
            quote = self._quote_fn(symbol) or {}
            price = float(quote.get("price") or pos.last_price or 0)
            cost = pos.avg_cost or price
            hold_days = self._hold_days(pos)
            statuses.append({
                "symbol": symbol,
                "name": quote.get("name", "") or pos.name,
                "quantity": pos.quantity,
                "available": pos.available,
                "cost": cost,
                "price": price,
                "pnl_pct": (price / cost - 1.0) * 100 if cost > 0 else 0.0,
                "stop_line": self.strategy.stop_line(cost) if cost > 0 else 0.0,
                "target_line": self.strategy.target_line(cost) if cost > 0 else 0.0,
                "hold_days": hold_days if hold_days is not None else "-",
            })
        return statuses
