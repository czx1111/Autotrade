"""Order execution pipeline: rules → risk → broker.

The executor is the single choke-point between an agent's trading decision and
a real or simulated order. It layers the two guardrail families in order, so a
rejection carries a precise reason rather than a broker error:

    1. venue rules        ``AShareTradingRules`` — ST blacklist, price-limit
                          band (with clipping), lot size, symbol validity.
    2. portfolio risk     ``RiskController`` — daily order budget, daily loss
                          cap, single-position cap, cash reserve, sector
                          concentration.
    3. T+1 sell check     ``AShareTradingRules.check_t1`` — enough shares were
                          available before today.
    4. human confirmation ``confirm_before_trade`` — live (qmt) orders pause for
                          an explicit confirmation unless told otherwise.

Nothing reaches the broker unless every gate passes. The executor is
broker-agnostic: it talks to any ``BaseBroker`` and reads portfolio state from
the broker's ``get_account`` / ``get_positions``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .broker import BaseBroker, Order, OrderResult, OrderSide, OrderType
from .dataflows.ashare_symbol_utils import parse_ashare_symbol
from .rules import AShareTradingRules, RiskController, RiskDecision

logger = logging.getLogger(__name__)

# Decision outcomes surfaced to the caller / scheduler / logs.
EXECUTED = "EXECUTED"
REJECTED = "REJECTED"
SKIPPED = "SKIPPED"
PENDING = "PENDING_CONFIRMATION"


@dataclass
class CheckOutcome:
    """One guardrail's verdict, kept for audit/traceability."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class ExecutionResult:
    """Result of running one order intent through the pipeline."""

    decision: str
    reason: str = ""
    order_result: OrderResult | None = None
    checks: list[CheckOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.decision == EXECUTED


def size_order(
    total_value: float,
    price: float,
    pct: float,
    lot: int = 100,
) -> int:
    """Convert a portfolio percentage into a whole number of shares.

    ``total_value`` is the portfolio's total asset value, ``price`` the current
    share price, and ``pct`` the target weight (0.05 = 5%). The result is
    rounded down to a multiple of ``lot`` (100 shares for most A-share boards).
    """
    if price <= 0:
        return 0
    target = total_value * pct
    shares = int(target // price)
    return (shares // lot) * lot


class OrderExecutor:
    """Validate and route one order through rules, risk, and the broker."""

    def __init__(
        self,
        broker: BaseBroker,
        rules: AShareTradingRules | None = None,
        risk: RiskController | None = None,
        config: dict | None = None,
        sector_provider=None,
    ):
        self.broker = broker
        self.rules = rules or AShareTradingRules()
        self.risk = risk or RiskController()
        self.config = config or {}
        self._sector_provider = sector_provider  # callable(symbol) -> sector name | None
        # 挂单看护回调（AutoTrader 注册）：order 被 broker 受理（accepted/
        # pending）后调用 (order, order_result)，用于成交确认/超时撤单。
        self.on_order_submitted = None

        # Snapshot the day's opening equity for the daily-loss gate. The
        # scheduler resets this via ``reset_day`` each trading morning.
        # 盘中重启时沿用当日已落盘的基线（state_dir 存在时），否则当天
        # 早前的亏损会被"遗忘"，日亏熔断形同虚设。
        acct = broker.get_account()
        self.day_start_equity = self._init_day_start(acct)

    # ── lifecycle ──

    def reset_day(self) -> None:
        """Re-snapshot opening equity for a new trading day."""
        acct = self.broker.get_account()
        self.day_start_equity = acct.total_asset or acct.available_cash
        path = self._day_start_path()
        if path is not None:
            self._save_day_start(path, self.day_start_equity)

    # ── day-start persistence ──

    def _day_start_path(self) -> Path | None:
        """当日基线落盘路径；未提供 state_dir（如测试）时不持久化。"""
        state_dir = self.config.get("state_dir")
        if not state_dir:
            return None
        return Path(state_dir) / f"{self.config.get('account_name') or 'default'}_day_start.json"

    def _init_day_start(self, acct) -> float:
        equity = acct.total_asset or acct.available_cash
        path = self._day_start_path()
        if path is None:
            return equity
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("date") == date.today().isoformat():
                    logger.info(
                        "day-start equity restored from %s: %s (intraday restart)",
                        path.name, data.get("equity"),
                    )
                    return float(data["equity"])
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            logger.warning("day-start state unreadable — re-snapshot", exc_info=True)
        self._save_day_start(path, equity)
        return equity

    def _save_day_start(self, path: Path, equity: float) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"date": date.today().isoformat(), "equity": equity}),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("day-start state write failed", exc_info=True)

    # ── main entry ──

    def execute(
        self,
        *,
        symbol: str,
        action: str,
        price: float,
        quantity: int,
        name: str | None = None,
        prev_close: float | None = None,
        is_st: bool = False,
        last_price: float | None = None,
        sector: str | None = None,
        confirm: bool = False,
        tag: str = "",
    ) -> ExecutionResult:
        """Run one order intent through every gate and, if it passes, the broker.

        ``action`` is ``"buy"``, ``"sell"``, or ``"hold"`` (case-insensitive).
        ``prev_close`` enables the price-limit band check; ``last_price`` is the
        current market price (used to price market orders and risk math).
        """
        action = action.lower().strip()
        checks: list[CheckOutcome] = []

        if action == "hold":
            return ExecutionResult(SKIPPED, "action is hold — no order placed", checks=checks)

        # 0. symbol validity + board detection
        parsed = parse_ashare_symbol(symbol)
        if parsed is None:
            return ExecutionResult(
                REJECTED, f"invalid A-share symbol {symbol!r}", checks=[
                    CheckOutcome("symbol", False, f"{symbol!r} is not an A-share code"),
                ],
            )
        code = parsed["code"]
        checks.append(CheckOutcome("symbol", True, f"resolved to {code}"))

        # 1. venue rules (ST / price-limit / lot)
        validation = self.rules.validate_order(
            code, action, price, quantity,
            prev_close=prev_close, is_st=is_st, name=name,
        )
        checks.append(CheckOutcome("venue_rules", validation.ok, validation.reason))
        if not validation.ok:
            return ExecutionResult(REJECTED, validation.reason, checks=checks)

        # Apply any auto-adjustments (price clipped into the limit band, quantity
        # rounded down to a valid lot).
        price = validation.adjusted_price or price
        quantity = validation.adjusted_quantity or quantity

        # 2. portfolio risk
        acct = self.broker.get_account()
        positions = self.broker.get_positions()

        # 2a. T+1 on sells — venue constraint, but needs broker position state.
        if action == "sell":
            t1 = self.rules.check_t1(code, quantity, {
                sym: {"quantity": p.quantity, "available": p.available}
                for sym, p in positions.items()
            })
            checks.append(CheckOutcome("t1", t1.ok, t1.reason))
            if not t1.ok:
                return ExecutionResult(REJECTED, t1.reason, checks=checks)

        total_value = acct.total_asset or 0.0
        order_value = price * quantity
        current_position_value = positions.get(code).market_value if code in positions else 0.0

        risk_checks = self._run_risk_checks(
            code=code,
            action=action,
            order_value=order_value,
            total_value=total_value,
            current_position_value=current_position_value,
            available_cash=acct.available_cash,
            sector=sector,
        )
        checks.extend(risk_checks)
        failed = [c for c in risk_checks if not c.passed]
        if failed:
            return ExecutionResult(
                REJECTED, "; ".join(c.detail for c in failed), checks=checks,
            )

        # 3. human confirmation for live (non-paper) accounts
        if self.broker.mode != "paper" and self.config.get("confirm_before_trade", True):
            if not confirm:
                return ExecutionResult(
                    PENDING,
                    "live order requires explicit confirmation (re-run with confirm=True)",
                    checks=checks,
                )

        # 4. place the order
        order = Order(
            symbol=code,
            side=OrderSide.BUY if action == "buy" else OrderSide.SELL,
            quantity=quantity,
            price=price,
            order_type=OrderType.LIMIT,
            tag=tag or name or code,
        )
        order_result = self.broker.place_order(order, last_price=last_price)

        if order_result.status.value == "filled":
            self.risk.record_order()
            return ExecutionResult(
                EXECUTED, order_result.message, order_result=order_result, checks=checks,
            )
        if order_result.status.value in ("accepted", "pending"):
            self.risk.record_order()
            if self.on_order_submitted is not None:
                try:
                    self.on_order_submitted(order, order_result)
                except Exception:
                    logger.exception("on_order_submitted callback failed")
            return ExecutionResult(
                EXECUTED, "order accepted by broker", order_result=order_result, checks=checks,
            )

        return ExecutionResult(
            REJECTED, order_result.message, order_result=order_result, checks=checks,
        )

    # ── risk helpers ──

    def _run_risk_checks(
        self,
        *,
        code: str,
        action: str,
        order_value: float,
        total_value: float,
        current_position_value: float,
        available_cash: float,
        sector: str | None,
    ) -> list[CheckOutcome]:
        checks: list[CheckOutcome] = []

        budget = self.risk.check_order_budget()
        checks.append(CheckOutcome("order_budget", budget.ok, budget.reason))

        loss = self.risk.check_daily_loss_limit(self.day_start_equity, total_value)
        checks.append(CheckOutcome("daily_loss", loss.ok, loss.reason))
        if not loss.ok:
            self._notify_daily_loss_breach(loss.reason)

        # Position-limit only constrains buys (a sell reduces exposure).
        if action == "buy":
            pos = self.risk.check_position_limit(
                code, order_value, total_value, current_position_value,
            )
            checks.append(CheckOutcome("position_limit", pos.ok, pos.reason))

            cash = self.risk.check_cash_reserve(order_value, available_cash)
            checks.append(CheckOutcome("cash_reserve", cash.ok, cash.reason))

            if sector and self._sector_provider is not None:
                by_sector = self._sector_exposure()
                conc = self.risk.check_concentration(
                    sector, order_value, by_sector, total_value,
                )
                checks.append(CheckOutcome("concentration", conc.ok, conc.reason))

        return checks

    def _notify_daily_loss_breach(self, reason: str) -> None:
        """日亏熔断触发时推送钉钉 critical 告警（按日去重）。"""
        try:
            from .notifier import notify

            account = self.config.get("account_name", "?")
            notify(
                "日亏损熔断触发",
                f"**账号**：{account}\n\n**状态**：当日亏损已达限额，暂停开新仓\n\n"
                f"**详情**：{reason}",
                level="critical",
                key=f"daily-loss:{account}:{date.today().isoformat()}",
            )
        except Exception:  # noqa: BLE001 — 告警失败不影响风控判定
            logger.debug("daily-loss notify failed", exc_info=True)

    def _sector_exposure(self) -> dict:
        """Aggregate current position value by sector, using the sector provider."""
        if self._sector_provider is None:
            return {}
        by_sector: dict[str, float] = {}
        for sym, pos in self.broker.get_positions().items():
            sector = self._sector_provider(sym)
            if sector:
                by_sector[sector] = by_sector.get(sector, 0.0) + pos.market_value
        return by_sector
