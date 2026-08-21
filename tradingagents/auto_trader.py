"""自动化交易闭环：多代理分析 → 决策解析 → 仓位映射 → 风控执行 → 大额审批。

这是连接「TradingAgents 多代理分析图」与「券商执行层」的无人值守引擎。
每个账号一个 :class:`AutoTrader` 实例，各自持有独立的 watchlist、仓位参数、
风控与审批阈值（两账号独立跑不同策略）：

    盘前 pre_market   重置当日风控基线、过期旧审批、LLM 从基础股票池筛选当日重点
    盘中 intraday     逐只运行分析图 → 解析 Rating → 目标仓位差额 → 下单；
                      大额订单进入审批队列，批准后下一轮自动执行
    盘后 post_market  T+1 滚动、当日执行汇总报告；周五/月末自动生成周报/月报

Rating → 动作映射（默认，可在账号配置覆盖）::

    Buy         买到目标仓位 12%
    Overweight  买到目标仓位 6%
    Hold        不动
    Underweight 减掉当前持仓的一半
    Sell        清仓（T+1 可用部分）

每个 symbol 每个交易日只分析一次（LLM 成本控制）；盘中后续 tick 只处理
已批准的待执行订单。传入 ``state_dir`` 时，「今日已分析」标记与日亏基线
落盘持久，盘中进程重启不会重复分析下单、也不会重置当日亏损护栏。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .broker import BaseBroker, get_broker
from .execution import (
    EXECUTED,
    PENDING,
    REJECTED,
    OrderExecutor,
    size_order,
)
from .dataflows.trading_calendar import is_trading_time_today
from .open_orders import OpenOrderTracker
from .strategy import StrategyConfig

logger = logging.getLogger(__name__)

# ── 决策解析 ──────────────────────────────────────────────────────────────

_RATING_RE = re.compile(
    r"\*\*Rating\*\*\s*:\s*\**\s*(Buy|Overweight|Hold|Underweight|Sell)\b",
    re.IGNORECASE,
)

VALID_RATINGS = ("buy", "overweight", "hold", "underweight", "sell")

# rating → 目标仓位（None = 不动）。underweight 特殊处理为「减半」。
DEFAULT_RATING_WEIGHTS = {
    "buy": 0.12,
    "overweight": 0.06,
    "hold": None,
    "underweight": "halve",   # 当前仓位减半
    "sell": 0.0,              # 清仓
}


def parse_rating(decision_text: str) -> str | None:
    """从决策文本中提取 rating（小写）。

    兼容两种输入形态：

    - 原始 markdown（``**Rating**: Buy``，测试与 decision_fn 注入）；
    - ``TradingAgentsGraph.propagate`` 返回的已解析裸评级（``"Buy"``）——
      propagate 的第二个返回值就是 ``SignalProcessor.process_signal`` 的
      结果，直接是一个评级词，再跑 markdown 正则必然失配（UNPARSED）。
    """
    if not decision_text:
        return None
    text = decision_text.strip()
    if text.lower() in VALID_RATINGS:
        return text.lower()
    match = _RATING_RE.search(text)
    return match.group(1).lower() if match else None


@dataclass
class Quote:
    """下单定价所需的即时行情。"""

    price: float = 0.0        # 最新价
    prev_close: float = 0.0   # 昨收（涨跌停带检查）
    name: str = ""

    @property
    def is_st(self) -> bool:
        return "ST" in self.name.upper()


# ── 大额审批存储 ─────────────────────────────────────────────────────────


@dataclass
class PendingOrder:
    """等待人工批准的大额订单意向（存 JSON，跨进程复用）。"""

    id: str
    symbol: str
    action: str                 # buy / sell
    quantity: int
    estimate_value: float
    reason: str = ""            # rating + 决策摘要
    status: str = "pending"     # pending | approved | rejected | executed | expired
    created_at: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> "PendingOrder":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ApprovalStore:
    """账号级审批队列，JSON 持久化（默认 ``~/.tradingagents/approvals/``）。"""

    def __init__(self, account_name: str, path: Path | None = None):
        if path is None:
            from .default_config import DEFAULT_CONFIG

            base = Path(DEFAULT_CONFIG.get("results_dir", ".")) / "approvals"
            base.mkdir(parents=True, exist_ok=True)
            path = base / f"{account_name}.json"
        self.path = Path(path)
        self._entries: dict[str, PendingOrder] = self._load()

    def _load(self) -> dict[str, PendingOrder]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {e["id"]: PendingOrder.from_dict(e) for e in raw}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("approval store %s unreadable (%s) — starting fresh", self.path, exc)
            return {}

    def _save(self) -> None:
        self.path.write_text(
            json.dumps([e.to_dict() for e in self._entries.values()],
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, order: PendingOrder) -> None:
        self._entries[order.id] = order
        self._save()

    def get(self, order_id: str) -> PendingOrder | None:
        return self._entries.get(order_id)

    def list(self, status: str | None = None) -> list[PendingOrder]:
        items = list(self._entries.values())
        return [o for o in items if status is None or o.status == status]

    def set_status(self, order_id: str, status: str) -> bool:
        if order_id not in self._entries:
            return False
        self._entries[order_id].status = status
        self._save()
        return True

    def expire_before(self, day: str) -> None:
        """把某日之前创建、仍未终态的条目标记过期（盘前调用）。"""
        terminal = ("executed", "rejected", "expired")
        changed = False
        for entry in self._entries.values():
            if entry.status in terminal or not entry.created_at:
                continue
            if entry.created_at[:10] < day:
                entry.status = "expired"
                changed = True
        if changed:
            self._save()


# ── 行情 ──────────────────────────────────────────────────────────────────

# 小列表阈值：≤ 该数量直接逐票多源并行（腾讯→新浪，~300ms），
# 不碰东财全市场快照——快照拉全市场 ~1MB 只为喂 3-5 只 watchlist
# 本就不划算，且东财挂掉时要付 6s+ 连接超时才走兜底。
_SMALL_LIST_THRESHOLD = 10

# 东财快照失败后的熔断冷却：冷却期内大列表也直接走逐票，避免每次
# 都等东财超时（实测 EM 故障时每轮阻塞 6-8s）。
_EM_COOLDOWN_SECONDS = 600.0
_em_failed_at: float | None = None


def _quotes_from_em_snapshot(wanted: set[str]) -> dict[str, Quote]:
    """东财全市场快照（大列表批量路径）。失败时记录熔断时间。"""
    global _em_failed_at
    try:
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        quotes: dict[str, Quote] = {}
        for _, row in df.iterrows():
            code = str(row.get("代码", ""))
            if code not in wanted:
                continue
            quote = Quote(
                price=float(row.get("最新价") or 0.0),
                prev_close=float(row.get("昨收") or 0.0),
                name=str(row.get("名称") or ""),
            )
            if quote.price > 0:
                quotes[code] = quote
        _em_failed_at = None
        return quotes
    except Exception as exc:
        _em_failed_at = time.time()
        logger.warning("EM spot snapshot failed (%s) — cooldown %.0fs", exc, _EM_COOLDOWN_SECONDS)
        return {}


def _quote_from_multisource(code: str) -> Quote | None:
    """单票多源报价（腾讯→新浪，带 45s TTL 缓存）。"""
    from .dataflows import quote_sources

    try:
        q = quote_sources.get_quote(code)
    except Exception as exc:
        logger.debug("per-symbol quote failed for %s: %s", code, exc)
        return None
    if q and q.get("price", 0) > 0:
        return Quote(
            price=float(q["price"]),
            prev_close=float(q.get("prev_close") or 0.0),
            name=str(q.get("name") or ""),
        )
    return None


def fetch_quotes_ashare(symbols: list[str]) -> dict[str, Quote]:
    """取最新价/昨收/名称，返回 bare code → Quote。

    路径选择：

    - 小列表（≤10，watchlist/盘中下单的常态）：逐票多源并行
      （腾讯→新浪，~300ms），完全不碰东财快照。
    - 大列表（>10，选股/全市场场景）：东财全市场快照一次拉全（健康时
      一个请求覆盖全部），失败进入 10 分钟熔断冷却，冷却期内退化为
      逐票多源；快照缺码/价格无效的部分也走逐票补齐。

    下单定价不能因单一供应商故障而中断——任何路径全源失败才告警。
    """
    from concurrent.futures import ThreadPoolExecutor

    from .dataflows.ashare_symbol_utils import normalize_ashare_symbol

    wanted = {normalize_ashare_symbol(s) or s for s in symbols}
    if not wanted:
        return {}

    quotes: dict[str, Quote] = {}

    # 大列表 + EM 冷却期外：先试 EM 快照批量
    if len(wanted) > _SMALL_LIST_THRESHOLD and (
        _em_failed_at is None or time.time() - _em_failed_at > _EM_COOLDOWN_SECONDS
    ):
        quotes.update(_quotes_from_em_snapshot(wanted))

    # 逐票多源：并行补齐（小列表全量；大列表补 EM 缺口）
    missing = wanted - set(quotes)
    if missing:
        with ThreadPoolExecutor(max_workers=min(len(missing), 8)) as pool:
            results = pool.map(_quote_from_multisource, sorted(missing))
        for code, quote in zip(sorted(missing), results):
            if quote is not None:
                quotes[code] = quote

    missing = wanted - set(quotes)
    if missing:
        logger.warning("no quote for symbols: %s", ", ".join(sorted(missing)))
        _notify_quote_failure(sorted(missing))
    return quotes


def _notify_quote_failure(symbols: list[str]) -> None:
    """全部行情源（东财快照 + 腾讯/新浪逐票兜底）都拿不到报价 → 钉钉告警。

    按日+代码组合去重：同一天同一批代码只提醒一次，不轰炸群聊。
    """
    try:
        from .notifier import notify

        day = date.today().isoformat()
        notify(
            "行情数据源故障",
            f"**状态**：东财快照与腾讯/新浪兜底均失败\n\n"
            f"**缺报价代码**：{', '.join(symbols)}\n\n"
            f"**影响**：相关订单已跳过，网络恢复后下一轮自动补",
            level="warning",
            key=f"quote-fail:{day}:{','.join(symbols)}",
        )
    except Exception:  # noqa: BLE001 — 通知失败不改变主流程
        logger.debug("quote-failure notify failed", exc_info=True)


# ── 账号配置 ──────────────────────────────────────────────────────────────


@dataclass
class AccountConfig:
    """一个自动化账号的全部策略参数（accounts.json 中一项）。"""

    name: str
    broker_settings: dict = field(default_factory=dict)   # broker/paper/qmt/easytrader 配置
    watchlist: list[str] = field(default_factory=list)    # 基础股票池（bare code）
    focus_max: int = 3                                    # 盘前 LLM 筛选后的当日重点数量
    screening_enabled: bool = True                        # 是否启用盘前 LLM 筛选
    rating_weights: dict = field(default_factory=lambda: dict(DEFAULT_RATING_WEIGHTS))
    min_order_value: float = 5_000.0                      # 低于该金额的差额不下单（防折腾）
    large_order_confirm_value: float = 50_000.0           # ≥ 该金额需人工批准
    max_position_pct: float = 0.20                        # 单票上限（与 RiskController 对齐）
    order_fill_timeout_min: float = 15.0                  # 挂单超时撤单（分钟）
    strategy: dict = field(default_factory=dict)          # 盯盘退出策略（stop_loss 等）
    risk: dict = field(default_factory=dict)              # RiskController 参数（仓位/日亏/单日笔数）

    @classmethod
    def from_dict(cls, data: dict) -> "AccountConfig":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known.setdefault("name", "default")
        cfg = cls(**known)
        merged = dict(DEFAULT_RATING_WEIGHTS)
        merged.update(cfg.rating_weights or {})
        cfg.rating_weights = merged
        return cfg


# ── 自动交易器 ────────────────────────────────────────────────────────────


class AutoTrader:
    """单账号无人值守交易循环（三阶段：盘前/盘中/盘后）。"""

    def __init__(
        self,
        account: dict | AccountConfig,
        config: dict | None = None,
        *,
        broker: BaseBroker | None = None,
        graph=None,
        decision_fn=None,
        quote_fn=None,
        approval_store: ApprovalStore | None = None,
        now_fn=None,
        state_dir: str | Path | None = None,
    ):
        self.account = (
            account if isinstance(account, AccountConfig) else AccountConfig.from_dict(account)
        )
        self.config = config or {}
        self._graph = graph
        self._decision_fn = decision_fn          # (symbol, date_str) -> decision text
        self._quote_fn = quote_fn or fetch_quotes_ashare
        self._now_fn = now_fn or datetime.now
        # 状态目录：提供时「今日已分析」标记 / 日亏基线 / 挂单看护落盘，
        # 盘中重启不丢当日状态（守护进程与 UI 都传；测试默认不传）。
        self._state_dir = Path(state_dir) if state_dir else None

        # paper 模式按账号名派生独立状态文件（多账号互不覆盖）
        broker_settings = {
            **self.account.broker_settings,
            "account_name": self.account.name,
        }
        broker = broker or get_broker(broker_settings)
        acct_cfg = {
            "confirm_before_trade": self.account.broker_settings.get(
                "confirm_before_trade", False
            ),
            "account_name": self.account.name,
        }
        if self._state_dir is not None:
            acct_cfg["state_dir"] = str(self._state_dir)
        from .rules import RiskController

        risk_params = {
            k: v for k, v in self.account.risk.items()
            if k in RiskController.__init__.__code__.co_varnames
        }
        self.executor = OrderExecutor(
            broker=broker, config=acct_cfg,
            risk=RiskController(**risk_params) if risk_params else None,
        )
        self.approvals = approval_store or ApprovalStore(self.account.name)

        # 挂单看护：executor 受理即登记，盯盘/盘中轮次对账（成交确认、
        # 超时撤单）。paper 盘即时成交不会进入看护列表。
        tracker_path = (
            self._state_dir / "open_orders" / f"{self.account.name}.json"
            if self._state_dir is not None else None
        )
        self.open_orders = OpenOrderTracker(
            self.account.name, broker, path=tracker_path,
            timeout_min=self.account.order_fill_timeout_min, now_fn=self._now_fn,
        )
        self.executor.on_order_submitted = self._on_order_submitted

        self.today_watchlist: list[str] = []
        self._analyzed: dict[str, str] = self._load_analyzed()   # symbol -> 分析日期，防重复分析
        self._anomaly_wakeups: dict[str, datetime] = {}   # symbol -> 上次异动唤醒时间（冷却护栏）
        self._quotes: dict[str, Quote] = {}
        self._quotes_at: datetime | None = None
        self.strategy = StrategyConfig.from_dict(self.account.strategy)
        self._monitor = None                     # PriceMonitor，惰性创建

    # ── 属性 ──

    @property
    def broker(self) -> BaseBroker:
        return self.executor.broker

    def _today(self) -> str:
        return self._now_fn().strftime("%Y-%m-%d")

    # ── 当日状态落盘（防盘中重启重复分析下单） ──

    def _analyzed_path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return self._state_dir / f"{self.account.name}_analyzed.json"

    def _load_analyzed(self) -> dict[str, str]:
        path = self._analyzed_path()
        if path is None:
            return {}
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                # 只保留当日条目：旧日期本来就不会命中，落盘即清零。
                today = self._today()
                kept = {s: d for s, d in data.items() if d == today}
                if kept:
                    logger.info(
                        "[%s] restored %d analyzed symbol(s) from state (intraday restart)",
                        self.account.name, len(kept),
                    )
                return kept
        except (json.JSONDecodeError, OSError, TypeError):
            logger.warning("[%s] analyzed state unreadable — starting fresh",
                           self.account.name)
        return {}

    def _save_analyzed(self) -> None:
        path = self._analyzed_path()
        if path is None:
            return
        try:
            today = self._today()
            data = {s: d for s, d in self._analyzed.items() if d == today}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError:
            logger.warning("[%s] analyzed state write failed",
                           self.account.name, exc_info=True)

    # ── 挂单看护（成交确认 / 超时撤单） ──

    def _on_order_submitted(self, order, order_result) -> None:
        """executor 受理挂单回调 → 进入看护列表。"""
        self.open_orders.track(
            order_id=order_result.order_id,
            symbol=order.symbol,
            action=order.side.value,
            quantity=order.quantity,
            price=float(order.price or 0.0),
            tag=order.tag,
        )

    def _reconcile_open_orders(self) -> list[dict]:
        """对账挂单：成交确认、超时撤单（含钉钉告警）；返回日志记录。"""
        records: list[dict] = []
        for ev in self.open_orders.reconcile():
            kind = ev["kind"]
            if kind == "filled":
                logger.info(
                    "[%s] open order filled: %s %s x%d (%s)",
                    self.account.name, ev["action"], ev["symbol"],
                    ev["quantity"], ev["order_id"],
                )
                records.append({
                    "symbol": ev["symbol"], "decision": "OPEN_ORDER",
                    "outcome": "FILLED",
                    "detail": f"{ev['action']} x{ev['quantity']} 已成交（{ev['order_id']}）",
                })
                continue
            if kind == "cancelled":
                self._notify_open_order_event(ev)
                records.append({
                    "symbol": ev["symbol"], "decision": "OPEN_ORDER",
                    "outcome": "CANCELLED",
                    "detail": f"挂单超过 {self.account.order_fill_timeout_min:.0f} 分钟未成交，已自动撤单（{ev['order_id']}）",
                })
            elif kind == "cancel_failed":
                self._notify_open_order_event(ev)
                records.append({
                    "symbol": ev["symbol"], "decision": "OPEN_ORDER",
                    "outcome": "CANCEL_FAILED",
                    "detail": f"挂单超时且自动撤单失败：{ev.get('error', '')}（{ev['order_id']}）",
                })
            elif kind == "expired":
                records.append({
                    "symbol": ev["symbol"], "decision": "OPEN_ORDER",
                    "outcome": "EXPIRED",
                    "detail": f"隔日挂单移出看护（{ev['order_id']}）",
                })
        return records

    def _notify_open_order_event(self, ev: dict) -> None:
        """挂单超时撤单（warning）/ 撤单失败（critical）→ 钉钉。"""
        if ev["kind"] == "cancelled":
            title, level = "挂单超时已自动撤单", "warning"
            action = (f"超过 {self.account.order_fill_timeout_min:.0f} 分钟未成交，已自动撤单。"
                      "如仍意向成交请重新下单或等待下一轮分析。")
        else:
            title, level = "挂单超时且撤单失败", "critical"
            action = f"自动撤单失败（{ev.get('error', '未知原因')}），请到交易客户端手动处理。"
        try:
            from .notifier import notify

            notify(
                title,
                f"**账号**：{self.account.name}\n\n"
                f"**订单**：{ev['action'].upper()} {ev['symbol']} ×{ev['quantity']}"
                f" @ {ev['price']:.2f}\n\n"
                f"**挂单时间**：{ev['placed_at']}\n\n"
                f"**动作**：{action}",
                level=level,
                key=f"open-order:{ev['order_id']}",
            )
        except Exception:  # noqa: BLE001 — 通知失败不影响主流程
            logger.debug("open-order notify failed", exc_info=True)

    # ── 决策来源（默认走多代理分析图） ──

    def _get_graph(self):
        if self._graph is None:
            from .default_config import DEFAULT_CONFIG
            from .graph.trading_graph import TradingAgentsGraph

            merged = DEFAULT_CONFIG.copy()
            merged.update(self.config)
            self._graph = TradingAgentsGraph(debug=False, config=merged)
        return self._graph

    def _decide(self, symbol: str) -> str:
        if self._decision_fn is not None:
            return self._decision_fn(symbol, self._today())
        _, decision = self._get_graph().propagate(symbol, self._today())
        return decision

    # ── 行情（60 秒缓存） ──

    def _get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """按需取行情：缓存按代码键控，缺的代码补拉，而不是整表替换。

        原实现 60 秒内直接返回上一次的字典——连续请求不同代码时（盘中
        逐票下单、UI 快速轮询）新代码不在缓存里，会被静默当成"无行情"
        跳过下单。
        """
        now = self._now_fn()
        cache_fresh = (
            self._quotes_at is not None
            and (now - self._quotes_at) <= timedelta(seconds=60)
        )
        if cache_fresh:
            missing = [s for s in symbols if s not in self._quotes]
            if not missing:
                return self._quotes
            self._quotes.update(self._quote_fn(missing))
            self._quotes_at = now
            return self._quotes
        self._quotes = self._quote_fn(symbols)
        self._quotes_at = now
        return self._quotes

    # ── 三阶段 ──

    def run_pre_market(self) -> list[str]:
        """盘前：重置风控基线、过期旧审批、筛选当日重点。"""
        today = self._today()
        self.executor.reset_day()
        self.approvals.expire_before(today)
        self.today_watchlist = self._screen_watchlist()
        logger.info(
            "[%s] pre-market: focus list = %s", self.account.name, self.today_watchlist
        )
        return self.today_watchlist

    def run_intraday(self, force: bool = False) -> list[dict]:
        """盘中：先执行已批准的订单，再对未分析的标的跑分析并下单。"""
        now = self._now_fn()
        if not force and not is_trading_time_today(now):
            logger.info(
                "[%s] intraday skipped: outside trading hours or not a trading day",
                self.account.name,
            )
            return []

        results: list[dict] = []
        results.extend(self._reconcile_open_orders())
        results.extend(self._execute_approved())

        if not self.today_watchlist:
            self.today_watchlist = list(self.account.watchlist)

        for symbol in self.today_watchlist:
            if self._analyzed.get(symbol) == self._today():
                continue
            try:
                results.append(self._analyze_and_trade(symbol))
            except Exception:
                logger.exception("[%s] analysis failed for %s", self.account.name, symbol)
                results.append({
                    "symbol": symbol, "decision": "ERROR",
                    "detail": f"analysis pipeline failed (see log)",
                })
        return results

    def run_post_market(self) -> dict:
        """盘后：T+1 滚动 + 当日汇总 + 净值点记录 + 周期复盘（周五/月末）。"""
        self.broker.next_trading_day(self._today())
        summary = self._daily_summary()
        self._write_summary(summary)
        try:
            from .ui.store import append_equity_point

            append_equity_point(
                self.config.get("results_dir", "."), self.account.name,
                {
                    "date": summary["date"],
                    "total_asset": summary["total_asset"],
                    "cash": summary["available_cash"],
                },
            )
        except Exception as exc:  # 净值记录失败不影响主流程
            logger.debug("equity point not recorded: %s", exc)
        self._maybe_periodic_review()
        return summary

    # ── 周期复盘（周报/月报） ──

    def _maybe_periodic_review(self) -> None:
        """周五盘后出周报、本月最后一个交易日盘后出月报（失败只记日志）。"""
        try:
            now = self._now_fn()
            if now.weekday() == 4:      # 周五
                self.run_review("weekly")
            if self._is_last_trading_day_of_month(now):
                self.run_review("monthly")
        except Exception:
            logger.exception("[%s] periodic review failed", self.account.name)

    def _is_last_trading_day_of_month(self, now: datetime) -> bool:
        try:
            from .dataflows.trading_calendar import is_trading_day

            from .review import is_last_trading_day_of_month
        except Exception:
            logger.debug("trading calendar unavailable — monthly review deferred")
            return False
        try:
            return is_last_trading_day_of_month(now, is_trading_day)
        except Exception:
            logger.debug("last-trading-day check failed", exc_info=True)
            return False

    def run_review(self, kind: str) -> dict | None:
        """生成周报（当周）或月报（当月）；返回指标，无数据时返回 None。

        报告路径 ``results_dir/auto/review/``；数据不足时跳过写盘。
        """
        from . import review

        results_dir = self.config.get("results_dir")
        if not results_dir:
            from .default_config import DEFAULT_CONFIG

            results_dir = DEFAULT_CONFIG.get("results_dir", ".")
        now = self._now_fn()
        if kind == "weekly":
            start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        elif kind == "monthly":
            start = now.strftime("%Y-%m-01")
        else:
            raise ValueError(f"kind must be 'weekly' or 'monthly', got {kind!r}")
        dailies = review.load_daily_summaries(
            results_dir, self.account.name,
            start_date=start, end_date=now.strftime("%Y-%m-%d"),
        )
        metrics = review.write_review(results_dir, self.account.name, kind, dailies)
        if metrics is not None:
            self._notify_review(metrics)
        return metrics

    def _notify_review(self, metrics: dict) -> None:
        """复盘报告生成 → 钉钉 info（附核心指标）。"""
        try:
            from .notifier import notify

            notify(
                f"{metrics['kind_cn']}已生成",
                f"**账号**：{metrics['account']}\n\n"
                f"**区间**：{metrics['start_date']} ~ {metrics['end_date']}"
                f"（{metrics['trading_days']} 个交易日）\n\n"
                f"**区间盈亏**：{metrics['period_pnl']:+,.2f} CNY"
                f"（{metrics['period_return']:+.2%}）\n\n"
                f"**日胜率**：{metrics['day_win_rate']:.0%}；"
                f"成交 {metrics['trade_count']} 笔\n\n"
                f"**报告**：{metrics.get('report_path', '')}",
                level="info",
                key=f"review:{metrics['account']}:{metrics['kind']}:{metrics['end_date']}",
            )
        except Exception:  # noqa: BLE001 — 通知失败不影响复盘
            logger.debug("review notify failed", exc_info=True)

    # ── 盯盘（分钟级，由调度器驱动） ──

    # 异动唤醒 LLM 的最小间隔：同一标的 60 分钟内不重复唤醒（成本护栏）
    ANOMALY_WAKEUP_COOLDOWN_MIN = 60

    def get_monitor(self):
        """惰性创建本账号的 PriceMonitor（UI 与守护进程共用一个实例）。"""
        if self._monitor is None:
            from .monitor import PriceMonitor

            self._monitor = PriceMonitor(
                self.account.name,
                broker=self.broker,
                executor=self.executor,
                strategy=self.strategy,
                on_anomaly=self._on_anomaly,
            )
        return self._monitor

    def _on_anomaly(self, symbol: str, kind: str, detail: str) -> None:
        """异动回调：重置「今日已分析」标记，下一轮盘中扫描重新跑 LLM。

        冷却护栏：同一标的 60 分钟内只唤醒一次——盯盘巡检是分钟级的，
        没有冷却的话持续阴跌会每 5 分钟触发一次全量多代理分析（一次
        约 10+ 次 LLM 调用），成本失控。
        """
        now = self._now_fn()
        last = self._anomaly_wakeups.get(symbol)
        if last is not None and (now - last) < timedelta(
            minutes=self.ANOMALY_WAKEUP_COOLDOWN_MIN
        ):
            logger.debug(
                "[%s] anomaly wakeup for %s in cooldown (last %s)",
                self.account.name, symbol, last.strftime("%H:%M"),
            )
            return
        self._anomaly_wakeups[symbol] = now
        self._analyzed.pop(symbol, None)   # 允许重新分析
        self._save_analyzed()
        logger.info(
            "[%s] anomaly wakeup: %s (%s) — %s; will re-run analysis next intraday scan",
            self.account.name, symbol, kind, detail,
        )

    def run_monitor(self) -> list[dict]:
        """盯盘巡检一次：挂单对账 → 策略评估 → 触发信号自动卖出。非交易日/时段外跳过。"""
        now = self._now_fn()
        if not is_trading_time_today(now):
            logger.debug(
                "[%s] monitor skipped: outside trading hours or not a trading day",
                self.account.name,
            )
            return []
        records = self._reconcile_open_orders()
        records.extend(self.get_monitor().check_once())
        return records

    # ── 手动一键下单（UI 调用；同一套风控与大额审批门控） ──

    def submit_manual_trade(
        self,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        *,
        name: str = "",
        prev_close: float | None = None,
    ) -> dict:
        """UI「一键交易」入口：小额直接下单，实盘大额进审批队列。

        与自动决策共用 executor 的全部风控（涨跌停/手数/T+1/仓位/日亏），
        返回结构化结果字典（含审批 ID，便于 UI 提示）。
        """
        action = action.lower().strip()
        if action not in ("buy", "sell"):
            return {"symbol": symbol, "outcome": "REJECTED", "detail": f"invalid action {action!r}"}

        order_value = price * quantity
        if self.broker.mode != "paper" and order_value >= self.account.large_order_confirm_value:
            entry = self.queue_for_approval(
                symbol, action, quantity, order_value,
                reason=f"manual {action}",
            )
            return {
                "symbol": symbol, "action": action, "quantity": quantity,
                "outcome": "PENDING_APPROVAL",
                "detail": f"订单金额 {order_value:,.0f} 元需审批",
                "approval_id": entry.id,
            }

        result = self.executor.execute(
            symbol=symbol, action=action, price=price, quantity=quantity,
            name=name or symbol, prev_close=prev_close, last_price=price,
            confirm=True, tag="manual",
        )
        record = {
            "symbol": symbol, "action": action, "quantity": quantity,
            "outcome": result.decision, "detail": result.reason,
            "checks": [(c.name, c.passed, c.detail) for c in result.checks],
        }
        if result.decision == PENDING:
            entry = self.queue_for_approval(
                symbol, action, quantity, order_value, reason=f"manual {action}",
            )
            record["outcome"] = "PENDING_APPROVAL"
            record["approval_id"] = entry.id
        return record

    # ── 盘前筛选 ──

    def _screen_watchlist(self) -> list[str]:
        pool = [s for s in self.account.watchlist if s]
        if not pool:
            return []
        if not self.account.screening_enabled or len(pool) <= self.account.focus_max:
            return pool[: self.account.focus_max] if len(pool) > self.account.focus_max else pool

        focus_max = max(1, self.account.focus_max)
        try:
            picked = self._llm_screen(pool, focus_max)
        except Exception as exc:
            logger.warning("[%s] LLM screening failed (%s) — using first %d of pool",
                           self.account.name, exc, focus_max)
            return pool[:focus_max]
        if not picked:
            return pool[:focus_max]
        # 只保留股票池内合法代码，LLM 幻觉的代码直接丢弃。
        valid = [s for s in picked if s in pool]
        return valid or pool[:focus_max]

    def _llm_screen(self, pool: list[str], focus_max: int) -> list[str]:
        from .default_config import DEFAULT_CONFIG
        from .llm_clients import create_llm_client

        provider = self.config.get("llm_provider", DEFAULT_CONFIG["llm_provider"])
        model = self.config.get("quick_think_llm", DEFAULT_CONFIG["quick_think_llm"])
        base_url = self.config.get("backend_url", DEFAULT_CONFIG.get("backend_url"))
        llm = create_llm_client(provider, model, base_url).get_llm()

        market_context = ""
        try:
            from .dataflows.interface import route_to_vendor

            market_context = str(route_to_vendor("get_ashare_market_snapshot"))
        except Exception as exc:
            logger.debug("market snapshot unavailable for screening: %s", exc)

        prompt = (
            "你是A股交易助手。从候选股票池中选出今日最值得重点分析交易的股票。\n"
            f"候选池: {', '.join(pool)}\n\n今日市场概况:\n{market_context}\n\n"
            f"只输出最多 {focus_max} 个股票代码，用英文逗号分隔，不要有任何其他文字。"
            "股票代码必须来自候选池。"
        )
        answer = str(llm.invoke(prompt).content).strip()
        codes = [c.strip() for c in re.split(r"[，,\s]+", answer) if c.strip()]
        return codes[:focus_max]

    # ── 分析 → 下单 ──

    def _analyze_and_trade(self, symbol: str) -> dict:
        today = self._today()
        decision_text = self._decide(symbol)
        rating = parse_rating(decision_text)
        self._analyzed[symbol] = today
        self._save_analyzed()

        if rating is None:
            return {"symbol": symbol, "decision": "UNPARSED", "detail": decision_text[:120]}
        if rating == "hold":
            return {"symbol": symbol, "decision": "hold", "detail": "no action"}

        quote = self._get_quotes([symbol]).get(symbol)
        if quote is None or quote.price <= 0:
            return {"symbol": symbol, "decision": rating,
                    "detail": "no quote — order skipped"}

        intent = self._build_order_intent(symbol, rating, quote)
        if intent is None:
            return {"symbol": symbol, "decision": rating, "detail": "below min order value"}

        action, quantity, note = intent
        order_value = quote.price * quantity

        # 大额门控：实盘（非 paper）且金额达到阈值 → 先入审批队列，不发单。
        if self.broker.mode != "paper" and order_value >= self.account.large_order_confirm_value:
            entry = self.queue_for_approval(
                symbol, action, quantity, order_value, reason=f"{rating}; {note}",
            )
            return {
                "symbol": symbol,
                "decision": rating,
                "action": action,
                "quantity": quantity,
                "outcome": "PENDING_APPROVAL",
                "detail": f"order value {order_value:,.0f} CNY — approve id {entry.id}",
            }

        result = self.executor.execute(
            symbol=symbol,
            action=action,
            price=quote.price,
            quantity=quantity,
            name=quote.name,
            prev_close=quote.prev_close,
            is_st=quote.is_st,
            last_price=quote.price,
            confirm=True,   # 小额订单直接放行；大额已在上方预拦截
            tag=f"auto:{rating}",
        )

        record = {
            "symbol": symbol,
            "decision": rating,
            "action": action,
            "quantity": quantity,
            "outcome": result.decision,
            "detail": result.reason,
            "note": note,
        }

        # executor 层 confirm_before_trade=True 时，任何实盘单都会返回 PENDING：
        # 同样进审批队列，批准后由 _execute_approved 重下。
        if result.decision == PENDING:
            entry = self.queue_for_approval(
                symbol, action, quantity, order_value, reason=f"{rating}; {note}",
            )
            record["outcome"] = "PENDING_APPROVAL"
            record["detail"] = f"confirm_before_trade enabled — approve id {entry.id}"

        return record

    def _build_order_intent(
        self, symbol: str, rating: str, quote: Quote
    ) -> tuple[str, int, str] | None:
        """rating + 当前持仓 → (action, quantity, note)；None = 无需下单。"""
        acct = self.broker.get_account()
        positions = self.broker.get_positions()
        pos = positions.get(symbol)
        total_value = acct.total_asset or acct.available_cash
        weight = self.account.rating_weights.get(rating)

        if rating in ("buy", "overweight") and isinstance(weight, (int, float)):
            current_value = pos.market_value if pos else 0.0
            delta = total_value * float(weight) - current_value
            quantity = size_order(max(delta, 0.0), quote.price, 1.0)  # 全额换算后取整手
            if quantity * quote.price < self.account.min_order_value:
                return None
            return "buy", quantity, f"target weight {float(weight):.0%}"

        if rating in ("underweight", "sell"):
            if not pos or pos.quantity <= 0:
                return None
            if rating == "underweight":
                quantity = (pos.quantity // 2 // 100) * 100          # 减半
            else:
                quantity = ((pos.available or pos.quantity) // 100) * 100  # 清仓可用
            if quantity <= 0 or quantity * quote.price < self.account.min_order_value:
                return None
            return "sell", quantity, "reduce/exit position"

        return None

    # ── 审批流 ──

    def _execute_approved(self) -> list[dict]:
        """执行已批准的待执行订单（重新验证风控，价格用最新行情）。"""
        results: list[dict] = []
        for entry in self.approvals.list("approved"):
            quote = self._get_quotes([entry.symbol]).get(entry.symbol)
            if quote is None or quote.price <= 0:
                results.append({"symbol": entry.symbol, "decision": "APPROVED",
                                "outcome": "SKIPPED", "detail": "no quote"})
                continue
            result = self.executor.execute(
                symbol=entry.symbol,
                action=entry.action,
                price=quote.price,
                quantity=entry.quantity,
                name=quote.name,
                prev_close=quote.prev_close,
                is_st=quote.is_st,
                last_price=quote.price,
                confirm=True,
                tag=f"approved:{entry.id}",
            )
            if result.decision in (EXECUTED, REJECTED):
                self.approvals.set_status(entry.id, "executed" if result.decision == EXECUTED else "rejected")
            results.append({
                "symbol": entry.symbol,
                "decision": f"APPROVED({entry.id})",
                "action": entry.action,
                "quantity": entry.quantity,
                "outcome": result.decision,
                "detail": result.reason,
            })
        return results

    def queue_for_approval(self, symbol: str, action: str, quantity: int,
                           value: float, reason: str) -> PendingOrder:
        """把一笔大额订单意向放入审批队列（AutoTrader 主流程调用）。"""
        seq = len(self.approvals.list()) + 1
        entry = PendingOrder(
            id=f"{self.account.name}-{self._today().replace('-', '')}-{symbol}-{seq}",
            symbol=symbol,
            action=action,
            quantity=quantity,
            estimate_value=value,
            reason=reason,
            created_at=self._now_fn().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.approvals.add(entry)
        logger.info("[%s] large order queued for approval: %s", self.account.name, entry.id)
        self._notify_approval(entry)
        return entry

    def _notify_approval(self, entry: PendingOrder) -> None:
        """大额订单进入审批队列 → 钉钉 warning（等你人工批准，值得被打扰）。"""
        try:
            from .notifier import notify

            notify(
                "大额订单待审批",
                f"**账号**：{self.account.name}\n\n"
                f"**订单**：{entry.action.upper()} {entry.symbol} × {entry.quantity}"
                f"（≈ {entry.estimate_value:,.0f} 元）\n\n"
                f"**依据**：{entry.reason}\n\n"
                f"批准：`python run_auto.py --approve {entry.id}`",
                level="warning",
                key=f"approval:{entry.id}",
            )
        except Exception:  # noqa: BLE001 — 通知失败不影响入队
            logger.debug("approval notify failed", exc_info=True)

    # ── 盘后汇总 ──

    def _daily_summary(self) -> dict:
        acct = self.broker.get_account()
        positions = self.broker.get_positions()
        trades = self.broker.get_trades()
        day_pnl = acct.total_asset - self.executor.day_start_equity
        return {
            "account": self.account.name,
            "date": self._today(),
            "total_asset": acct.total_asset,
            "available_cash": acct.available_cash,
            "day_pnl": day_pnl,
            "positions": {
                s: {"quantity": p.quantity, "market_value": p.market_value}
                for s, p in positions.items()
            },
            "trades_today": len(trades),
            "trades_detail": [
                {
                    "trade_id": t.trade_id,
                    "order_id": t.order_id,
                    "symbol": t.symbol,
                    "name": t.name,
                    "side": t.side.value,
                    "quantity": t.quantity,
                    "price": t.price,
                    "traded_at": t.traded_at.strftime("%Y-%m-%d %H:%M:%S")
                    if t.traded_at else "",
                }
                for t in trades
            ],
            "pending_approvals": len(self.approvals.list("pending"))
            + len(self.approvals.list("approved")),
        }

    def _write_summary(self, summary: dict) -> None:
        try:
            from .default_config import DEFAULT_CONFIG

            base = Path(DEFAULT_CONFIG.get("results_dir", ".")) / "auto"
            base.mkdir(parents=True, exist_ok=True)
            # 同名 JSON 落盘：周报/月报复盘的数据源（md 面向人，JSON 面向聚合）。
            json_path = base / f"{summary['account']}_{summary['date'].replace('-', '')}.json"
            json_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            path = base / f"{summary['account']}_{summary['date'].replace('-', '')}.md"
            lines = [
                f"# 自动交易日报 — {summary['account']} {summary['date']}",
                "",
                f"- 总资产: {summary['total_asset']:,.2f} CNY",
                f"- 可用资金: {summary['available_cash']:,.2f} CNY",
                f"- 当日盈亏: {summary['day_pnl']:+,.2f} CNY",
                f"- 当日成交笔数: {summary['trades_today']}",
                f"- 待审批订单: {summary['pending_approvals']}",
                "",
                "## 持仓",
            ]
            for sym, info in summary["positions"].items():
                lines.append(f"- {sym}: {info['quantity']} 股, 市值 {info['market_value']:,.2f}")
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as exc:
            logger.warning("failed to write daily summary: %s", exc)
